"""
kis_api.py — KIS OpenAPI 래퍼 (계좌별 독립 동작)
모든 함수가 Account 객체를 첫 번째 인자로 받습니다.
"""

import json
import math
import time
import datetime
import requests
import yfinance as yf
import pytz

import config as cfg
from config import Account, setup_logging

log = setup_logging()


# ============================================================
#  토큰 관리 (계좌별)
# ============================================================

_last_renewal: dict = {}   # {cano: datetime}


def get_token(acc: Account, force_renew: bool = False) -> str:
    if not force_renew and _token_valid(acc):
        saved = json.load(open(acc.token_file, "r", encoding="utf-8"))
        return saved["token"]

    if force_renew and acc.cano in _last_renewal:
        elapsed = (datetime.datetime.now() - _last_renewal[acc.cano]).total_seconds()
        if elapsed < 300:
            try:
                return json.load(open(acc.token_file))["token"]
            except Exception:
                pass

    return _issue_new_token(acc)


def _token_valid(acc: Account) -> bool:
    import os
    if not os.path.exists(acc.token_file):
        return False
    try:
        saved  = json.load(open(acc.token_file, "r", encoding="utf-8"))
        expire = datetime.datetime.strptime(saved["expire"], "%Y-%m-%d %H:%M:%S")
        if expire > datetime.datetime.now() + datetime.timedelta(hours=1):
            return True
        log.info(f"[토큰][{acc.name}] 캐시 만료 ({saved['expire']}) — 재발급")
    except Exception as e:
        log.warning(f"[토큰][{acc.name}] 읽기 실패 ({e}) — 재발급")
    return False


