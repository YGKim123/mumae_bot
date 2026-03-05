"""
strategy.py — 무한매수법칙 V2.2 전략 계산 + 주문 실행

【핵심 공식】
  T값    : ceil(매수누적액 / 1회매수액 * 100) / 100  ← 소수점 2자리 올림
  별%    : target_profit * (1 - 2T / total_a)
           ※ target_profit=10, total_a=40 기준으로 "10 - T/2" 와 동일
  별가   : avg_p * (1 + 별% / 100)
  매수별가: 별가 - 0.01  ← 매도 LOC와 충돌 방지

【매수 주문】
  전반전 (T ≤ total_a/2):
    b1: floor(1회분/2 / avg_p)       @ avg_p    LOC
    b2: floor(1회분/2 / 매수별가)    @ 매수별가 LOC
  후반전 (T > total_a/2):
    b2: floor(1회분 / 매수별가)      @ 매수별가 LOC

【매도 주문】(전후반전 공통)
  3/4 : avg_p * (1 + target_profit%)  지정가
  1/4 : 별가 (star_p)                 LOC
  ※ star_p ≤ avg_p 일 때만 avg_p+0.01 로 자전거래 방지 보정

【쿼터손절 모드】(T >= total_a - 1)
  매수: avg_p * (1 - target_profit%)  LOC  (항상 -10%)
  매도(1~10회차): 1/4 @ -target% LOC  +  3/4 지정가
  매도(10회 완료): 1/4 MOC
"""

import math
import kis_api
import storage
from config import Account, setup_logging

log = setup_logging()


def _calc_loc_sell_price(avg_p: float, star_p: float, b1: int, b2: int) -> tuple[float, bool, str]:
    """
    LOC 매도2 호가를 계산합니다.

    기본값: star_p (별가) — 항상 별가에 LOC 매도를 겁니다.

    자전거래 방지 보정:
      star_p가 동시에 제출되는 매수 LOC 호가(b1=avg_p) 이하일 때만 보정합니다.
      보정값 = avg_p + 0.01 (최소한 매수가보다 $0.01 높게)

      보정이 필요한 케이스: 후반전에서 star_pct가 음수가 되어 star_p < avg_p

      b2(star_p 호가)는 매도 기준가 자체이므로 비교 대상에서 제외합니다.
      줍줍/가속매수는 avg_p 이하이므로 max_buy에 영향을 주지 않습니다.

    Returns:
        loc_sell_p   : 최종 LOC 매도 호가
        was_adjusted : star_p 대비 보정 여부
        reason       : 보정 사유 (보정 시에만)
    """
    # LOC 매도 기준가는 star_p
    loc_sell_p = round(star_p, 2)

    # b1(avg_p 매수)이 있을 때만 자전거래 체크
    if b1 > 0 and avg_p > 0 and star_p <= avg_p:
        adjusted   = round(avg_p + 0.01, 2)
        reason     = (f"star_p({star_p:.2f}) ≤ avg_p({avg_p:.2f}) "
                      f"→ avg_p + 0.01 = {adjusted:.2f} 로 보정")
        return adjusted, True, reason

    return loc_sell_p, False, ""


