"""
jobs.py — 스케줄 작업 (계좌별로 독립 실행)
각 job 함수는 cfg.ACCOUNTS 리스트를 순회하며 계좌별로 처리합니다.
"""

import math
import datetime
import pytz

import config as cfg
import kis_api
import storage
import strategy
import reporter
from config import Account, setup_logging

log = setup_logging()
_daily_trade_running = False


# ============================================================
#  계좌 1개 처리 내부 함수
# ============================================================

def _premarket_one(acc: Account):
    order_log = []
    for ticker in acc.strategy:
        if storage.is_locked(acc, ticker):
            continue
        curr_p = kis_api.get_current_price(acc, ticker)
        if curr_p <= 0:
            continue
        s     = acc.strategy[ticker]
        avg_p = s["data"].get("avg", 0)
        qty_t = int(s["data"].get("qty", 0))
        if avg_p <= 0 or qty_t <= 0:
            continue
        target_p = avg_p * (1 + s["target_profit"] / 100)
        if curr_p < target_p:
            continue

        storage.sync_account(acc)
        avg_p = acc.strategy[ticker]["data"].get("avg", 0)
        qty_t = int(acc.strategy[ticker]["data"].get("qty", 0))
        profit_rt = (curr_p - avg_p) / avg_p * 100
        one_time  = s["seed"] / s["total_a"]
        entry_p   = round(curr_p * 1.15, 2)
        entry_qty = math.floor(one_time / curr_p)

        log.info(f"[프리마켓][{acc.name}] {ticker} 목표가 돌파! ${curr_p:.2f} / 평단 ${avg_p:.2f} ({profit_rt:+.1f}%)")
        order_log.append(f"[{ticker}] 목표가 돌파! ${curr_p:.2f} / 평단 ${avg_p:.2f} ({profit_rt:+.1f}%)")

        res_sell = kis_api.send_order(acc, ticker, qty_t, curr_p, "SELL", "00")
        if res_sell.get("rt_cd") != "0":
            log.error(f"[프리마켓][{acc.name}] {ticker} 매도 실패: {res_sell.get('msg1','')}")
            order_log.append(f"  ❌ 매도 실패: {res_sell.get('msg1','')}")
            storage.set_lock(acc, ticker)
            continue

        order_no = res_sell.get("output", {}).get("ODNO", "")
        filled, fq, fp = kis_api.check_order_filled(acc, order_no, ticker, "SELL")
        if filled:
            realized  = (fp - avg_p) * fq
            new_cumul = storage.add_profit(acc, ticker, realized, fq, fp, avg_p, order_no=order_no)
            order_log.append(f"  ✅ 체결 {fq}주 @ ${fp:.2f} | 수익 ${realized:+,.2f} | 누적 ${new_cumul:,.2f}")
            res_buy = kis_api.send_order(acc, ticker, entry_qty, entry_p, "BUY", "LOC")
            order_log.append(f"  {'✅' if res_buy.get('rt_cd')=='0' else '❌'} LOC 재진입 {entry_qty}주 @ ${entry_p:.2f}")
        else:
            order_log.append("  ⚠️ 매도 미체결 — 수동 확인 필요")
        storage.set_lock(acc, ticker)

    if order_log:
        reporter.send_report(acc, trigger="premarket", order_log=order_log)


