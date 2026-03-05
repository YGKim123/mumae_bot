"""
calculator.py — 무한매수법칙 전략 시뮬레이터 (주문 없음)
=========================================================

현재 보유 상황을 직접 입력하면 다음날 주문 계획을 계산해줍니다.
실제 KIS API / 계좌 연결 불필요 — .env 없이도 동작합니다.

사용법:
  python calculator.py                 # 대화형 입력
  python calculator.py --ticker SOXL --avg 63.80 --qty 25 --seed 10000 --t 20 --target 12

또는 코드에서 import 해서 직접 호출:
  from calculator import simulate
  result = simulate(ticker="SOXL", avg=63.80, qty=25, seed=10000, total_a=20, target_profit=12.0)
  print(result["summary"])
"""

import math
import argparse
import sys

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False


# ============================================================
#  핵심 계산
# ============================================================

def simulate(
    ticker:        str,
    avg:           float,   # 평단가
    qty:           int,     # 보유 수량
    seed:          float,   # 종목 시드 (USD)
    total_a:       float,   # 총 분할 횟수 (기본 20)
    target_profit: float,   # 목표 수익률 % (예: 12.0)
    current_price: float = 0.0,  # 현재가 (0이면 yfinance로 조회 시도)
    prev_close:    float = 0.0,  # 전일 종가 (0이면 yfinance로 조회 시도)
    no_turbo:      bool  = False,
) -> dict:
    """
    전략 계산 결과를 dict로 반환.

    반환값 구조:
      summary      : 출력용 텍스트 (print에 바로 사용 가능)
      orders       : 주문 리스트 [{"type", "side", "qty", "price", "amount", "note"}, ...]
      t_val        : 현재 T값
      star_pct     : 현재 별% (다음 매수 목표 수익률)
      target_price : 목표가 (익절 기준)
      total_buy_est: 매수에 필요한 예상 금액
    """

    # ── 1. 가격 조회 ──
    if current_price <= 0 and HAS_YF:
        try:
            current_price = yf.Ticker(ticker).fast_info["last_price"]
        except Exception:
            current_price = 0.0

    if prev_close <= 0 and HAS_YF:
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if len(hist) >= 2:
                prev_close = float(hist["Close"].iloc[-2])
            elif len(hist) >= 1:
                prev_close = float(hist["Close"].iloc[-1])
        except Exception:
            prev_close = 0.0

    # ── 2. 기본 수치 ──
    ota      = seed / total_a          # 1회분 금액
    half_amt = ota / 2.0
    cumul    = avg * qty               # 총 매입금

    t_val    = round(cumul / ota, 2) if ota > 0 and cumul > 0 else 0.0
    star_pct = round(target_profit * (1.0 - 2.0 * t_val / total_a), 2) if t_val > 0 else target_profit
    star_p   = avg * (1 + star_pct / 100) if avg > 0 else 0
    target_p = avg * (1 + target_profit / 100) if avg > 0 else 0

    qty_3_4 = math.floor(qty * 0.75)
    qty_1_4 = qty - qty_3_4

    is_first_half = t_val <= total_a / 2

    # ── 3. 매수 수량 계산 ──
    if is_first_half:
        b2 = math.floor(half_amt / star_p) if star_p > 0 else 0
        b1 = (math.floor(half_amt / avg)   + 1) if avg > 0 else 0
    else:
        b2 = math.floor(ota / star_p) if star_p > 0 else 0
        b1 = 0

    # ── 4. 가속 매수 ──
    turbo_price = turbo_qty = 0
    if not no_turbo and avg > 0 and qty > 0 and prev_close > 0:
        base        = min(avg, prev_close)
        turbo_price = base * 0.95
        turbo_qty   = math.floor(ota / turbo_price)

    # ── 5. 줍줍 (6단계 그리드) ──
    grid_orders = []
    if avg > 0:
        bq = math.floor(ota / avg)
        for i in range(1, 7):
            gp = round(ota / (bq + i), 2)
            if gp <= 0:
                break
            grid_orders.append({"price": gp, "qty": 1, "amount": gp * 1})

    # ── 6. 주문 목록 조립 ──
    orders = []
    total_buy_est = 0.0

    if b1 > 0:
        amt = b1 * avg
        orders.append({"type": "기본매수1", "side": "BUY",  "qty": b1, "price": avg,    "amount": amt,  "note": "LOC 평단가"})
        total_buy_est += amt

    if b2 > 0:
        amt = b2 * star_p
        orders.append({"type": "기본매수2", "side": "BUY",  "qty": b2, "price": star_p, "amount": amt,  "note": f"LOC 별가 ({star_pct:.2f}%)"})
        total_buy_est += amt

    if turbo_qty > 0:
        amt = turbo_qty * turbo_price
        orders.append({"type": "가속매수",  "side": "BUY",  "qty": turbo_qty, "price": turbo_price, "amount": amt, "note": "LOC (평단/전일 중 낮은가 × 0.95)"})
        total_buy_est += amt

    for i, g in enumerate(grid_orders, 1):
        orders.append({"type": f"줍줍{i}",  "side": "BUY",  "qty": g["qty"], "price": g["price"], "amount": g["amount"], "note": "LOC 그리드"})
        total_buy_est += g["amount"]

    if qty > 0:
        if qty_3_4 > 0:
            orders.append({"type": "매도예약1", "side": "SELL", "qty": qty_3_4, "price": target_p, "amount": qty_3_4 * target_p, "note": "지정가 3/4"})
        if qty_1_4 > 0:
            orders.append({"type": "매도예약2", "side": "SELL", "qty": qty_1_4, "price": star_p + 0.01, "amount": qty_1_4 * (star_p + 0.01), "note": "LOC 1/4"})

    # ── 7. 현재 손익 ──
    pnl_pct = (current_price - avg) / avg * 100 if avg > 0 and current_price > 0 else 0.0
    pnl_amt = (current_price - avg) * qty          if avg > 0 and current_price > 0 else 0.0
    to_target_pct = (target_p - current_price) / current_price * 100 if current_price > 0 and target_p > 0 else 0.0

    force_quarter = t_val >= 19

    # ── 8. 요약 텍스트 생성 ──
    half_label = "전반전" if is_first_half else "후반전"
    W = 60

    lines = [
        "=" * W,
        f"  {ticker} 무한매수 계산 결과",
        "=" * W,
        f"  시드: ${seed:,.0f}  |  분할: {int(total_a)}회  |  목표수익률: {target_profit}%",
        "-" * W,
        f"  현재 상태",
        f"    T값:    {t_val:.2f} / {int(total_a)}  ({half_label})",
        f"    별%:    {star_pct:.2f}%",
        f"    보유:   {qty}주  @  ${avg:.4f}  (매입금 ${cumul:,.2f})",
    ]

    if current_price > 0:
        lines += [
            f"    현재가: ${current_price:.4f}  ({pnl_pct:+.2f}%  /  ${pnl_amt:+,.2f})",
            f"    목표가: ${target_p:.4f}  (현재가 대비 {to_target_pct:+.2f}% 필요)",
        ]
    else:
        lines.append(f"    목표가: ${target_p:.4f}")

    if prev_close > 0:
        lines.append(f"    전일종가: ${prev_close:.4f}")

    if force_quarter:
        lines += [
            "-" * W,
            "  ⚠️  T≥19 — 쿼터손절 발동",
            f"    MOC 매도 1/4: {qty_1_4}주",
            f"    지정가 매도 3/4: {qty_3_4}주  @ ${target_p:.4f}",
        ]
    else:
        lines += [
            "-" * W,
            f"  다음날 주문 계획  (총 매수 필요금액: ${total_buy_est:,.2f})",
        ]
        for o in orders:
            side_kr = "매수" if o["side"] == "BUY" else "매도"
            lines.append(
                f"    [{o['type']:6s}] {side_kr}  {o['qty']:4d}주  @  ${o['price']:>9.4f}"
                f"  ≈ ${o['amount']:>9.2f}  ({o['note']})"
            )

    lines.append("=" * W)

    return {
        "ticker":         ticker,
        "t_val":          t_val,
        "star_pct":       star_pct,
        "target_price":   target_p,
        "star_price":     star_p,
        "is_first_half":  is_first_half,
        "force_quarter":  force_quarter,
        "orders":         orders,
        "total_buy_est":  total_buy_est,
        "current_price":  current_price,
        "pnl_pct":        pnl_pct,
        "pnl_amt":        pnl_amt,
        "summary":        "\n".join(lines),
    }