def build_order_info(acc: Account, ticker: str, no_turbo: bool = False) -> dict:
    s         = acc.strategy[ticker]
    d         = s["data"]
    ota       = s["seed"] / s["total_a"]
    half_amt  = ota / 2.0
    avg_p     = round(d.get("avg", 0), 2)
    qty_total = int(d.get("qty", 0))

    # ❶ T값: 소수점 2자리 올림 (반올림 아님)
    cumul = d.get("cumul", 0)
    if ota > 0 and cumul > 0:
        t_val = math.ceil(cumul / ota * 100) / 100
    else:
        t_val = 0.0

    # ❷ 별% = target_profit * (1 - 2T/total_a)
    #   target=10, total_a=40 기준 "10 - T/2" 공식과 동일
    star_pct = round(s["target_profit"] * (1.0 - 2.0 * t_val / s["total_a"]), 2) if t_val > 0 else s["target_profit"]

    # ❸ 별가(매도 기준), 매수별가(별가-0.01), 목표가
    star_p     = round(avg_p * (1 + star_pct / 100), 2) if avg_p > 0 else 0
    buy_star_p = round(star_p - 0.01, 2)  if star_p > 0 else 0  # 매수별가: 매도 LOC와 충돌 방지
    target_p   = round(avg_p * (1 + s["target_profit"] / 100), 2) if avg_p > 0 else 0

    qty_3_4       = math.floor(qty_total * 0.75)
    qty_1_4       = qty_total - qty_3_4
    is_first_half = t_val <= s["total_a"] / 2

    # ❹ 매수 수량
    if is_first_half:
        # 전반전: 각 절반씩
        b1 = math.floor(half_amt / avg_p)      if avg_p      > 0 else 0
        b2 = math.floor(half_amt / buy_star_p) if buy_star_p > 0 else 0
    else:
        # 후반전: 1회분 전체를 매수별가로
        b1 = 0
        b2 = math.floor(ota / buy_star_p) if buy_star_p > 0 else 0

    # 가속매수 (원문에 없는 추가기능)
    # no_turbo 파라미터 OR strategy config의 use_turbo=False 이면 비활성화
    use_turbo   = s.get("use_turbo", True)
    turbo_price = turbo_qty = 0
    if not no_turbo and use_turbo and avg_p > 0 and qty_total > 0:
        try:
            prev        = kis_api.get_prev_close(acc, ticker)
            base        = min(avg_p, prev)
            turbo_price = round(base * 0.95, 2)
            turbo_qty   = math.floor(ota / turbo_price)
        except Exception:
            pass

    # ❺ LOC 매도가: star_p 그대로. star_p ≤ avg_p 일 때만 보정
    loc_sell_p, loc_adjusted, loc_adjust_reason = _calc_loc_sell_price(avg_p, star_p, b1, b2)
    if loc_adjusted:
        log.warning(f"[자전거래방지][{ticker}] {loc_adjust_reason}")

    # ❻ 쿼터손절 발동: T >= total_a - 1
    force_quarter = t_val >= (s["total_a"] - 1)
    # 쿼터손절 매수가: avg * (1 - target_profit%)
    quarter_buy_p = round(avg_p * (1 - s["target_profit"] / 100), 2) if avg_p > 0 else 0
    quarter_step  = int(d.get("quarter_step", 0))

    return {
        "avg_price":      avg_p,
        "star_price":     star_p,       # 매도 LOC 기준가
        "buy_star_price": buy_star_p,   # 매수 별가 (star_p - 0.01)
        "star_pct":       star_pct,
        "target_price":   target_p,
        "target_profit":  s["target_profit"],
        "b1": b1, "b2": b2,
        "qty_total": qty_total, "qty_3_4": qty_3_4, "qty_1_4": qty_1_4,
        "t_val": t_val, "total_a": s["total_a"], "seed": s["seed"],
        "is_first_half":      is_first_half,
        "turbo_price": turbo_price, "turbo_qty": turbo_qty, "no_turbo": no_turbo,
        "force_quarter_sell": force_quarter,
        "quarter_buy_p":      quarter_buy_p,
        "quarter_step":       quarter_step,
        "loc_sell_price":     loc_sell_p,
        "loc_adjusted":       loc_adjusted,
        "loc_adjust_reason":  loc_adjust_reason,
    }


def estimate_required_amount(info: dict) -> float:
    if info.get("force_quarter_sell"):
        return 0.0
    total = 0.0
    if info["b1"] > 0:
        total += info["b1"] * info["avg_price"]
    if info["b2"] > 0:
        total += info["b2"] * info["star_price"]
    if info["turbo_qty"] > 0:
        total += info["turbo_qty"] * info["turbo_price"]
    if info["avg_price"] > 0:
        ota = info["seed"] / info["total_a"]
        bq  = math.floor(ota / info["avg_price"])
        for i in range(1, 7):
            gp = ota / (bq + i)
            if gp <= 0:
                break
            total += gp
    return total