def _issue_new_token(acc: Account) -> str:
    url  = f"{cfg.URL_BASE}/oauth2/tokenP"
    body = {"grant_type": "client_credentials",
            "appkey": acc.app_key, "appsecret": acc.app_secret}
    try:
        resp     = requests.post(url, headers={"content-type": "application/json"},
                                 data=json.dumps(body), timeout=15)
        res_data = resp.json()
        token    = res_data.get("access_token")
        if token:
            _last_renewal[acc.cano] = datetime.datetime.now()
            expire_str = res_data.get("access_token_token_expired") or \
                         (datetime.datetime.now() + datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            json.dump({"token": token, "expire": expire_str},
                      open(acc.token_file, "w", encoding="utf-8"))
            log.info(f"[토큰][{acc.name}] 발급 완료, 만료: {expire_str}")
            return token
        log.error(f"[토큰][{acc.name}] 발급 실패 — {resp.status_code} | {res_data}")
        _print_token_hint(acc, res_data)
    except requests.exceptions.ConnectionError as e:
        log.error(f"[토큰][{acc.name}] 네트워크 실패: {e}")
    except requests.exceptions.Timeout:
        log.error(f"[토큰][{acc.name}] 타임아웃")
    except Exception as e:
        log.error(f"[토큰][{acc.name}] 예외: {type(e).__name__}: {e}")
    return ""


def _print_token_hint(acc: Account, res_data: dict):
    msg  = res_data.get("msg1", "") or res_data.get("message", "") or str(res_data)
    code = res_data.get("rt_cd", "") or res_data.get("error_code", "")
    log.error(f"[토큰][{acc.name}] 코드: {code} | {msg}")
    hints = {
        "유효하지 않은 appkey":    "APP_KEY 재확인",
        "유효하지 않은 appsecret": "APP_SECRET 재확인",
        "접근토큰 발급 잠김":       "당일 발급 한도 초과. 내일 재시도.",
        "모의투자":                 "모의투자 URL: https://openapivts.koreainvestment.com:29443",
        "already":                 f"{acc.token_file} 삭제 후 재시도.",
    }
    for kw, hint in hints.items():
        if kw in msg:
            log.error(f"  💡 [{acc.name}] {hint}")
            return


def renew_token_if_needed(acc: Account):
    import os
    need = True
    if os.path.exists(acc.token_file):
        try:
            saved  = json.load(open(acc.token_file))
            expire = datetime.datetime.strptime(saved["expire"], "%Y-%m-%d %H:%M:%S")
            if (expire - datetime.datetime.now()).total_seconds() / 3600 >= 2:
                need = False
        except Exception:
            pass
    if need:
        token = get_token(acc, force_renew=True)
        log.info(f"[토큰][{acc.name}] {'갱신 완료' if token else '갱신 실패'}")


# ============================================================
#  공통 호출 래퍼
# ============================================================

def call(acc: Account, method: str, url: str, headers: dict,
         params: dict = None, data: dict = None) -> dict:
    def _req():
        if method == "GET":
            return requests.get(url, headers=headers, params=params).json()
        return requests.post(url, headers=headers,
                             data=json.dumps(data) if data else None).json()
    res = _req()
    if res and "만료된 token" in res.get("msg1", ""):
        log.warning(f"[API][{acc.name}] 토큰 만료 — 재발급 후 재시도")
        new_tok = get_token(acc, force_renew=True)
        if new_tok:
            headers = {**headers, "authorization": f"Bearer {new_tok}"}
            res = _req()
    return res


def _base_headers(acc: Account, tr_id: str) -> dict:
    return {
        "Content-Type": "application/json",
        "authorization": f"Bearer {get_token(acc)}",
        "appkey":    acc.app_key,
        "appsecret": acc.app_secret,
        "tr_id":     tr_id,
        "custtype":  "P",
    }


# ============================================================
#  계좌 조회
# ============================================================

def query_balance_raw(acc: Account) -> dict:
    return call(acc, "GET",
        url=f"{cfg.URL_BASE}/uapi/overseas-stock/v1/trading/inquire-balance",
        headers=_base_headers(acc, "JTTT3012R"),
        params={
            "CANO": acc.cano, "ACNT_PRDT_CD": acc.acnt_prdt_cd,
            "OVRS_EXCG_CD": "NASD", "TR_CRCY_CD": "USD",
            "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
        })


def query_available_cash(acc: Account) -> tuple:
    attempts = [
        {"name": "psamount-JTTT", "tr_id": "JTTT3007R",
         "url":  f"{cfg.URL_BASE}/uapi/overseas-stock/v1/trading/inquire-psamount",
         "params": {"CANO": acc.cano, "ACNT_PRDT_CD": acc.acnt_prdt_cd,
                    "OVRS_EXCG_CD": "NASD", "OVRS_ORD_UNPR": "10", "ITEM_CD": "SOXL"},
         "field": "output.ord_psbl_frcr_amt"},
        {"name": "psamount-TTTS", "tr_id": "TTTS3007R",
         "url":  f"{cfg.URL_BASE}/uapi/overseas-stock/v1/trading/inquire-psamount",
         "params": {"CANO": acc.cano, "ACNT_PRDT_CD": acc.acnt_prdt_cd,
                    "OVRS_EXCG_CD": "NASD", "OVRS_ORD_UNPR": "10", "ITEM_CD": "SOXL"},
         "field": "output.ord_psbl_frcr_amt"},
        {"name": "present-CTRP", "tr_id": "CTRP6504R",
         "url":  f"{cfg.URL_BASE}/uapi/overseas-stock/v1/trading/inquire-present-balance",
         "params": {"CANO": acc.cano, "ACNT_PRDT_CD": acc.acnt_prdt_cd,
                    "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "840",
                    "TR_MKET_CD": "00", "INQR_DVSN_CD": "00"},
         "field": "scan_output2"},
    ]
    base_h = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {get_token(acc)}",
        "appkey": acc.app_key, "appsecret": acc.app_secret, "custtype": "P",
    }
    for a in attempts:
        try:
            res = call(acc, "GET", a["url"], {**base_h, "tr_id": a["tr_id"]}, params=a["params"])
            if not res or res.get("rt_cd") != "0":
                continue
            if a["field"] == "scan_output2":
                o2  = res.get("output2", {})
                if isinstance(o2, list) and o2:
                    o2 = o2[0]
                val = float(o2.get("frcr_drwg_psbl_amt_1", 0)) if isinstance(o2, dict) else 0
            else:
                parts = a["field"].split(".")
                val   = float(res.get(parts[0], {}).get(parts[1], 0))
            if val > 0:
                return val, a["name"]
        except Exception as e:
            log.debug(f"[잔고][{acc.name}] {a['name']} 실패: {e}")
    return 0.0, "조회실패"