def _daily_trade_one(acc: Account):
    log.info(f"{'='*60}")
    log.info(f"[자동매매][{acc.name}] CANO:{acc.cano} 시작")
    log.info(f"{'='*60}")

    order_log: list = []
    storage.sync_account(acc)

    profit_tickers: list = []
    normal_tickers: list = []
    all_infos: dict      = {}

    # 단계 1: 목표가 돌파 처리
    for ticker in acc.strategy:
        if storage.is_locked(acc, ticker):
            log.info(f"[자동매매][{acc.name}] {ticker}: 오늘 처리됨 — 건너뜀")
            continue
        s     = acc.strategy[ticker]
        avg_p = s["data"].get("avg", 0)
        qty_t = int(s["data"].get("qty", 0))
        if avg_p <= 0 or qty_t <= 0:
            normal_tickers.append(ticker); continue
        target_p = avg_p * (1 + s["target_profit"] / 100)
        try:
            curr_p = kis_api.get_current_price(acc, ticker)
        except Exception:
            normal_tickers.append(ticker); continue
        if curr_p < target_p:
            normal_tickers.append(ticker); continue

        profit_rt = (curr_p - avg_p) / avg_p * 100
        entry_p   = round(curr_p * 1.15, 2)
        entry_qty = math.floor(s["seed"] / s["total_a"] / curr_p)
        log.info(f"[자동매매][{acc.name}] {ticker} 목표가 달성! ${curr_p:.2f} ({profit_rt:.1f}%)")
        order_log.append(f"[{ticker}] 목표가 달성! ${curr_p:.2f} / 평단 ${avg_p:.2f} ({profit_rt:.1f}%)")

        res_sell = kis_api.send_order(acc, ticker, qty_t, curr_p, "SELL", "00")
        if res_sell.get("rt_cd") == "0":
            order_no = res_sell.get("output", {}).get("ODNO", "")
            filled, fq, fp = kis_api.check_order_filled(acc, order_no, ticker, "SELL")
            if filled:
                realized  = (fp - avg_p) * fq
                new_cumul = storage.add_profit(acc, ticker, realized, fq, fp, avg_p, order_no=order_no)
                order_log.append(f"  ✅ 체결 {fq}주 @ ${fp:.2f} | 수익 ${realized:+,.2f} | 누적 ${new_cumul:,.2f}")
                res_buy = kis_api.send_order(acc, ticker, entry_qty, entry_p, "BUY", "LOC")
                order_log.append(f"  {'✅' if res_buy.get('rt_cd')=='0' else '❌'} LOC 재진입 {entry_qty}주 @ ${entry_p:.2f}")
            else:
                order_log.append("  ⚠️ 매도 미체결 — 수동 확인 필요")
        else:
            order_log.append(f"  ❌ 매도 실패: {res_sell.get('msg1','')}")
        profit_tickers.append(ticker)
        storage.set_lock(acc, ticker)

    # 단계 2: 일반 매수 / 쿼터매도
    for ticker in normal_tickers:
        if storage.is_locked(acc, ticker):
            continue
        s    = acc.strategy[ticker]
        info = strategy.build_order_info(acc, ticker)

        if info["qty_total"] == 0:
            try:
                curr_price = kis_api.get_current_price(acc, ticker)
                ota        = s["seed"] / s["total_a"]
                entry_p    = curr_price * 1.15
                entry_qty  = math.floor(ota / curr_price)
                if entry_qty > 0:
                    res    = kis_api.send_order(acc, ticker, entry_qty, entry_p, "BUY", "LOC")
                    status = "✅" if res.get("rt_cd") == "0" else f"❌ ({res.get('msg1','')})"
                    order_log.append(f"[{ticker}] 최초 진입 {entry_qty}주 @ ${entry_p:.2f}: {status}")
                    storage.set_lock(acc, ticker)
            except Exception as e:
                log.error(f"[자동매매][{acc.name}] {ticker} 최초 진입 오류: {e}")
            continue

        if info["force_quarter_sell"]:
            order_log.append(f"[{ticker}] 쿼터매도 발동 (T={info['t_val']:.1f})")
            if info["qty_1_4"] > 0:
                res = kis_api.send_order(acc, ticker, info["qty_1_4"], 0, "SELL", "MOC")
                order_log.append(f"  MOC 1/4({info['qty_1_4']}주): {'✅' if res.get('rt_cd')=='0' else '❌'}")
            if info["qty_3_4"] > 0:
                res = kis_api.send_order(acc, ticker, info["qty_3_4"], info["target_price"], "SELL", "00")
                order_log.append(f"  지정가 3/4({info['qty_3_4']}주) ${info['target_price']:.2f}: {'✅' if res.get('rt_cd')=='0' else '❌'}")
            storage.set_lock(acc, ticker)
            continue

        all_infos[ticker] = info

    # 단계 3: 자금 조정 후 통합 주문
    if all_infos:
        all_infos, stage = strategy.adjust_for_cash(acc, all_infos)
        if stage > 0:
            log.info(f"[자동매매][{acc.name}] 자금 조정 {stage}단계 (잔고: ${acc.current_cash:,.2f})")

        for ticker, info in all_infos.items():
            if info.get("force_quarter_sell") and info["qty_total"] > 0:
                reason = f"T={info['t_val']:.1f}>=19" if info["t_val"] >= 19 else "잔금소진"
                order_log.append(f"[{ticker}] 쿼터매도 ({reason})")
                if info["qty_1_4"] > 0:
                    res = kis_api.send_order(acc, ticker, info["qty_1_4"], 0, "SELL", "MOC")
                    order_log.append(f"  MOC 1/4({info['qty_1_4']}주): {'✅' if res.get('rt_cd')=='0' else '❌'}")
                if info["qty_3_4"] > 0:
                    res = kis_api.send_order(acc, ticker, info["qty_3_4"], info["target_price"], "SELL", "00")
                    order_log.append(f"  지정가 3/4({info['qty_3_4']}주) ${info['target_price']:.2f}: {'✅' if res.get('rt_cd')=='0' else '❌'}")
                storage.set_lock(acc, ticker)
            else:
                result = strategy.execute_all_order(acc, ticker, info)
                order_log.extend(result.splitlines())

    log.info(f"[자동매매][{acc.name}] 완료 | 잔고: ${acc.current_cash:,.2f}")
    reporter.send_report(acc, trigger="daily", order_log=order_log)