def adjust_for_cash(acc: Account, all_infos: dict) -> tuple:
    stage     = 0
    total_req = sum(estimate_required_amount(all_infos[t]) for t in all_infos)

    if acc.current_cash > 0 and total_req > acc.current_cash:
        stage = 1
        for t in all_infos:
            all_infos[t] = build_order_info(acc, t, no_turbo=True)
        total_req = sum(estimate_required_amount(all_infos[t]) for t in all_infos)

    if acc.current_cash > 0 and total_req > acc.current_cash and stage == 1:
        stage = 2
        for t in all_infos:
            info = all_infos[t]
            if info["qty_total"] > 0:
                info["force_quarter_sell"] = True
                info["qty_3_4"] = math.floor(info["qty_total"] * 0.75)
                info["qty_1_4"] = info["qty_total"] - info["qty_3_4"]

    return all_infos, stage



def execute_quarter_order(acc: Account, ticker: str, info: dict) -> str:
    """
    쿼터손절 모드 주문 (T >= total_a - 1).
    매수: avg * (1 - target_profit%) LOC
    매도 step<10 : 1/4 @ -target% LOC  +  3/4 지정가
    매도 step==10: 1/4 MOC
    """
    avg_p        = info["avg_price"]
    target_p     = info["target_price"]
    quarter_buy  = info.get("quarter_buy_p", round(avg_p * 0.9, 2))
    qstep        = info.get("quarter_step", 0)
    q_3_4, q_1_4 = info["qty_3_4"], info["qty_1_4"]
    ota          = info["seed"] / info["total_a"]

    lines = [f"[{ticker}][{acc.name}] 🔴 쿼터손절 (step={qstep}) ═══"]

    # 매수: -target% LOC
    buy_qty = math.floor(ota / quarter_buy) if quarter_buy > 0 else 0
    if buy_qty > 0:
        res = kis_api.send_order(acc, ticker, buy_qty, quarter_buy, "BUY", "LOC")
        ok  = res.get("rt_cd") == "0"
        lines.append(f"  매수 -{info['target_profit']:.0f}%(${quarter_buy:.2f}) {buy_qty}주 LOC: {'✅' if ok else '❌ '+res.get('msg1','')}")

    sell_ok = sell_total = 0
    if qstep < 10:
        # 1~10회 매수기간: LOC -target% + 지정가
        loc_sell_q = round(avg_p * (1 - info["target_profit"] / 100), 2)
        if q_1_4 > 0:
            res = kis_api.send_order(acc, ticker, q_1_4, loc_sell_q, "SELL", "LOC")
            sell_total += 1
            ok = res.get("rt_cd") == "0"
            if ok: sell_ok += 1
            lines.append(f"  매도 LOC 1/4({q_1_4}주) ${loc_sell_q:.2f}: {'✅' if ok else '❌ '+res.get('msg1','')}")
        if q_3_4 > 0:
            res = kis_api.send_order(acc, ticker, q_3_4, target_p, "SELL", "00")
            sell_total += 1
            ok = res.get("rt_cd") == "0"
            if ok: sell_ok += 1
            lines.append(f"  매도 지정가 3/4({q_3_4}주) ${target_p:.2f}: {'✅' if ok else '❌ '+res.get('msg1','')}")
    else:
        # 10회 완료 직후: MOC
        if q_1_4 > 0:
            res = kis_api.send_order(acc, ticker, q_1_4, 0, "SELL", "MOC")
            sell_total += 1
            ok = res.get("rt_cd") == "0"
            if ok: sell_ok += 1
            lines.append(f"  매도 MOC 1/4({q_1_4}주): {'✅' if ok else '❌ '+res.get('msg1','')}")

    lines.append(f"  ── 매수 1/1, 매도 {sell_ok}/{sell_total} ──")
    result = "\n".join(lines)
    log.info(result)
    storage.set_lock(acc, ticker)
    return result