def query_filled_orders(acc: Account) -> dict:
    kst     = pytz.timezone("Asia/Seoul")
    today   = datetime.datetime.now(kst).strftime("%Y%m%d")
    two_ago = (datetime.datetime.now(kst) - datetime.timedelta(days=2)).strftime("%Y%m%d")
    all_orders = []
    try:
        for excg in ["NASD", "NYSE", "AMEX"]:
            res = call(acc, "GET",
                f"{cfg.URL_BASE}/uapi/overseas-stock/v1/trading/inquire-ccnl",
                _base_headers(acc, "JTTT3001R"),
                params={
                    "CANO": acc.cano, "ACNT_PRDT_CD": acc.acnt_prdt_cd,
                    "PDNO": "", "ORD_STRT_DT": two_ago, "ORD_END_DT": today,
                    "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "01",
                    "OVRS_EXCG_CD": excg, "SORT_SQN": "DS",
                    "ORD_GNO_BRNO": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
                })
            if res and res.get("rt_cd") == "0":
                all_orders.extend(res.get("output", []))
    except Exception as e:
        return {"rt_cd": "-1", "msg1": str(e)}
    return {"rt_cd": "0", "output": all_orders}


# ============================================================
#  주문
# ============================================================

def send_order(acc: Account, ticker: str, qty: int, price: float,
               side: str = "BUY", order_type: str = "00",
               max_retries: int = 2) -> dict:
    type_map    = {"LOC": "34", "LOO": "02", "MOC": "33", "MOO": "31"}
    actual_type = type_map.get(order_type, order_type)
    tr_id       = "TTTT1002U" if side == "BUY" else "TTTT1006U"
    res         = {}
    for attempt in range(max_retries + 1):
        try:
            body = {
                "CANO": acc.cano, "ACNT_PRDT_CD": acc.acnt_prdt_cd,
                "OVRS_EXCG_CD": cfg.EXCHANGE_CODE.get(ticker, "NASD"),
                "PDNO": ticker,
                "ORD_QTY": str(int(qty)),
                "OVRS_ORD_UNPR": f"{price:.2f}",
                "CTAC_TLNO": "", "MGCO_APTM_ODNO": "",
                "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": actual_type,
            }
            res = requests.post(
                f"{cfg.URL_BASE}/uapi/overseas-stock/v1/trading/order",
                headers=_base_headers(acc, tr_id), data=json.dumps(body),
            ).json()
            if res.get("rt_cd") == "0":
                return res
            if attempt < max_retries:
                time.sleep(1)
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1)
                continue
            return {"rt_cd": "-1", "msg1": f"통신오류: {e}"}
    return res


def check_order_filled(acc: Account, order_no: str, ticker: str,
                       side: str = "SELL", max_checks: int = 10, interval: int = 3) -> tuple:
    kst = pytz.timezone("Asia/Seoul")
    for i in range(max_checks):
        try:
            today   = datetime.datetime.now(kst).strftime("%Y%m%d")
            two_ago = (datetime.datetime.now(kst) - datetime.timedelta(days=2)).strftime("%Y%m%d")
            res = call(acc, "GET",
                f"{cfg.URL_BASE}/uapi/overseas-stock/v1/trading/inquire-ccnl",
                _base_headers(acc, "JTTT3001R"),
                params={
                    "CANO": acc.cano, "ACNT_PRDT_CD": acc.acnt_prdt_cd,
                    "PDNO": ticker, "ORD_STRT_DT": two_ago, "ORD_END_DT": today,
                    "SLL_BUY_DVSN": "01" if side == "SELL" else "02",
                    "CCLD_NCCS_DVSN": "01",
                    "OVRS_EXCG_CD": cfg.EXCHANGE_CODE.get(ticker, "NASD"),
                    "SORT_SQN": "DS", "ORD_GNO_BRNO": "",
                    "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
                })
            if res and res.get("rt_cd") == "0":
                for o in res.get("output", []):
                    if o.get("odno") == order_no or o.get("orgn_odno") == order_no:
                        fq = int(float(o.get("ft_ccld_qty", "0")))
                        if fq > 0:
                            fp = float(o.get("ft_ccld_unpr3", o.get("ft_ccld_unpr", "0")))
                            return True, fq, fp
        except Exception as e:
            log.debug(f"[체결확인][{acc.name}] 오류: {e}")
        if i < max_checks - 1:
            time.sleep(interval)
    return False, 0, 0.0