def _build_planned_orders(acc: Account) -> list:
    """
    오늘 자동매매에서 넣을 예정인 주문 목록을 반환.
    BIL 매도 시 저장해두고, 더블체크 때 실제 접수 여부 대조에 사용.

    반환 형식:
      [{"ticker": "SOXL", "side": "BUY", "qty": 5, "price": 65.86, "order_type": "LOC"}, ...]
    """
    planned = []
    for ticker in acc.strategy:
        if storage.is_locked(acc, ticker):
            continue
        try:
            info = strategy.build_order_info(acc, ticker)

            # 쿼터매도 발동 케이스
            if info["force_quarter_sell"] and info["qty_total"] > 0:
                if info["qty_1_4"] > 0:
                    planned.append({"ticker": ticker, "side": "SELL",
                                    "qty": info["qty_1_4"], "price": 0,
                                    "order_type": "MOC"})
                if info["qty_3_4"] > 0:
                    planned.append({"ticker": ticker, "side": "SELL",
                                    "qty": info["qty_3_4"], "price": info["target_price"],
                                    "order_type": "00"})
                continue

            # 최초 진입
            if info["qty_total"] == 0:
                curr_p = kis_api.get_current_price(acc, ticker)
                if curr_p > 0:
                    s   = acc.strategy[ticker]
                    ota = s["seed"] / s["total_a"]
                    ep  = round(curr_p * 1.15, 2)
                    eq  = math.floor(ota / curr_p)
                    if eq > 0:
                        planned.append({"ticker": ticker, "side": "BUY",
                                        "qty": eq, "price": ep,
                                        "order_type": "LOC"})
                continue

            # 통합 주문: 매수들
            if info["b1"] > 0:
                planned.append({"ticker": ticker, "side": "BUY",
                                 "qty": info["b1"], "price": info["avg_price"],
                                 "order_type": "LOC"})
            if info["b2"] > 0:
                planned.append({"ticker": ticker, "side": "BUY",
                                 "qty": info["b2"], "price": info["star_price"],
                                 "order_type": "LOC"})
            if info["turbo_qty"] > 0:
                planned.append({"ticker": ticker, "side": "BUY",
                                 "qty": info["turbo_qty"], "price": info["turbo_price"],
                                 "order_type": "LOC"})
            # 줍줍 6단계
            avg_p = info["avg_price"]
            if avg_p > 0:
                s   = acc.strategy[ticker]
                ota = s["seed"] / s["total_a"]
                bq  = math.floor(ota / avg_p)
                for i in range(1, 7):
                    gp = round(ota / (bq + i), 2)
                    if gp > 0:
                        planned.append({"ticker": ticker, "side": "BUY",
                                         "qty": 1, "price": gp,
                                         "order_type": "LOC"})
            # 매도들
            if info["qty_3_4"] > 0:
                planned.append({"ticker": ticker, "side": "SELL",
                                 "qty": info["qty_3_4"], "price": info["target_price"],
                                 "order_type": "00"})
            if info["qty_1_4"] > 0:
                planned.append({"ticker": ticker, "side": "SELL",
                                 "qty": info["qty_1_4"],
                                 "price": info.get("loc_sell_price", 0),
                                 "order_type": "LOC"})
        except Exception as e:
            log.debug(f"[계획주문][{acc.name}] {ticker} 오류: {e}")
    return planned