# ============================================================
#  대화형 / CLI 진입점
# ============================================================

def _interactive():
    """대화형 모드: 직접 값을 입력받아 계산."""
    print("\n" + "=" * 60)
    print("  무한매수법칙 계산기  (주문 없음 — 시뮬레이션 전용)")
    print("=" * 60)
    print("  현재 보유 상황을 입력하면 다음날 주문 계획을 계산합니다.")
    print("  현재가 / 전일종가는 yfinance로 자동 조회됩니다.")
    print("=" * 60 + "\n")

    ticker        = input("종목 (예: SOXL): ").strip().upper()
    avg           = float(input("평단가 (USD, 예: 63.80): ").strip())
    qty           = int(input("보유 수량 (주, 예: 25): ").strip())
    seed          = float(input("종목 시드 (USD, 예: 10000): ").strip())
    total_a       = float(input("분할 횟수 (기본 20): ").strip() or "20")
    target_profit = float(input("목표 수익률 % (예: 12): ").strip())
    no_turbo_str  = input("가속매수 제외? (y/N): ").strip().lower()
    no_turbo      = no_turbo_str == "y"

    result = simulate(ticker, avg, qty, seed, total_a, target_profit, no_turbo=no_turbo)
    print("\n" + result["summary"])


def _cli(args):
    """CLI 모드."""
    result = simulate(
        ticker        = args.ticker.upper(),
        avg           = args.avg,
        qty           = args.qty,
        seed          = args.seed,
        total_a       = args.t,
        target_profit = args.target,
        current_price = getattr(args, "price", 0.0),
        prev_close    = getattr(args, "prev", 0.0),
        no_turbo      = getattr(args, "no_turbo", False),
    )
    print(result["summary"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="무한매수법칙 전략 계산기 (주문 없음)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ticker",   type=str,   default="", help="종목 (예: SOXL)")
    parser.add_argument("--avg",      type=float, default=0,  help="평단가")
    parser.add_argument("--qty",      type=int,   default=0,  help="보유 수량")
    parser.add_argument("--seed",     type=float, default=0,  help="시드 (USD)")
    parser.add_argument("--t",        type=float, default=20, help="분할 횟수 (기본 20)")
    parser.add_argument("--target",   type=float, default=12, help="목표 수익률 % (기본 12)")
    parser.add_argument("--price",    type=float, default=0,  help="현재가 (0 = 자동 조회)")
    parser.add_argument("--prev",     type=float, default=0,  help="전일 종가 (0 = 자동 조회)")
    parser.add_argument("--no-turbo", action="store_true",    help="가속매수 제외")

    args = parser.parse_args()

    # 필수 인자가 없으면 대화형 모드
    if not args.ticker or args.avg == 0 or args.qty == 0 or args.seed == 0:
        _interactive()
    else:
        _cli(args)
