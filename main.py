"""
main.py — 자동매매 실행 진입점

사용법:
  python main.py                   # 스케줄러만 (이메일 리포트)
  python main.py --telegram        # 스케줄러 + 텔레그램 봇 동시 실행
  python main.py status [계좌명]   # 현황 출력
  python main.py report [계좌명]   # 수동 리포트 메일 발송
  python main.py history [n]       # 수익 내역
  python main.py unlock [계좌명]   # 매매 잠금 해제
  python main.py accounts          # 등록 계좌 목록
"""

import sys
import threading
import logging
import datetime
import schedule
import time
import pytz

import config as cfg
import kis_api
import storage
import reporter
import jobs
from config import Account, setup_logging

log = setup_logging()


# ============================================================
#  운영 편의 함수
# ============================================================

def print_status(acc: Account):
    storage.sync_account(acc)
    kst = pytz.timezone("Asia/Seoul")
    print(f"\n{'='*62}")
    print(f"  [{acc.name}] CANO: {acc.cano} — {datetime.datetime.now(kst).strftime('%Y-%m-%d %H:%M')} KST")
    print(f"{'='*62}")

    total_inv = total_val = 0.0
    for ticker in acc.strategy:
        s     = acc.strategy[ticker]
        d     = s["data"]
        avg_p = d.get("avg", 0.0); qty_t = int(d.get("qty", 0))
        ota   = s["seed"] / s["total_a"]
        t_val = round(d.get("cumul", 0) / ota, 2) if ota > 0 and d.get("cumul", 0) > 0 else 0.0
        star_pct = round(s["target_profit"] * (1.0 - 2.0 * t_val / s["total_a"]), 2) if t_val > 0 else s["target_profit"]
        try:
            curr_p = kis_api.get_current_price(acc, ticker) if qty_t > 0 else 0.0
        except Exception:
            curr_p = 0.0
        target_p   = avg_p * (1 + s["target_profit"] / 100) if avg_p > 0 else 0.0
        pnl        = (curr_p - avg_p) * qty_t if avg_p > 0 else 0.0
        pnl_pct    = (curr_p - avg_p) / avg_p * 100 if avg_p > 0 else 0.0
        curr_value = curr_p * qty_t
        total_inv += avg_p * qty_t
        total_val += curr_value if curr_value > 0 else avg_p * qty_t
        half_label = "전반전" if t_val <= s["total_a"] / 2 else "후반전"
        print(f"\n  [{ticker}] 목표 {s['target_profit']}%  T={t_val:.1f}/{int(s['total_a'])} ({half_label})  별% {star_pct:.2f}%")
        if qty_t > 0:
            print(f"    보유: {qty_t}주  평단: ${avg_p:.4f}  현재: ${curr_p:.4f}")
            print(f"    손익: ${pnl:+,.2f} ({pnl_pct:+.2f}%)  목표가: ${target_p:.4f}")
        else:
            print("    보유 없음")

    print(f"\n{'─'*62}")
    total_pnl = total_val - total_inv
    print(f"  투자금: ${total_inv:,.2f}  평가금: ${total_val:,.2f}  손익: ${total_pnl:+,.2f}")
    print(f"  보유달러: ${acc.current_cash:,.2f}")
    cp = storage.load_cumul(acc)
    ctot = cp.get("SOXL", 0.0) + cp.get("TQQQ", 0.0)
    print(f"  누적수익: SOXL ${cp.get('SOXL',0):+,.2f}  TQQQ ${cp.get('TQQQ',0):+,.2f}  합계 ${ctot:+,.2f}")
    print(f"{'='*62}\n")


def print_profit_history(acc: Account, n: int = 20):
    cp      = storage.load_cumul(acc)
    history = cp.get("history", [])[-n:]
    print(f"\n[{acc.name}] 최근 수익 내역 ({len(history)}건)")
    for h in reversed(history):
        print(f"  {h['date']}  {h['ticker']}  매도 {h['sell_qty']}주 @ ${h['sell_price']:.2f}"
              f"  수익 ${h['profit']:+,.2f}  누적 ${h['cumul_total']:,.2f}")


def _find_account(name: str) -> Account:
    for acc in cfg.ACCOUNTS:
        if acc.name.upper() == name.upper():
            return acc
    raise ValueError(f"계좌 '{name}' 없음. 가능한 계좌: {[a.name for a in cfg.ACCOUNTS]}")


def _target_accounts(name: str) -> list:
    if name:
        return [_find_account(name)]
    return cfg.ACCOUNTS