def _calc_daily_needed(acc: Account) -> float:
    """
    오늘 자동매매에 필요한 예상 금액 계산.
    (b1 평단매수 + b2 별가매수 + 줍줍 6단계 + 터보매수)
    """
    needed = 0.0
    for ticker in acc.strategy:
        if storage.is_locked(acc, ticker):
            continue
        try:
            info = strategy.build_order_info(acc, ticker)
            # 매수 주문 금액
            needed += info["b1"] * info["avg_price"]
            needed += info["b2"] * info["star_price"]
            # 줍줍 6단계 (각 1주씩)
            avg_p = info["avg_price"]
            if avg_p > 0:
                s   = acc.strategy[ticker]
                ota = s["seed"] / s["total_a"]
                bq  = math.floor(ota / avg_p)
                for i in range(1, 7):
                    gp = round(ota / (bq + i), 2)
                    if gp > 0:
                        needed += gp
            # 터보 매수
            needed += info["turbo_qty"] * info["turbo_price"]
        except Exception as e:
            log.debug(f"[BIL][{acc.name}] {ticker} 주문정보 오류: {e}")
    return needed


def _bil_manage_one(acc: Account):
    """
    BIL(단기국채 ETF) 버퍼 관리 — 수수료 최소화를 위한 1주일 단위 운용.

    [매수 조건] 예수금 > 1주일치 필요금 + BIL_BUFFER_USD
      → 초과분으로 BIL MOO 매수 (장기 파킹)

    [매도 조건] 예수금 < 오늘 하루치 필요금
      → 오늘 부족분만큼 BIL MOO 매도 (최소 매도)

    [유지 구간] 오늘치 ≤ 예수금 ≤ 1주일치+버퍼
      → 아무것도 안 함 (수수료 절약)

    주문 타입 MOO: 미국장 개장(KST 23:30)에 시장가 체결.
    KST 18:25(자동매매 5분 전)에 실행됩니다.
    """
    if not cfg.BIL_ENABLED:
        return

    storage.sync_account(acc)
    cash = max(acc.current_cash, 0.0)

    daily_needed  = _calc_daily_needed(acc)
    weekly_needed = daily_needed * cfg.BIL_WEEKLY_DAYS   # 기본 5 (영업일)

    bil_qty, bil_price = kis_api.get_bil_balance(acc)
    if bil_price <= 0:
        bil_price = kis_api.get_current_price(acc, "BIL")
    if bil_price <= 0:
        log.warning(f"[BIL][{acc.name}] BIL 시세 조회 실패 — 관리 건너뜀")
        return

    buy_threshold  = weekly_needed + cfg.BIL_BUFFER_USD   # 이 이상이면 매수
    sell_threshold = daily_needed                          # 이 미만이면 매도

    log.info(
        f"[BIL][{acc.name}] 예수금 ${cash:,.2f} | "
        f"오늘 ${daily_needed:,.2f} | 1주일 ${weekly_needed:,.2f} | "
        f"매수기준 ${buy_threshold:,.2f} | BIL {bil_qty}주 @ ${bil_price:.2f}"
    )

    # ── 매수: 1주일치+버퍼 초과분 파킹 ─────────────────────
    if cash > buy_threshold:
        surplus = cash - buy_threshold
        buy_qty = int(surplus / bil_price)
        if buy_qty > 0:
            res = kis_api.send_order(acc, "BIL", buy_qty, 0, "BUY", "MOO")
            ok  = res.get("rt_cd") == "0"
            log.info(
                f"[BIL][{acc.name}] MOO 매수 {buy_qty}주 "
                f"(여유 ${surplus:,.2f}): {'✅' if ok else '❌ ' + res.get('msg1','')}"
            )
            return

    # ── 매도: 오늘치 부족분만 충당 ──────────────────────────
    if cash < sell_threshold and bil_qty > 0:
        short    = sell_threshold - cash
        sell_qty = min(math.ceil(short / bil_price), bil_qty)
        if sell_qty > 0:
            res = kis_api.send_order(acc, "BIL", sell_qty, 0, "SELL", "MOO")
            ok  = res.get("rt_cd") == "0"
            log.info(
                f"[BIL][{acc.name}] MOO 매도 {sell_qty}주 "
                f"(부족분 ${short:,.2f} 충당): {'✅' if ok else '❌ ' + res.get('msg1','')}"
            )
            if ok:
                # 정규장 더블체크용: 오늘 넣어야 할 주문 목록 계산해서 저장
                planned = _build_planned_orders(acc)
                storage.set_bil_sold_today(acc, planned)
                log.info(f"[BIL][{acc.name}] 더블체크 계획 주문 {len(planned)}건 저장")
            return

    log.info(f"[BIL][{acc.name}] 유지 구간 (${sell_threshold:,.2f}~${buy_threshold:,.2f}) — 거래 없음")


