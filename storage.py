"""
storage.py — 계좌별 로컬 파일 관리
모든 함수가 Account 객체를 받아 계좌별 파일을 분리해서 읽고 씁니다.
"""

import os
import json
import datetime
import pytz

import config as cfg
import kis_api
from config import Account, setup_logging

log = setup_logging()


def get_us_today() -> str:
    return datetime.datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d")


# ============================================================
#  매매 잠금
# ============================================================

def is_locked(acc: Account, ticker: str) -> bool:
    if not os.path.exists(acc.trade_lock_file):
        return False
    try:
        return json.load(open(acc.trade_lock_file)).get(ticker) == get_us_today()
    except Exception:
        return False


def set_lock(acc: Account, ticker: str):
    locks = {}
    if os.path.exists(acc.trade_lock_file):
        try:
            locks = json.load(open(acc.trade_lock_file))
        except Exception:
            pass
    locks[ticker] = get_us_today()
    json.dump(locks, open(acc.trade_lock_file, "w"))


def reset_lock(acc: Account):
    if os.path.exists(acc.trade_lock_file):
        os.remove(acc.trade_lock_file)
        log.info(f"[잠금][{acc.name}] 잠금 해제 완료")
    else:
        log.info(f"[잠금][{acc.name}] 잠금 없음")


# ============================================================
#  누적 수익
# ============================================================

def load_cumul(acc: Account) -> dict:
    if os.path.exists(acc.cumul_profit_file):
        try:
            data = json.load(open(acc.cumul_profit_file, encoding="utf-8"))
            return {
                "SOXL":             float(data.get("SOXL", 0.0)),
                "TQQQ":             float(data.get("TQQQ", 0.0)),
                "history":          data.get("history", []),
                "processed_orders": data.get("processed_orders", []),
            }
        except Exception:
            pass
    return {"SOXL": 0.0, "TQQQ": 0.0, "history": [], "processed_orders": []}


def save_cumul(acc: Account, data: dict):
    json.dump(data, open(acc.cumul_profit_file, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def add_profit(acc: Account, ticker: str, profit: float, sell_qty: int,
               sell_price: float, avg_price: float, order_no: str = "") -> float:
    if sell_qty <= 0 or avg_price <= 0:
        return load_cumul(acc).get(ticker, 0.0)
    data      = load_cumul(acc)
    processed = data.get("processed_orders", [])
    if order_no and order_no in processed:
        return data.get(ticker, 0.0)
    data[ticker] = data.get(ticker, 0.0) + profit
    kst_now = datetime.datetime.now(pytz.timezone("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
    data["history"].append({
        "date": kst_now, "ticker": ticker,
        "profit": round(profit, 2), "sell_qty": sell_qty,
        "sell_price": round(sell_price, 2), "avg_price": round(avg_price, 2),
        "cumul_total": round(data[ticker], 2), "order_no": order_no,
    })
    if order_no:
        processed.append(order_no)
        data["processed_orders"] = processed[-200:]
    data["history"] = data["history"][-100:]
    save_cumul(acc, data)
    return data[ticker]


# ============================================================
#  계좌 동기화
# ============================================================

def sync_account(acc: Account) -> tuple:
    """KIS 잔고를 읽어 acc.strategy / acc.current_cash 업데이트."""
    try:
        res = kis_api.query_balance_raw(acc)

        for ticker in acc.strategy:
            acc.strategy[ticker]["data"] = {"avg": 0.0, "qty": 0.0, "cumul": 0.0}

        if res.get("rt_cd") == "0":
            for item in res.get("output1", []):
                ticker = item["ovrs_pdno"]
                if ticker in acc.strategy:
                    avg = float(item["pchs_avg_pric"])
                    qty = float(item["ovrs_cblc_qty"])
                    acc.strategy[ticker]["data"] = {"avg": avg, "qty": qty, "cumul": avg * qty}

        cash, source = kis_api.query_available_cash(acc)
        acc.current_cash = cash
        log.info(f"[계좌동기화][{acc.name}] 완료 | CANO: {acc.cano} | 잔고: ${cash:,.2f} ({source})")
        return True, f"동기화 성공 (잔고: ${cash:,.2f})"
    except Exception as e:
        log.error(f"[계좌동기화][{acc.name}] 실패: {e}")
        return False, str(e)

# ============================================================
#  BIL 매도 플래그 (정규장 더블체크 트리거)
# ============================================================

def set_bil_sold_today(acc: Account, planned_orders: list):
    """
    BIL 매도 주문 발생을 기록하고, 오늘 넣어야 할 주문 목록을 함께 저장.

    planned_orders 형식:
      [{"ticker": "SOXL", "side": "BUY", "qty": 5, "price": 65.86, "order_type": "LOC"}, ...]
    더블체크 시 이 목록을 실제 체결/미체결과 대조하여 누락된 주문만 재접수.
    """
    data = {"date": get_us_today(), "planned": planned_orders}
    json.dump(data, open(acc.bil_sold_file, "w", encoding="utf-8"), ensure_ascii=False)
    log.info(f"[BIL플래그][{acc.name}] 매도 기록 완료 | 계획 주문 {len(planned_orders)}건")


def get_bil_sold_today(acc: Account) -> tuple[bool, list]:
    """
    오늘 BIL 매도가 있었으면 (True, planned_orders) 반환.
    없으면 (False, []) 반환.
    """
    if not os.path.exists(acc.bil_sold_file):
        return False, []
    try:
        data = json.load(open(acc.bil_sold_file, encoding="utf-8"))
        if data.get("date") != get_us_today():
            return False, []
        return True, data.get("planned", [])
    except Exception:
        return False, []


def clear_bil_sold(acc: Account):
    """BIL 매도 플래그 삭제."""
    if os.path.exists(acc.bil_sold_file):
        os.remove(acc.bil_sold_file)