def execute_all_order(acc: Account, ticker: str, info: dict) -> str:
    s          = acc.strategy[ticker]
    avg_p      = info["avg_price"]
    star_p     = info["star_price"]
    target_p   = info["target_price"]
    b1, b2     = info["b1"], info["b2"]
    q_3_4      = info["qty_3_4"]
    q_1_4      = info["qty_1_4"]
    turbo_qty  = info["turbo_qty"]
    turbo_p    = info["turbo_price"]
    half_label = "전반전" if info["is_first_half"] else "후반전"

    # ── 보정된 LOC 매도2 호가 사용 ───────────────────────────
    # info에 이미 build_order_info()에서 계산된 값이 있으면 사용,
    # 없으면 (외부에서 직접 info dict를 만든 경우) 재계산
    if "loc_sell_price" in info:
        loc_sell_p   = info["loc_sell_price"]
        loc_adjusted = info.get("loc_adjusted", False)
        loc_reason   = info.get("loc_adjust_reason", "")
    else:
        loc_sell_p, loc_adjusted, loc_reason = _calc_loc_sell_price(avg_p, star_p, b1, b2)

    lines  = [f"[{ticker}][{acc.name}] 통합 주문 ({half_label}) ========"]

    # 자전거래 방지 보정 리포팅
    if loc_adjusted:
        lines.append(f"  ⚠️ [자전거래방지] LOC매도가 보정: {loc_reason}")

    buy_ok = buy_total = 0

    if b1 > 0:
        res = kis_api.send_order(acc, ticker, b1, avg_p, "BUY", "LOC")
        buy_total += 1
        if res.get("rt_cd") == "0":
            buy_ok += 1
            lines.append(f"  매수1 평단(${avg_p:.2f}) {b1}주 LOC: ✅")
        else:
            lines.append(f"  매수1 평단(${avg_p:.2f}) {b1}주 LOC: ❌ ({res.get('msg1','')})")

    if b2 > 0:
        buy_star_p = info.get("buy_star_price", round(star_p - 0.01, 2))
        res = kis_api.send_order(acc, ticker, b2, buy_star_p, "BUY", "LOC")
        buy_total += 1
        ok = res.get("rt_cd") == "0"
        if ok: buy_ok += 1
        lines.append(f"  매수2 별가-0.01(${buy_star_p:.2f}) {b2}주 LOC: {'✅' if ok else '❌ '+res.get('msg1','')}")

    if turbo_qty > 0 and turbo_p > 0:
        res = kis_api.send_order(acc, ticker, turbo_qty, turbo_p, "BUY", "LOC")
        lines.append(f"  가속매수 ${turbo_p:.2f} {turbo_qty}주 LOC: {'✅' if res.get('rt_cd')=='0' else '❌'}")

    if avg_p > 0:
        ota = s["seed"] / s["total_a"]
        bq  = math.floor(ota / avg_p)
        grid_ok = grid_total = 0
        for i in range(1, 7):
            gp = round(ota / (bq + i), 2)
            if gp <= 0:
                break
            grid_total += 1
            res = kis_api.send_order(acc, ticker, 1, gp, "BUY", "LOC")
            if res.get("rt_cd") == "0":
                grid_ok += 1
        lines.append(f"  줍줍 6단계: {grid_ok}/{grid_total}")

    sell_ok = sell_total = 0
    if q_3_4 > 0:
        res = kis_api.send_order(acc, ticker, q_3_4, target_p, "SELL", "00")
        sell_total += 1
        if res.get("rt_cd") == "0":
            sell_ok += 1
            lines.append(f"  매도1 지정가 3/4({q_3_4}주) ${target_p:.2f}: ✅")
        else:
            lines.append(f"  매도1 지정가 3/4({q_3_4}주) ${target_p:.2f}: ❌ ({res.get('msg1','')})")

    if q_1_4 > 0:
        res = kis_api.send_order(acc, ticker, q_1_4, loc_sell_p, "SELL", "LOC")
        sell_total += 1
        adj_mark = " [보정됨]" if loc_adjusted else ""
        if res.get("rt_cd") == "0":
            sell_ok += 1
            lines.append(f"  매도2 LOC 1/4({q_1_4}주) ${loc_sell_p:.2f}{adj_mark}: ✅")
        else:
            lines.append(f"  매도2 LOC 1/4({q_1_4}주) ${loc_sell_p:.2f}{adj_mark}: ❌ ({res.get('msg1','')})")

    lines.append(f"  ── 총계: 매수 {buy_ok}/{buy_total}, 매도 {sell_ok}/{sell_total} ──")
    result = "\n".join(lines)
    log.info(result)
    storage.set_lock(acc, ticker)
    return result