def _doublecheck_one(acc: Account) -> list[str]:
    """
    BIL 매도가 있었던 날 정규장 개장 후 주문 거부 여부 확인 및 재주문.

    비교 방식:
      - 18:25에 저장된 "계획 주문 목록" vs 실제 체결+미체결 내역
      - 체결/미체결 어디에도 없는 주문 = 예수금 부족으로 거부된 것
      - 거부된 주문은 원래 타입(LOC/지정가/MOC)으로 재접수

    주의: 일부 주문은 정상 접수되었을 수 있으므로 전체 재주문이 아닌
          누락된 것만 선별해서 재주문.

    반환: 텔레그램 알림용 메시지 리스트
    """
    msgs = []

    bil_sold, planned_orders = storage.get_bil_sold_today(acc)
    if not bil_sold:
        return msgs  # BIL 매도 없었던 날은 스킵

    if not planned_orders:
        log.warning(f"[더블체크][{acc.name}] 계획 주문 목록 없음 — 스킵")
        storage.clear_bil_sold(acc)
        return msgs

    log.info(f"[더블체크][{acc.name}] 시작 | 계획 주문 {len(planned_orders)}건 대조")

    # ── 실제 오늘 접수된 주문 수집 (체결 + 미체결) ───────────
    # 매수 주문은 (ticker, side, qty, price) 조합으로 식별
    # → price 기준으로 중복 주문 방지

    # 체결 내역에서 수집
    filled_set: set[tuple] = set()   # (ticker, side, price_rounded)
    filled_res = kis_api.query_filled_orders(acc)
    if filled_res.get("rt_cd") == "0":
        for o in filled_res.get("output", []):
            t  = o.get("ovrs_pdno", "")
            sd = "BUY" if o.get("sll_buy_dvsn_cd", "") == "02" else "SELL"
            p  = round(float(o.get("ft_ccld_unpr", o.get("ft_ord_unpr3", "0"))), 2)
            filled_set.add((t, sd, p))

    # 미체결 내역에서 수집
    pending_set: set[tuple] = set()  # (ticker, side, price_rounded)
    pending_res = kis_api.query_pending_orders(acc)
    if pending_res.get("rt_cd") == "0":
        for o in pending_res.get("output", []):
            t  = o.get("pdno", "")
            sd = "BUY" if o.get("sll_buy_dvsn_cd", "") == "02" else "SELL"
            p  = round(float(o.get("ft_ord_unpr3", "0")), 2)
            pending_set.add((t, sd, p))

    accepted_set = filled_set | pending_set  # 체결 또는 미체결로 접수된 것

    # ── 계획 주문 vs 실제 대조 ───────────────────────────────
    rejected = []   # 거부(누락)된 주문
    ok_count = 0

    for plan in planned_orders:
        ticker     = plan["ticker"]
        side       = plan["side"]
        price      = round(float(plan["price"]), 2)
        qty        = plan["qty"]
        order_type = plan["order_type"]

        key = (ticker, side, price)
        if key in accepted_set:
            ok_count += 1
            continue

        # LOC/MOC 주문은 price=0으로 저장되므로 0짜리 key도 확인
        key_zero = (ticker, side, 0.0)
        if price == 0.0 and key_zero in accepted_set:
            ok_count += 1
            continue

        # 없으면 거부된 것으로 판단
        rejected.append(plan)

    msgs.append(
        f"📊 계획 {len(planned_orders)}건 중 "
        f"정상 {ok_count}건 / 거부 {len(rejected)}건"
    )

    if not rejected:
        msgs.append("✅ 모든 주문 정상 접수됨")
        storage.clear_bil_sold(acc)
        log.info(f"[더블체크][{acc.name}] 전체 정상 — 플래그 해제")
        return msgs

    # ── 거부된 주문 재접수 (원래 타입 그대로) ───────────────
    log.warning(f"[더블체크][{acc.name}] 거부 주문 {len(rejected)}건 재접수 시작")
    for plan in rejected:
        ticker     = plan["ticker"]
        side       = plan["side"]
        qty        = plan["qty"]
        price      = plan["price"]
        order_type = plan["order_type"]
        side_str   = "매수" if side == "BUY" else "매도"
        price_str  = f"${price:.2f}" if price > 0 else "LOC/MOC"

        log.info(f"[더블체크][{acc.name}] 재주문: {ticker} {side_str} {qty}주 "
                 f"@ {price_str} ({order_type})")
        res = kis_api.send_order(acc, ticker, qty, price, side, order_type)
        ok  = res.get("rt_cd") == "0"
        status = "✅" if ok else f"❌ {res.get('msg1', '')}"
        msgs.append(
            f"🔄 [{ticker}] {side_str} {qty}주 @ {price_str} ({order_type}): {status}"
        )

    storage.clear_bil_sold(acc)
    log.info(f"[더블체크][{acc.name}] 완료 — 플래그 해제")
    return msgs