# ============================================================
#  시세 (계좌 무관 — 공용)
# ============================================================

def get_market_session() -> str:
    import pandas_market_calendars as mcal
    est = pytz.timezone("US/Eastern")
    now = datetime.datetime.now(est)
    h, m, wd = now.hour, now.minute, now.weekday()
    if wd >= 5:
        return "closed"
    if (h == 9 and m >= 30) or (10 <= h < 16):
        return "regular"
    if 16 <= h < 20:
        return "aftermarket"
    if h >= 4 and (h < 9 or (h == 9 and m < 30)):
        return "premarket"
    return "closed"


def is_market_open_today() -> bool:
    import pandas_market_calendars as mcal
    est   = pytz.timezone("US/Eastern")
    today = datetime.datetime.now(est).date()
    nyse  = mcal.get_calendar("NYSE")
    return not nyse.schedule(start_date=today, end_date=today).empty


def get_current_price(acc: Account, ticker: str) -> float:
    if get_market_session() == "regular":
        try:
            res = call(acc, "GET",
                f"{cfg.URL_BASE}/uapi/overseas-price/v1/quotations/price",
                _base_headers(acc, "HHDFS76200200"),
                params={"AUTH": "", "EXCD": cfg.PRICE_EXCHANGE_CODE.get(ticker, "NAS"), "SYMB": ticker})
            if res and res.get("rt_cd") == "0":
                price = float(res.get("output", {}).get("last", 0))
                if price > 0:
                    return price
        except Exception:
            pass
    try:
        return yf.Ticker(ticker).fast_info["last_price"]
    except Exception:
        return 0.0


def get_prev_close(acc: Account, ticker: str) -> float:
    try:
        res = call(acc, "GET",
            f"{cfg.URL_BASE}/uapi/overseas-price/v1/quotations/price",
            _base_headers(acc, "HHDFS76200200"),
            params={"AUTH": "", "EXCD": cfg.PRICE_EXCHANGE_CODE.get(ticker, "NAS"), "SYMB": ticker})
        if res and res.get("rt_cd") == "0":
            base = float(res.get("output", {}).get("base", 0))
            if base > 0:
                return base
    except Exception:
        pass
    hist = yf.Ticker(ticker).history(period="5d")
    return float(hist["Close"].iloc[-2] if len(hist) >= 2 else hist["Close"].iloc[-1])


# ============================================================
#  BIL 잔고 조회 (국내 해외주식 잔고 API 재활용)
# ============================================================

def get_bil_balance(acc: Account) -> tuple[int, float]:
    """
    BIL 보유 수량과 현재가를 반환합니다.
    반환: (보유수량, 현재가)  — 미보유 시 (0, 0.0)
    """
    try:
        res = call(acc, "GET",
            f"{cfg.URL_BASE}/uapi/overseas-stock/v1/trading/inquire-balance",
            _base_headers(acc, "JTTT3012R"),
            params={
                "CANO": acc.cano, "ACNT_PRDT_CD": acc.acnt_prdt_cd,
                "OVRS_EXCG_CD": "NYSE", "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
            })
        if res and res.get("rt_cd") == "0":
            for item in res.get("output1", []):
                if item.get("ovrs_pdno", "").upper() == "BIL":
                    qty   = int(float(item.get("ovrs_cblc_qty", "0")))
                    price = float(item.get("now_pric2", item.get("ovrs_now_pric1", "0")))
                    if price <= 0:
                        price = get_current_price(acc, "BIL")
                    return qty, price
    except Exception as e:
        log.debug(f"[BIL잔고][{acc.name}] 오류: {e}")
    return 0, 0.0