# ============================================================
#  스케줄러 (이메일 전용 — 텔레그램 없이 실행 시)
# ============================================================

def register_schedules():
    schedule.every().day.at("00:10").do(jobs.job_doublecheck)
    schedule.every().day.at("18:25").do(jobs.job_bil_manage)
    schedule.every().day.at("18:30").do(jobs.job_daily_trade)
    schedule.every().day.at("08:00").do(jobs.job_settlement_check)
    schedule.every(6).hours.do(jobs.job_token_renewal)
    schedule.every(5).minutes.do(jobs.job_premarket_check)
    names = [a.name for a in cfg.ACCOUNTS]
    log.info(f"[스케줄러] 계좌 {len(cfg.ACCOUNTS)}개: {names}")


# ============================================================
#  메인
# ============================================================

def main(with_telegram: bool = False):
    logging.getLogger().setLevel(logging.DEBUG)
    log.info("=" * 62)
    log.info(f"  무한매수법칙 자동매매 — 계좌 {len(cfg.ACCOUNTS)}개"
             + ("  (텔레그램 봇 포함)" if with_telegram else ""))
    log.info("=" * 62)

    failed = []
    for acc in cfg.ACCOUNTS:
        log.info(f"  [{acc.name}] CANO: {acc.cano}  KEY: {acc.app_key[:6]}{'*'*10}")
        if not kis_api.get_token(acc):
            log.error(f"  ❌ [{acc.name}] 토큰 발급 실패")
            failed.append(acc.name)

    if len(failed) == len(cfg.ACCOUNTS):
        log.error("모든 계좌 토큰 실패 — 종료")
        return

    logging.getLogger().setLevel(logging.INFO)

    for acc in cfg.ACCOUNTS:
        if acc.name not in failed:
            storage.sync_account(acc)
            print_status(acc)

    if with_telegram:
        import telegram_bot

        # schedule 루프를 데몬 스레드에서 실행
        # (봇의 job_queue와 중복되지 않도록 register_schedules는 호출하지 않음)
        def _schedule_loop():
            # 텔레그램 봇이 없을 때를 위한 fallback 스케줄 루프
            # --telegram 모드에서는 봇 job_queue가 스케줄을 담당하므로
            # 여기서는 토큰 갱신만 담당 (봇 없이도 토큰 만료 방지)
            schedule.every(6).hours.do(jobs.job_token_renewal)
            log.info("[스케줄] 토큰 갱신 루프 시작 (데몬 스레드)")
            while True:
                schedule.run_pending()
                time.sleep(30)

        sched_thread = threading.Thread(target=_schedule_loop, daemon=True, name="scheduler")
        sched_thread.start()

        # 봇은 메인 스레드에서 실행 (시그널 핸들러 요구사항)
        log.info("[텔레그램] 봇을 메인 스레드에서 실행합니다...")
        telegram_bot.run_bot()   # blocking — Ctrl+C 로 종료
    else:
        register_schedules()
        log.info("스케줄러 실행 중... (종료: Ctrl+C)")
        while True:
            schedule.run_pending()
            time.sleep(10)


# ============================================================
#  CLI
# ============================================================

if __name__ == "__main__":
    args = sys.argv[1:]

    # --telegram 플래그 분리
    with_tg = "--telegram" in args
    args    = [a for a in args if a != "--telegram"]

    cmd      = args[0] if args else ""
    acc_name = args[1] if len(args) > 1 else ""

    if cmd == "status":
        for acc in _target_accounts(acc_name):
            kis_api.get_token(acc)
            print_status(acc)

    elif cmd == "report":
        for acc in _target_accounts(acc_name):
            kis_api.get_token(acc)
            storage.sync_account(acc)
            reporter.send_report(acc, trigger="manual")
            log.info(f"[{acc.name}] 수동 리포트 발송 완료")

    elif cmd == "history":
        n = int(args[2]) if len(args) > 2 else 20
        for acc in _target_accounts(acc_name):
            print_profit_history(acc, n)

    elif cmd == "unlock":
        for acc in _target_accounts(acc_name):
            storage.reset_lock(acc)

    elif cmd == "accounts":
        print(f"\n등록된 계좌 ({len(cfg.ACCOUNTS)}개):")
        for acc in cfg.ACCOUNTS:
            print(f"  [{acc.name}]  CANO: {acc.cano}")
            for t, sc in acc.strategy_config.items():
                print(f"    {t}: 시드 ${sc['seed']:,.0f}  분할 {int(sc['total_a'])}회  목표 {sc['target_profit']}%")

    else:
        main(with_telegram=with_tg)