def _settlement_one(acc: Account):
    log.info(f"[체결확인][{acc.name}] 조회 시작")
    res = kis_api.query_filled_orders(acc)
    if res.get("rt_cd") != "0":
        log.error(f"[체결확인][{acc.name}] 실패: {res.get('msg1','')}")
        return

    orders = res.get("output", [])
    if not orders:
        log.info(f"[체결확인][{acc.name}] 체결 내역 없음")
        return

    storage.sync_account(acc)
    buy_cnt = sell_cnt = 0
    buy_amt = sell_amt = 0.0

    for o in orders:
        ticker       = o.get("ovrs_pdno", "?")
        side         = "매수" if o.get("sll_buy_dvsn_cd", "") == "02" else "매도"
        filled_qty   = int(float(o.get("ft_ccld_qty", "0")))
        filled_price = float(o.get("ft_ccld_unpr3", o.get("ft_ccld_unpr", "0")))
        total        = filled_qty * filled_price
        order_no     = o.get("odno", "")
        if filled_qty <= 0:
            continue
        log.info(f"  [{acc.name}] {ticker} {side} {filled_qty}주 x ${filled_price:.2f} = ${total:.2f}")
        if side == "매수":
            buy_cnt += 1; buy_amt += total
        else:
            sell_cnt += 1; sell_amt += total
            if ticker in acc.strategy:
                avg_p = acc.strategy[ticker]["data"]["avg"]
                if avg_p > 0:
                    realized  = (filled_price - avg_p) * filled_qty
                    new_cumul = storage.add_profit(acc, ticker, realized, filled_qty, filled_price, avg_p, order_no=order_no)
                    log.info(f"    → 실현수익 ${realized:+,.2f} (누적 ${new_cumul:,.2f})")

    order_log = [
        f"매수 {buy_cnt}건  합계 ${buy_amt:,.2f}",
        f"매도 {sell_cnt}건  합계 ${sell_amt:,.2f}",
    ]
    log.info(f"[체결확인][{acc.name}] 완료 | 매수 {buy_cnt}건 / 매도 {sell_cnt}건")
    reporter.send_report(acc, trigger="settlement", order_log=order_log)


# ============================================================
#  스케줄 진입점 (모든 계좌 순회)
# ============================================================

def job_premarket_check():
    kst     = pytz.timezone("Asia/Seoul")
    now_kst = datetime.datetime.now(kst)
    if not (now_kst.hour == 18 and now_kst.minute < 30):
        return
    if not kis_api.is_market_open_today():
        return
    log.info("[프리마켓] 목표가 체크 시작")
    for acc in cfg.ACCOUNTS:
        _premarket_one(acc)


def job_daily_trade():
    global _daily_trade_running
    if _daily_trade_running:
        log.warning("[자동매매] 이미 실행 중")
        return
    if not kis_api.is_market_open_today():
        log.info("[자동매매] 휴장일 — 건너뜀")
        return
    _daily_trade_running = True
    try:
        for acc in cfg.ACCOUNTS:
            _daily_trade_one(acc)
    finally:
        _daily_trade_running = False


def job_settlement_check():
    import pandas_market_calendars as mcal
    est   = pytz.timezone("US/Eastern")
    today = datetime.datetime.now(est).date()
    nyse  = mcal.get_calendar("NYSE")
    yest  = today - datetime.timedelta(days=1)
    if nyse.schedule(start_date=today, end_date=today).empty and \
       nyse.schedule(start_date=yest, end_date=yest).empty:
        return
    for acc in cfg.ACCOUNTS:
        _settlement_one(acc)


def job_token_renewal():
    for acc in cfg.ACCOUNTS:
        kis_api.renew_token_if_needed(acc)


def job_doublecheck() -> dict[str, list[str]]:
    """
    KST 00:10 — 정규장 개장(23:30) 40분 후 주문 더블체크.
    BIL 매도가 있었던 계좌만 SOXL/TQQQ 미체결/체결 확인.
    반환: {acc.name: [메시지, ...]}
    """
    if not kis_api.is_market_open_today():
        return {}
    results = {}
    for acc in cfg.ACCOUNTS:
        try:
            msgs = _doublecheck_one(acc)
            if msgs:
                results[acc.name] = msgs
        except Exception as e:
            log.error(f"[더블체크][{acc.name}] 오류: {e}")
    return results


def job_bil_manage():
    """KST 18:25 — 자동매매(18:30) 5분 전 BIL 버퍼 점검 및 MOO 주문."""
    if not cfg.BIL_ENABLED:
        return
    if not kis_api.is_market_open_today():
        log.info("[BIL] 휴장일 — 건너뜀")
        return
    log.info("[BIL] 버퍼 관리 시작")
    for acc in cfg.ACCOUNTS:
        try:
            _bil_manage_one(acc)
        except Exception as e:
            log.error(f"[BIL][{acc.name}] 오류: {e}")
