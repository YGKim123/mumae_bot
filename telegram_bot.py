"""
telegram_bot.py — 텔레그램 봇 인터페이스
=========================================

명령어:
  /start            봇 시작 & 스케줄 등록
  /status [계좌명]  실시간 시세 + 현황
  /sync   [계좌명]  수동 주문 지시서 작성 (인라인 버튼으로 실행)
  /cash   [계좌명]  예수금 + 보유 종목 조회
  /report [계좌명]  포트폴리오 수익률 현황
  /reset  [계좌명]  매매 잠금 해제
  /profit_history [계좌명]  누적 수익 내역
  /profit_reset   [계좌명]  누적 수익 기록 초기화

멀티 계좌: /sync ACC1  처럼 계좌명을 붙이면 해당 계좌만 처리
           계좌명 생략 시 전체 계좌 처리

실행:
  python telegram_bot.py        # 봇만 실행 (자체 스케줄 포함)
  또는 main.py 실행 시 --telegram 플래그 추가 예정
"""

import os
import math
import json
import datetime
import asyncio
import threading
import requests as _requests

import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, CallbackQueryHandler,
    MessageHandler, filters,
    ContextTypes,
)

import config as cfg
import kis_api
import storage
import strategy
import reporter
import jobs
from config import Account, setup_logging

log = setup_logging()

# ── 봇 설정 (.env) ────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")   # 빈 문자열 = 누구나 허용
CHAT_ID_FILE    = "chat_id.dat"


# ============================================================
#  유틸
# ============================================================

def _save_chat_id(chat_id: int):
    with open(CHAT_ID_FILE, "w") as f:
        f.write(str(chat_id))


def _load_chat_id() -> int | None:
    if os.path.exists(CHAT_ID_FILE):
        try:
            return int(open(CHAT_ID_FILE).read().strip())
        except Exception:
            pass
    return None


def _is_allowed(update: Update) -> bool:
    """TELEGRAM_CHAT_ID 설정 시 해당 chat_id만 허용."""
    if not ALLOWED_CHAT_ID:
        return True
    return str(update.effective_chat.id) == ALLOWED_CHAT_ID


def _get_account(arg: str) -> Account | None:
    """인자로 받은 계좌명 → Account 반환. 없거나 빈 문자열이면 None."""
    if not arg:
        return None
    for acc in cfg.ACCOUNTS:
        if acc.name.upper() == arg.upper():
            return acc
    return None


def _target_accounts(args: list) -> list[Account]:
    """
    커맨드 인자에서 계좌명 파싱.
    /cmd ACC1 → [ACC1 계좌]
    /cmd      → 전체 ACCOUNTS
    """
    name = args[0].upper() if args else ""
    acc  = _get_account(name)
    return [acc] if acc else cfg.ACCOUNTS


def _acc_label(acc: Account) -> str:
    return f"[{acc.name}]" if len(cfg.ACCOUNTS) > 1 else ""


def _market_session_kr() -> str:
    s = kis_api.get_market_session()
    return {"regular": "정규장", "premarket": "프리마켓",
            "aftermarket": "시간외", "closed": "휴장"}.get(s, s)


# ============================================================
#  /start
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    chat_id = update.effective_chat.id
    _save_chat_id(chat_id)
    _register_bot_schedules(context.job_queue, chat_id)

    acc_list = "\n".join(
        f"  • [{a.name}] CANO: {a.cano}  SOXL {a.strategy_config['SOXL']['target_profit']}% / TQQQ {a.strategy_config['TQQQ']['target_profit']}%"
        for a in cfg.ACCOUNTS
    )
    multi_note = ""
    if len(cfg.ACCOUNTS) > 1:
        multi_note = "\n멀티 계좌: 명령어 뒤에 계좌명을 붙이세요 (예: /sync ACC1)\n계좌명 생략 시 전체 계좌에 적용됩니다.\n"

    msg = (
        "🤖 무한매수법칙 자동매매봇 가동!\n\n"
        f"등록된 계좌:\n{acc_list}\n"
        f"{multi_note}\n"
        "📅 스케줄:\n"
        "  • 매일 00:10 KST — BIL 매도일 주문 더블체크\n"
        "  • 매일 18:00~18:30 KST — 프리마켓 목표가 체크 (5분마다)\n"
        "  • 매일 18:25 KST — BIL 예수금 버퍼 관리\n"
        "  • 매일 18:30 KST — 정규장 자동매매\n"
        "  • 매일 08:00 KST — 전일 체결 확인\n"
        "  • 6시간마다 — 토큰 자동 갱신\n\n"
        "📌 명령어:\n"
        "  /status   — 실시간 시세 + 현황\n"
        "  /preview  — 오늘 주문 상세 미리보기 (주문 안 함)\n"
        "  /sync     — 수동 주문 지시서 + 버튼\n"
        "  /order    — 즉시 주문 실행 (버튼 없이)\n"
        "  /orders   — 미체결 주문 조회\n"
        "  /set      — 전략 설정 변경 (종목/예산/목표수익률/분할횟수)\n"
        "  /settings — 현재 전략 설정 확인\n"
        "  /cash     — 예수금 + 보유 종목\n"
        "  /report   — 포트폴리오 현황\n"
        "  /reset    — 매매 잠금 해제\n"
        "  /profit_history — 수익 내역\n"
        "  /profit_reset   — 수익 기록 초기화"
    )
    await update.message.reply_text(msg)


# ============================================================
#  /status [계좌명]
# ============================================================

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    await update.message.reply_text("📡 실시간 시세 조회 중...")
    accounts = _target_accounts(context.args)

    kst = pytz.timezone("Asia/Seoul")
    now_kst = datetime.datetime.now(kst).strftime("%H:%M:%S")
    market  = _market_session_kr()

    for acc in accounts:
        # 보유 없으면 동기화
        if not any(int(acc.strategy[t]["data"].get("qty", 0)) > 0 for t in acc.strategy):
            storage.sync_account(acc)

        label = _acc_label(acc)
        msg   = f"📊 실시간 시세 현황 {label}\n조회: {now_kst} KST | {market}\n\n"

        for ticker in acc.strategy:
            try:
                curr_p = kis_api.get_current_price(acc, ticker)
                s      = acc.strategy[ticker]
                d      = s["data"]
                avg_p  = d.get("avg", 0)
                qty_t  = int(d.get("qty", 0))

                msg += f"[{ticker}]\n  현재가: ${curr_p:.4f}\n"
                if avg_p > 0 and qty_t > 0:
                    pnl_pct  = (curr_p - avg_p) / avg_p * 100
                    pnl_amt  = (curr_p - avg_p) * qty_t
                    target_p = avg_p * (1 + s["target_profit"] / 100)
                    ota      = s["seed"] / s["total_a"]
                    t_val    = round(d.get("cumul", 0) / ota, 2) if ota > 0 and d.get("cumul", 0) > 0 else 0
                    msg += f"  평단: ${avg_p:.2f} / {qty_t}주\n"
                    msg += f"  손익: ${pnl_amt:,.2f} ({pnl_pct:+.2f}%)\n"
                    msg += f"  목표가: ${target_p:.2f}  T={t_val:.1f}\n"
                else:
                    msg += "  보유 없음\n"
                msg += "\n"
            except Exception as e:
                msg += f"[{ticker}] 조회 실패: {e}\n\n"

        await update.message.reply_text(msg)


# ============================================================
#  /cash [계좌명]
# ============================================================

async def cmd_cash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    await update.message.reply_text("💵 예수금 조회 중...")
    accounts = _target_accounts(context.args)

    for acc in accounts:
        label = _acc_label(acc)
        try:
            # 잔고 조회
            res   = kis_api.query_balance_raw(acc)
            cash, src = kis_api.query_available_cash(acc)
            rt_cd = res.get("rt_cd", "N/A")

            msg  = f"💰 예수금 조회 {label}\n"
            msg += f"주문가능금액: ${cash:,.2f} ({src})\n"

            holdings = res.get("output1", []) if rt_cd == "0" else []
            all_evl  = 0.0
            if holdings:
                msg += "\n[보유 종목]\n"
                for item in holdings:
                    sym    = item.get("ovrs_pdno", "?")
                    qty_f  = float(item.get("ovrs_cblc_qty", "0"))
                    avg_f  = float(item.get("pchs_avg_pric", "0"))
                    try:
                        now_f = kis_api.get_current_price(acc, sym)
                        if now_f <= 0:
                            now_f = float(item.get("now_pric2", item.get("ovrs_now_pric1", "0")))
                    except Exception:
                        now_f = float(item.get("now_pric2", item.get("ovrs_now_pric1", "0")))
                    pnl    = (now_f - avg_f) * qty_f
                    pnl_rt = (now_f - avg_f) / avg_f * 100 if avg_f > 0 else 0
                    all_evl += now_f * qty_f
                    flag    = " ★" if sym in acc.strategy else ""
                    msg += (f"  {sym}{flag}: {qty_f:.0f}주 / 평단 ${avg_f:.2f} / "
                            f"현재 ${now_f:.2f} / 손익 ${pnl:+.2f} ({pnl_rt:+.1f}%)\n")

            msg += f"\n현금: ${cash:,.2f} / 주식: ${all_evl:,.2f}"

            # 잔고가 0이면 디버그 정보 추가
            if cash < 0.01 and all_evl < 0.01:
                msg += (f"\n\n[진단]\n"
                        f"  APP_KEY: {'설정됨' if acc.app_key else '미설정'}\n"
                        f"  CANO: {acc.cano}\n"
                        f"  API rt_cd: {rt_cd}")

            await update.message.reply_text(msg)

        except Exception as e:
            await update.message.reply_text(f"조회 오류 {label}: {e}")


# ============================================================
#  /report [계좌명]
# ============================================================

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    await update.message.reply_text("📈 포트폴리오 현황 조회 중...")
    accounts = _target_accounts(context.args)

    for acc in accounts:
        storage.sync_account(acc)
        label = _acc_label(acc)

        msg = f"📊 포트폴리오 현황 {label}\n\n"
        total_invested = total_value = 0.0

        for ticker in acc.strategy:
            s     = acc.strategy[ticker]
            d     = s["data"]
            avg_p = d.get("avg", 0)
            qty_t = int(d.get("qty", 0))
            ota   = s["seed"] / s["total_a"]
            t_val = round(d.get("cumul", 0) / ota, 2) if ota > 0 and d.get("cumul", 0) > 0 else 0.0
            star_pct = round(s["target_profit"] * (1.0 - 2.0 * t_val / s["total_a"]), 2) if t_val > 0 else s["target_profit"]

            msg += f"[{ticker}] 목표 {s['target_profit']}%\n"
            msg += f"  시드: ${s['seed']:,.0f}  1회분: ${ota:,.0f}\n"

            if qty_t == 0:
                msg += "  보유 없음 (T=0)\n\n"
                continue

            invested = avg_p * qty_t
            total_invested += invested
            try:
                curr_p    = kis_api.get_current_price(acc, ticker)
                curr_val  = curr_p * qty_t
                total_value += curr_val
                pnl       = curr_val - invested
                pnl_pct   = (curr_p - avg_p) / avg_p * 100
                target_p  = avg_p * (1 + s["target_profit"] / 100)
                to_target = (target_p - curr_p) / curr_p * 100
                half_lbl  = "전반전" if t_val <= s["total_a"] / 2 else "후반전"
                remaining = s["total_a"] - t_val
                msg += (f"  평단: ${avg_p:.2f} / 현재: ${curr_p:.2f}\n"
                        f"  수량: {qty_t}주  투자금: ${invested:,.2f}\n"
                        f"  평가금: ${curr_val:,.2f}\n"
                        f"  손익: ${pnl:,.2f} ({pnl_pct:+.2f}%)\n"
                        f"  T값: {t_val} ({half_lbl})  별%: {star_pct:.2f}%\n"
                        f"  목표가: ${target_p:.2f} ({to_target:+.1f}% 필요)\n"
                        f"  남은 회차: {remaining:.1f}회\n\n")
            except Exception:
                total_value += invested
                half_lbl = "전반전" if t_val <= s["total_a"] / 2 else "후반전"
                msg += (f"  평단: ${avg_p:.2f}  수량: {qty_t}주\n"
                        f"  T값: {t_val} ({half_lbl})  별%: {star_pct:.2f}%\n"
                        f"  현재가 조회 실패\n\n")

        total_pnl     = total_value - total_invested
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0
        msg += (f"══════════════════\n"
                f"총 투자금: ${total_invested:,.2f}\n"
                f"총 평가금: ${total_value:,.2f}\n"
                f"총 손익:   ${total_pnl:,.2f} ({total_pnl_pct:+.2f}%)\n"
                f"보유 달러: ${acc.current_cash:,.2f}\n")

        cp    = storage.load_cumul(acc)
        ctot  = cp.get("SOXL", 0.0) + cp.get("TQQQ", 0.0)
        if ctot != 0:
            msg += "\n[실현 누적수익]\n"
            for t in acc.strategy:
                cv = cp.get(t, 0.0)
                if cv != 0:
                    msg += f"  {t}: ${cv:,.2f}\n"
            msg += f"  합계: ${ctot:,.2f}\n"
            hist = cp.get("history", [])
            if hist:
                last = hist[-1]
                msg += f"  최근: {last['date']} {last['ticker']} ${last['profit']:+,.2f}\n"
        msg += "══════════════════"

        await update.message.reply_text(msg)

        # 이메일 리포트도 함께 발송
        reporter.send_report(acc, trigger="manual")


# ============================================================
#  /sync [계좌명]  — 수동 주문 지시서 + 인라인 버튼
# ============================================================

async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    accounts = _target_accounts(context.args)

    for acc in accounts:
        await _send_sync(update, acc)


async def _send_sync(update: Update, acc: Account):
    label = _acc_label(acc)

    # 잠금 확인
    locked = [t for t in acc.strategy if storage.is_locked(acc, t)]
    if len(locked) == len(acc.strategy):
        msg = (f"🔒 [안전장치] {label} 모든 종목이 오늘 이미 처리됐습니다.\n"
               f"잠금 종목: {', '.join(locked)}\n"
               f"/reset {acc.name} 으로 해제할 수 있습니다.")
        await update.message.reply_text(msg)
        return

    if locked:
        await update.message.reply_text(
            f"⚠️ {label} 잠금 종목: {', '.join(locked)} (오늘 매매 완료)\n"
            f"나머지 종목만 처리합니다.")

    await update.message.reply_text(f"🔄 {label} 계좌 동기화 중...")
    ok, res_msg = storage.sync_account(acc)
    if not ok:
        await update.message.reply_text(f"❌ 동기화 실패 {label}: {res_msg}")
        return

    # 목표가 돌파 종목
    target_hit = {}
    for ticker in acc.strategy:
        if ticker in locked:
            continue
        s     = acc.strategy[ticker]
        d     = s["data"]
        avg_p = d.get("avg", 0)
        qty_t = int(d.get("qty", 0))
        if avg_p <= 0 or qty_t <= 0:
            continue
        target_p = avg_p * (1 + s["target_profit"] / 100)
        try:
            curr_p = kis_api.get_current_price(acc, ticker)
            if curr_p >= target_p:
                target_hit[ticker] = {"curr": curr_p, "target": target_p,
                                      "avg": avg_p, "qty": qty_t,
                                      "profit_pct": s["target_profit"]}
        except Exception:
            pass

    if target_hit:
        hit_msg  = f"🎯 목표가 돌파 감지! {label}\n\n"
        hit_keys = []
        for ticker, h in target_hit.items():
            prt      = (h["curr"] - h["avg"]) / h["avg"] * 100
            one_time = acc.strategy[ticker]["seed"] / acc.strategy[ticker]["total_a"]
            entry_p  = round(h["curr"] * 1.15, 2)
            entry_qty = math.floor(one_time / h["curr"])
            hit_msg += (f"[{ticker}] 목표가 달성!\n"
                        f"  평단: ${h['avg']:.2f} / 현재: ${h['curr']:.2f} ({prt:.2f}%)\n"
                        f"  1단계 전량매도: {h['qty']}주 @ ${h['target']:.2f}\n"
                        f"  2단계 LOC재진입: {entry_qty}주 @ ${entry_p:.2f}\n\n")
            cb = f"profit:{acc.name}:{ticker}:{h['qty']}:{h['target']:.2f}:{entry_qty}:{entry_p:.2f}"
            hit_keys.append([InlineKeyboardButton(f"✅ {ticker} 익절+재진입 실행", callback_data=cb)])
        await update.message.reply_text(hit_msg, reply_markup=InlineKeyboardMarkup(hit_keys))

    # 일반 주문 지시서
    all_infos = {}
    for ticker in acc.strategy:
        if ticker in locked or ticker in target_hit:
            continue
        all_infos[ticker] = strategy.build_order_info(acc, ticker)

    if not all_infos:
        return

    total_req = sum(strategy.estimate_required_amount(all_infos[t]) for t in all_infos)
    adj_stage = 0
    if acc.current_cash > 0 and total_req > acc.current_cash:
        adj_stage = 1
        for t in all_infos:
            all_infos[t] = strategy.build_order_info(acc, t, no_turbo=True)
        total_req = sum(strategy.estimate_required_amount(all_infos[t]) for t in all_infos)
    if acc.current_cash > 0 and total_req > acc.current_cash and adj_stage == 1:
        adj_stage = 2
        for t in all_infos:
            info = all_infos[t]
            if info["qty_total"] > 0:
                info["force_quarter_sell"] = True
                info["qty_3_4"] = math.floor(info["qty_total"] * 0.75)
                info["qty_1_4"] = info["qty_total"] - info["qty_3_4"]

    # 지시서 텍스트
    detail = (f"📋 무한매수 통합 지시서 {label}\n"
              f"보유달러: ${acc.current_cash:,.2f}\n")
    if adj_stage > 0:
        detail += f"⚠️ 자금조정 {adj_stage}단계 적용\n"
        if adj_stage >= 1: detail += "  1단계: 가속매수 제외\n"
        if adj_stage >= 2: detail += "  2단계: 쿼터매도 발동\n"
    detail += "\n"

    keyboard  = []
    order_sum = f"🖱️ 버튼으로 즉시 주문 {label}\n\n"

    for ticker in all_infos:
        info = all_infos[ticker]
        s    = acc.strategy[ticker]
        ota  = s["seed"] / s["total_a"]

        if info["force_quarter_sell"] and info["qty_total"] > 0:
            reason  = f"T={info['t_val']:.1f}≥{int(info['total_a']-1)}" if info["force_quarter_sell"] else "잔금소진"
            detail += (f"[{ticker} - 쿼터매도 ({reason})]\n"
                       f"  평단: ${info['avg_price']:.2f}  수량: {info['qty_total']}주\n"
                       f"  1. MOC 1/4 ({info['qty_1_4']}주)\n"
                       f"  2. 지정가 3/4 ({info['qty_3_4']}주) → ${info['target_price']:.2f}\n\n")
            order_sum += f"{ticker}: 쿼터매도({reason}) / {info['qty_3_4']}+{info['qty_1_4']}주\n"
            cb_sell = (f"qsell:{acc.name}:{ticker}:{info['qty_3_4']}:{info['target_price']:.2f}:{info['qty_1_4']}")
            keyboard.append([InlineKeyboardButton(f"⚠️ {ticker} 쿼터매도 ({reason})", callback_data=cb_sell)])
            continue

        if info["qty_total"] == 0:
            try:
                curr_p    = kis_api.get_current_price(acc, ticker)
                entry_p   = curr_p * 1.15
                entry_qty = math.floor(ota / curr_p)
                detail   += f"[{ticker}] 최초 진입: {entry_qty}주 @ ${entry_p:.2f} LOC\n\n"
                order_sum += f"{ticker}: 최초진입 {entry_qty}주 @ ${entry_p:.2f}\n"
                cb_entry = f"entry:{acc.name}:{ticker}:{entry_qty}:{entry_p:.2f}"
                keyboard.append([InlineKeyboardButton(f"🆕 {ticker} 최초 매수 ({entry_qty}주)", callback_data=cb_entry)])
            except Exception:
                pass
            continue

        if info["t_val"] >= acc.strategy[ticker]["total_a"] - 1:
            continue

        half_lbl  = "전반전" if info["is_first_half"] else "후반전"
        turbo_txt = f"  가속: {info['turbo_qty']}주 @ ${info['turbo_price']:.4f}\n" if info["turbo_qty"] > 0 else ""
        buy_txt   = f"  매수1: {info['b1']}주 @ ${info['avg_price']:.4f}\n" if info["b1"] > 0 else ""
        req       = strategy.estimate_required_amount(info)

        # LOC 매도2 호가 보정 안내
        loc_sell_p   = info.get("loc_sell_price", round(info["star_price"] + 0.01, 4))
        loc_adj_note = ""
        if info.get("loc_adjusted"):
            loc_adj_note = (f"\n  ⚠️ LOC매도가 자동보정: ${info['star_price']:.4f}+0.01"
                            f" → ${loc_sell_p:.4f} (자전거래방지)")

        buy_star_p = info.get("buy_star_price", round(info["star_price"] - 0.01, 2))
        grid_prices = []
        if info["avg_price"] > 0:
            bq_ = math.floor((s["seed"]/s["total_a"]) / info["avg_price"])
            for i_ in range(1, 7):
                gp_ = round((s["seed"]/s["total_a"]) / (bq_ + i_), 2)
                if gp_ > 0: grid_prices.append(f"${gp_:.2f}")
        grid_txt = f"  줍줍: {len(grid_prices)}단계 ({" ".join(grid_prices[:3])}...)\n" if grid_prices else ""
        detail += (f"[{ticker}] {half_lbl}  T={info['t_val']:.2f}\n"
                   f"  평단: ${info['avg_price']:.2f}  |  보유: {info['qty_total']}주\n"
                   f"  별%: {info['star_pct']:+.2f}%  →  별가: ${info['star_price']:.2f}  (매수: ${buy_star_p:.2f})\n"
                   f"  목표가: ${info['target_price']:.2f} ({s['target_profit']:+.1f}%)\n"
                   f"{buy_txt}"
                   f"  매수2(별가-0.01): {info['b2']}주 @ ${buy_star_p:.2f}\n"
                   f"{turbo_txt}"
                   f"{grid_txt}"
                   f"  매도1(지정가): 3/4 {info['qty_3_4']}주 @ ${info['target_price']:.2f}\n"
                   f"  매도2(LOC):   1/4 {info['qty_1_4']}주 @ ${loc_sell_p:.2f}{loc_adj_note}\n"
                   f"  필요금액: ${req:,.2f}\n\n")

        turbo_txt2 = f"+가속{info['turbo_qty']}주" if info["turbo_qty"] > 0 else ""
        buy_label  = f"{info['b1']}+{info['b2']}주" if info["b1"] > 0 else f"{info['b2']}주(별%)"
        adj_note   = f" ⚠️보정" if info.get("loc_adjusted") else ""
        order_sum += f"{ticker}({half_lbl}): 매수{buy_label} {turbo_txt2} / 줍줍6 / 매도{info['qty_3_4']}+{info['qty_1_4']}주{adj_note}\n"

        # 콜백 데이터 (텔레그램 64바이트 제한)
        # loc_sell_p는 버튼 클릭 시 build_order_info()로 재계산하므로 제외
        cb_all = (f"all:{acc.name}:{ticker}:{info['b1']}:{info['avg_price']:.2f}:"
                  f"{info['b2']}:{info['star_price']:.2f}:{info['qty_3_4']}:{info['target_price']:.2f}:"
                  f"{info['qty_1_4']}:{info['turbo_qty']}:{info['turbo_price']:.2f}")
        keyboard.append([InlineKeyboardButton(f"🚀 {ticker} 통합 주문 ({half_lbl})", callback_data=cb_all)])

    # 잔고 요약 footer
    if total_req > 0:
        detail += f"══════════════════\n전체 필요금액: ${total_req:,.2f}\n"
        detail += (f"보유 달러:     ${acc.current_cash:,.2f}\n"
                   + (f"여유: ${acc.current_cash - total_req:,.2f}\n" if acc.current_cash >= total_req
                      else f"⚠️ 부족: ${total_req - acc.current_cash:,.2f}\n"))
        detail += "══════════════════"

    await update.message.reply_text(detail)
    if keyboard:
        await update.message.reply_text(order_sum, reply_markup=InlineKeyboardMarkup(keyboard))


# ============================================================
#  인라인 버튼 콜백
# ============================================================

async def _handle_set_callback(query, data: str, parts: list):
    """
    set:pick:<acc_name>:<ticker>
    set:seed:<acc_name>:<ticker>
    set:target:<acc_name>:<ticker>
    set:total_a:<acc_name>:<ticker>
    """
    sub = parts[1]  # pick / seed / target / total_a

    if sub == "pick":
        # set:pick:acc_name:ticker
        acc_name = parts[2]
        ticker   = parts[3]
        acc = next((a for a in cfg.ACCOUNTS if a.name == acc_name), None)
        if not acc:
            await query.edit_message_text(f"❌ 계좌를 찾을 수 없습니다: {acc_name}")
            return
        s = acc.strategy.get(ticker, {})
        turbo_now = s.get("use_turbo", True)
        turbo_toggle_lbl = f"⚡ 가속매수: {'ON ✅ → OFF로 변경' if turbo_now else 'OFF ❌ → ON으로 변경'}"
        keyboard = [
            [InlineKeyboardButton(
                f"💰 예산 변경  (현재 ${s.get('seed',0):,.0f})",
                callback_data=f"set:seed:{acc_name}:{ticker}")],
            [InlineKeyboardButton(
                f"🎯 목표수익률 변경  (현재 {s.get('target_profit',10):.1f}%)",
                callback_data=f"set:target:{acc_name}:{ticker}")],
            [InlineKeyboardButton(
                f"📊 분할횟수 변경  (현재 {int(s.get('total_a',20))}회)",
                callback_data=f"set:total_a:{acc_name}:{ticker}")],
            [InlineKeyboardButton(
                turbo_toggle_lbl,
                callback_data=f"set:turbo:{acc_name}:{ticker}")],
        ]
        await query.edit_message_text(
            f"⚙️ [{ticker}] 변경할 항목을 선택하세요:",
            reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if sub == "turbo":
        # 가속매수 ON/OFF 즉시 토글 (값 입력 불필요)
        acc_name = parts[2]
        ticker   = parts[3]
        acc = next((a for a in cfg.ACCOUNTS if a.name == acc_name), None)
        if not acc:
            await query.edit_message_text(f"❌ 계좌를 찾을 수 없습니다: {acc_name}")
            return
        s = acc.strategy.get(ticker, {})
        current = s.get("use_turbo", True)
        new_val = not current
        s["use_turbo"] = new_val
        acc.strategy_config[ticker]["use_turbo"] = new_val
        status = "ON ✅" if new_val else "OFF ❌"
        await query.edit_message_text(
            f"✅ [{ticker}] 가속매수 → {status}\n\n"
            f"⚠️ 재시작 시 .env 값으로 초기화됩니다.\n"
            f".env 에서 {acc_name}_{ticker}_USE_TURBO={'true' if new_val else 'false'} 로 설정하세요.")
        return

    if sub in ("seed", "target", "total_a"):
        # set:seed:acc_name:ticker
        acc_name = parts[2]
        ticker   = parts[3]
        chat_id  = query.message.chat_id
        labels   = {
            "seed":    "예산 (USD, 예: 10000)",
            "target":  "목표수익률 (%, 예: 10.0)",
            "total_a": "분할횟수 (예: 40)",
        }
        _set_state[chat_id] = {"acc_name": acc_name, "ticker": ticker, "field": sub}
        await query.edit_message_text(
            f"⚙️ [{ticker}] {labels[sub]} 를 입력하세요:\n(취소: /cancel)")
        return

    await query.edit_message_text(f"⚙️ 알 수 없는 set 명령: {sub}")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data   = query.data
    parts  = data.split(":")
    action = parts[0]

    # ── set: 계열은 구조가 달라 맨 앞에서 먼저 처리 ──────────
    if action == "set":
        await _handle_set_callback(query, data, parts)
        return

    # parts[1] = acc.name, parts[2] = ticker (all/profit/entry/qsell 공통)
    acc_name = parts[1]
    ticker   = parts[2]

    acc = _get_account(acc_name)
    if acc is None:
        await query.edit_message_text("❌ 계좌를 찾을 수 없습니다.")
        return

    if storage.is_locked(acc, ticker):
        await query.edit_message_text(
            f"🔒 {ticker} ({acc_name}) 오늘 이미 매매 완료 — 중복 주문 차단\n"
            f"/reset {acc_name} 으로 해제할 수 있습니다.")
        return

    # ── profit: 익절 + LOC 재진입 ──────────────────────────
    if action == "profit":
        sell_qty   = int(parts[3])
        sell_price = float(parts[4])
        buy_qty    = int(parts[5])
        buy_price  = float(parts[6])

        await query.edit_message_text(f"⏳ {ticker} 익절+재진입 주문 전송 중...")
        msg = f"[{ticker}] 익절+재진입\n\n1단계 전량 매도\n"

        res_sell = kis_api.send_order(acc, ticker, sell_qty, sell_price, "SELL", "00")
        if res_sell.get("rt_cd") == "0":
            msg += f"  {sell_qty}주 @ ${sell_price:.2f}: ✅\n"
        else:
            msg += f"  {sell_qty}주 @ ${sell_price:.2f}: ❌ ({res_sell.get('msg1','')})\n"

        msg += "\n2단계 LOC 재진입\n"
        res_buy = kis_api.send_order(acc, ticker, buy_qty, buy_price, "BUY", "LOC")
        if res_buy.get("rt_cd") == "0":
            msg += f"  {buy_qty}주 @ ${buy_price:.2f}: ✅\n"
        else:
            msg += f"  {buy_qty}주 @ ${buy_price:.2f}: ❌ ({res_buy.get('msg1','')})\n"

        storage.set_lock(acc, ticker)
        await query.edit_message_text(msg)
        return

    # ── entry: 최초 진입 ────────────────────────────────────
    if action == "entry":
        entry_qty   = int(parts[3])
        entry_price = float(parts[4])

        required = entry_qty * entry_price
        if acc.current_cash > 0 and acc.current_cash < required:
            await query.edit_message_text(
                f"⚠️ 예수금 부족\n필요: ${required:.2f}\n보유: ${acc.current_cash:.2f}")
            return

        await query.edit_message_text(f"⏳ {ticker} 최초 매수 주문 전송 중...")
        res = kis_api.send_order(acc, ticker, entry_qty, entry_price, "BUY", "LOC")
        if res.get("rt_cd") == "0":
            msg = f"[{ticker}] 최초 진입 ✅\n{entry_qty}주 @ ${entry_price:.2f} LOC"
        else:
            msg = f"[{ticker}] 최초 진입 ❌\n{entry_qty}주 @ ${entry_price:.2f}\n{res.get('msg1','')}"
        storage.set_lock(acc, ticker)
        await context.bot.send_message(chat_id=query.message.chat_id, text=msg)
        return

    # ── all: 통합 주문 ──────────────────────────────────────
    if action == "all":
        b1_qty    = int(parts[3])
        avg_p     = float(parts[4])
        b2_qty    = int(parts[5])
        star_p    = float(parts[6])
        q_3_4     = int(parts[7])
        target_p  = float(parts[8])
        q_1_4     = int(parts[9])
        turbo_qty = int(parts[10]) if len(parts) > 10 else 0
        turbo_p   = float(parts[11]) if len(parts) > 11 else 0.0
        # loc_sell_p는 콜백에서 제거됨 (64바이트 제한) → 현재 상태로 재계산
        storage.sync_account(acc)
        info_live = strategy.build_order_info(acc, ticker)
        loc_sell_p_cb = info_live.get("loc_sell_price", round(star_p + 0.01, 2))
        loc_adj       = info_live.get("loc_adjusted", False)
        loc_reason    = info_live.get("loc_adjust_reason", "")

        # 예수금 체크
        req_amt = b1_qty * avg_p + b2_qty * star_p + turbo_qty * turbo_p
        if avg_p > 0:
            s     = acc.strategy.get(ticker, {})
            ota   = s.get("seed", 0) / s.get("total_a", 20)
            bq    = math.floor(ota / avg_p)
            for ri in range(1, 7):
                rp = round(ota / (bq + ri), 2)
                if rp > 0:
                    req_amt += rp
        if acc.current_cash > 0 and acc.current_cash < req_amt:
            await query.edit_message_text(
                f"⚠️ 예수금 부족\n필요(추정): ${req_amt:.2f}\n보유: ${acc.current_cash:.2f}")
            return

        await query.edit_message_text(f"⏳ {ticker} 통합 주문 전송 중...")

        info = {
            "avg_price": avg_p, "star_price": star_p, "target_price": target_p,
            "b1": b1_qty, "b2": b2_qty,
            "qty_total": q_3_4 + q_1_4, "qty_3_4": q_3_4, "qty_1_4": q_1_4,
            "turbo_qty": turbo_qty, "turbo_price": turbo_p,
            "is_first_half": True, "force_quarter_sell": False,
            "t_val": 0, "total_a": acc.strategy.get(ticker, {}).get("total_a", 20),
            "seed": acc.strategy.get(ticker, {}).get("seed", 10000),
            "star_pct": 0, "target_profit": acc.strategy.get(ticker, {}).get("target_profit", 12),
            "no_turbo": False,
            "loc_sell_price":    loc_sell_p_cb,
            "loc_adjusted":      loc_adj,
            "loc_adjust_reason": loc_reason,
        }
        result = strategy.execute_all_order(acc, ticker, info)
        await context.bot.send_message(chat_id=query.message.chat_id, text=result)
        return

    # ── qsell: 쿼터매도 ────────────────────────────────────
    if action == "qsell":
        q_3_4    = int(parts[3])
        target_p = float(parts[4])
        q_1_4    = int(parts[5])

        await query.edit_message_text(f"⏳ {ticker} 쿼터매도 전송 중...")
        msg = f"[{ticker}] 쿼터매도\n\n"

        if q_1_4 > 0:
            res = kis_api.send_order(acc, ticker, q_1_4, 0, "SELL", "MOC")
            msg += f"MOC 1/4 ({q_1_4}주): {'✅' if res.get('rt_cd')=='0' else '❌ '+res.get('msg1','')}\n"
        if q_3_4 > 0:
            res = kis_api.send_order(acc, ticker, q_3_4, target_p, "SELL", "00")
            msg += f"지정가 3/4 ({q_3_4}주) @ ${target_p:.2f}: {'✅' if res.get('rt_cd')=='0' else '❌ '+res.get('msg1','')}\n"

        storage.set_lock(acc, ticker)
        await context.bot.send_message(chat_id=query.message.chat_id, text=msg)
        return

    await query.edit_message_text("알 수 없는 명령입니다.")


# ============================================================
#  /order [계좌명]  — 즉시 주문 실행 (버튼 없이 바로 전송)
# ============================================================

async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sync의 주문 부분만 즉시 실행합니다. 버튼 클릭 없이 바로 주문합니다.
    잠긴 종목은 건너뜁니다.
    """
    if not _is_allowed(update):
        return

    accounts = _target_accounts(context.args)
    for acc in accounts:
        label  = _acc_label(acc)
        locked = [t for t in acc.strategy if storage.is_locked(acc, t)]

        if len(locked) == len(acc.strategy):
            await update.message.reply_text(f"✅ {label} 오늘 주문 이미 완료 — 잠긴 종목: {', '.join(locked)}")
            continue

        ok, res_msg = storage.sync_account(acc)
        if not ok:
            await update.message.reply_text(f"❌ {label} 동기화 실패: {res_msg}")
            continue

        await update.message.reply_text(f"⏳ {label} 주문 실행 중...")

        order_log = []
        for ticker in acc.strategy:
            if storage.is_locked(acc, ticker):
                order_log.append(f"[{ticker}] 건너뜀 (오늘 처리 완료)")
                continue

            info = strategy.build_order_info(acc, ticker)

            # 쿼터매도 발동
            if info["force_quarter_sell"] and info["qty_total"] > 0:
                reason = f"T={info['t_val']:.1f}>=19" if info["t_val"] >= 19 else "잔금소진"
                order_log.append(f"[{ticker}] 쿼터매도 ({reason})")
                if info["qty_1_4"] > 0:
                    res = kis_api.send_order(acc, ticker, info["qty_1_4"], 0, "SELL", "MOC")
                    order_log.append(f"  MOC 1/4({info['qty_1_4']}주): {'✅' if res.get('rt_cd')=='0' else '❌ '+res.get('msg1','')}")
                if info["qty_3_4"] > 0:
                    res = kis_api.send_order(acc, ticker, info["qty_3_4"], info["target_price"], "SELL", "00")
                    order_log.append(f"  지정가 3/4({info['qty_3_4']}주) ${info['target_price']:.2f}: {'✅' if res.get('rt_cd')=='0' else '❌ '+res.get('msg1','')}")
                storage.set_lock(acc, ticker)
                continue

            # 최초 진입 (보유 없음)
            if info["qty_total"] == 0:
                try:
                    curr_p    = kis_api.get_current_price(acc, ticker)
                    s         = acc.strategy[ticker]
                    ota       = s["seed"] / s["total_a"]
                    entry_p   = round(curr_p * 1.15, 2)
                    entry_qty = math.floor(ota / curr_p)
                    if entry_qty > 0:
                        res = kis_api.send_order(acc, ticker, entry_qty, entry_p, "BUY", "LOC")
                        order_log.append(f"[{ticker}] 최초 진입 {entry_qty}주 @ ${entry_p:.2f}: "
                                         f"{'✅' if res.get('rt_cd')=='0' else '❌ '+res.get('msg1','')}")
                        storage.set_lock(acc, ticker)
                except Exception as e:
                    order_log.append(f"[{ticker}] 최초 진입 오류: {e}")
                continue

            # 통합 주문
            result = strategy.execute_all_order(acc, ticker, info)
            order_log.extend(result.splitlines())

        reply = "\n".join(order_log) if order_log else "처리할 주문 없음"
        await update.message.reply_text(f"📋 {label} 주문 결과\n\n{reply}")


# ============================================================
#  /orders [계좌명]  — 미체결 주문 조회
# ============================================================

async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """현재 미체결 주문 목록을 조회합니다."""
    if not _is_allowed(update):
        return

    await update.message.reply_text("\U0001f50d 미체결 주문 조회 중...")
    accounts = _target_accounts(context.args)

    for acc in accounts:
        label = _acc_label(acc)
        res   = kis_api.query_pending_orders(acc)

        if res.get("rt_cd") != "0":
            await update.message.reply_text(
                f"\u274c 미체결 조회 실패 {label}: {res.get('msg1','')}")
            continue

        orders = res.get("output", [])
        orders = [o for o in orders if int(float(o.get("nccs_qty", "0"))) > 0]

        if not orders:
            await update.message.reply_text(f"\u2705 미체결 주문 없음 {label}")
            continue

        msg = f"\U0001f4cb 미체결 주문 목록 {label} ({len(orders)}건)\n\n"
        for o in orders:
            ticker   = o.get("pdno", "?")
            name     = o.get("prdt_name", "")
            side_cd  = o.get("sll_buy_dvsn_cd", "")
            side_str = "\U0001f4c9매도" if side_cd == "01" else "\U0001f4c8매수"
            ord_qty  = int(float(o.get("ft_ord_qty", "0")))
            filled   = int(float(o.get("ft_ccld_qty", "0")))
            remain   = int(float(o.get("nccs_qty", "0")))
            price    = float(o.get("ft_ord_unpr3", "0"))
            ord_time = o.get("ord_tmd", "")
            odno     = o.get("odno", "?")
            rjct     = o.get("rjct_rson_name", "").strip()
            time_str = f"{ord_time[:2]}:{ord_time[2:4]}:{ord_time[4:]}" if len(ord_time) == 6 else ord_time
            rjct_str = f"\n  \u26a0\ufe0f 거부사유: {rjct}" if rjct else ""
            msg += (
                f"[{ticker}] {name} {side_str}\n"
                f"  주문: {ord_qty}주  체결: {filled}주  미체결: {remain}주\n"
                f"  호가: ${price:.2f}  주문번호: {odno}  시각: {time_str}{rjct_str}\n\n"
            )
        await update.message.reply_text(msg)



async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    accounts = _target_accounts(context.args)
    for acc in accounts:
        label = _acc_label(acc)
        import os as _os
        if _os.path.exists(acc.trade_lock_file):
            try:
                locks   = json.load(open(acc.trade_lock_file))
                cleared = ", ".join(f"{k}({v})" for k, v in locks.items())
                _os.remove(acc.trade_lock_file)
                await update.message.reply_text(
                    f"🔓 잠금 해제 완료 {label}\n해제: {cleared}\n/sync 로 수동 주문 가능합니다.")
            except Exception:
                _os.remove(acc.trade_lock_file)
                await update.message.reply_text(f"🔓 잠금 해제 완료 {label}")
        else:
            await update.message.reply_text(f"✅ 잠금된 종목 없음 {label}")


# ============================================================
#  /profit_history [계좌명]
# ============================================================

async def cmd_profit_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    accounts = _target_accounts(context.args)
    for acc in accounts:
        label   = _acc_label(acc)
        cp      = storage.load_cumul(acc)
        history = cp.get("history", [])

        if not history:
            await update.message.reply_text(f"📭 수익 내역 없음 {label}")
            continue

        msg = f"📜 수익 누적 내역 {label}\n\n"
        for h in history[-20:]:
            msg += (f"{h['date']} {h['ticker']}\n"
                    f"  {h['sell_qty']}주 @ ${h['sell_price']:.2f} (평단 ${h['avg_price']:.2f})\n"
                    f"  수익: ${h['profit']:+,.2f}  누적: ${h['cumul_total']:,.2f}\n")

        soxl_c = cp.get("SOXL", 0.0); tqqq_c = cp.get("TQQQ", 0.0)
        msg += (f"\n[현재 누적]\n"
                f"  SOXL: ${soxl_c:,.2f}  TQQQ: ${tqqq_c:,.2f}\n"
                f"  합계: ${soxl_c + tqqq_c:,.2f}")

        await update.message.reply_text(msg)


# ============================================================
#  /profit_reset [계좌명]
# ============================================================

async def cmd_profit_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    accounts = _target_accounts(context.args)
    for acc in accounts:
        label = _acc_label(acc)
        cp    = storage.load_cumul(acc)
        soxl  = cp.get("SOXL", 0.0); tqqq = cp.get("TQQQ", 0.0)
        total = soxl + tqqq

        if total == 0:
            await update.message.reply_text(f"초기화할 수익 없음 {label}")
            continue

        storage.save_cumul(acc, {"SOXL": 0.0, "TQQQ": 0.0, "history": [], "processed_orders": []})
        msg = (f"🗑️ 누적 수익 초기화 완료 {label}\n\n"
               f"초기화 내역:\n"
               + (f"  SOXL: ${soxl:,.2f}\n" if soxl else "")
               + (f"  TQQQ: ${tqqq:,.2f}\n" if tqqq else "")
               + f"  합계: ${total:,.2f}\n\n"
               f"수익금은 계좌에 현금으로 보유 중입니다.")
        await update.message.reply_text(msg)


# ============================================================
#  봇 내 스케줄 작업 (자동 알림)
# ============================================================

async def _bot_premarket_check(context: ContextTypes.DEFAULT_TYPE):
    """프리마켓 목표가 체크 + 텔레그램 알림."""
    import pytz as _pytz
    kst     = _pytz.timezone("Asia/Seoul")
    now_kst = datetime.datetime.now(kst)
    if not (now_kst.hour == 18 and now_kst.minute < 30):
        return
    if not kis_api.is_market_open_today():
        return

    chat_id = context.job.chat_id
    for acc in cfg.ACCOUNTS:
        label = _acc_label(acc)
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

            # 목표가 도달
            storage.sync_account(acc)
            avg_p = acc.strategy[ticker]["data"].get("avg", 0)
            qty_t = int(acc.strategy[ticker]["data"].get("qty", 0))
            profit_rt = (curr_p - avg_p) / avg_p * 100
            one_time  = s["seed"] / s["total_a"]
            entry_p   = round(curr_p * 1.15, 2)
            entry_qty = math.floor(one_time / curr_p)

            await context.bot.send_message(
                chat_id=chat_id,
                text=(f"🎯 {label} [{ticker}] 프리마켓 목표가 돌파!\n"
                      f"  현재: ${curr_p:.2f} / 평단: ${avg_p:.2f} ({profit_rt:+.1f}%)\n"
                      f"  자동 익절 실행 중..."))

            res_sell = kis_api.send_order(acc, ticker, qty_t, curr_p, "SELL", "00")
            if res_sell.get("rt_cd") != "0":
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ {label} [{ticker}] 매도 실패: {res_sell.get('msg1','')}")
                storage.set_lock(acc, ticker)
                continue

            order_no = res_sell.get("output", {}).get("ODNO", "")
            filled, fq, fp = kis_api.check_order_filled(acc, order_no, ticker, "SELL")
            if filled:
                realized  = (fp - avg_p) * fq
                new_cumul = storage.add_profit(acc, ticker, realized, fq, fp, avg_p, order_no=order_no)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(f"✅ {label} [{ticker}] 익절 체결\n"
                          f"  {fq}주 @ ${fp:.2f}  수익 ${realized:+,.2f}  누적 ${new_cumul:,.2f}"))
                res_buy = kis_api.send_order(acc, ticker, entry_qty, entry_p, "BUY", "LOC")
                status  = "✅ 성공" if res_buy.get("rt_cd") == "0" else f"❌ 실패({res_buy.get('msg1','')})"
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"  LOC 재진입 {entry_qty}주 @ ${entry_p:.2f}: {status}")
                reporter.send_report(acc, trigger="premarket",
                                     order_log=[f"[{ticker}] 익절 {fq}주 @ ${fp:.2f} → 재진입 {entry_qty}주"])
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ {label} [{ticker}] 매도 미체결 — /sync {acc.name} 으로 수동 확인하세요.")
            storage.set_lock(acc, ticker)


async def _bot_daily_trade(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    if not kis_api.is_market_open_today():
        await context.bot.send_message(chat_id=chat_id, text="📅 오늘은 미국 증시 휴장일입니다.")
        return
    await context.bot.send_message(chat_id=chat_id, text="🤖 18:30 KST 자동매매 시작!")
    for acc in cfg.ACCOUNTS:
        label = _acc_label(acc)
        await context.bot.send_message(chat_id=chat_id, text=f"⏳ {label} 주문 처리 중...")
        jobs._daily_trade_one(acc)
        await context.bot.send_message(chat_id=chat_id, text=f"✅ {label} 자동매매 완료")


async def _bot_bil_manage(context: ContextTypes.DEFAULT_TYPE):
    """BIL 버퍼 관리 — KST 18:25 자동 실행."""
    try:
        jobs.job_bil_manage()
    except Exception as e:
        log.error(f"[BIL봇] 오류: {e}")


async def _bot_doublecheck(context: ContextTypes.DEFAULT_TYPE):
    """정규장 개장 후 더블체크 — KST 00:10 자동 실행.
    BIL 매도가 있었던 날만 SOXL/TQQQ 주문 상태를 확인하고 결과를 알림."""
    chat_id = context.job.chat_id
    try:
        results = jobs.job_doublecheck()
        if not results:
            return  # BIL 매도 없었던 날은 조용히 패스
        for acc_name, msgs in results.items():
            header = f"\U0001f50d [더블체크] {acc_name} — BIL 매도일 주문 확인\n\n"
            body   = "\n".join(msgs)
            await context.bot.send_message(chat_id=chat_id, text=header + body)
    except Exception as e:
        log.error(f"[더블체크봇] 오류: {e}")
        await context.bot.send_message(
            chat_id=chat_id, text=f"\u274c 더블체크 오류: {e}")


async def _bot_settlement_check(context: ContextTypes.DEFAULT_TYPE):
    import pandas_market_calendars as mcal
    import pytz as _pytz
    chat_id = context.job.chat_id
    est   = _pytz.timezone("US/Eastern")
    today = datetime.datetime.now(est).date()
    nyse  = mcal.get_calendar("NYSE")
    yest  = today - datetime.timedelta(days=1)
    if nyse.schedule(start_date=today, end_date=today).empty and \
       nyse.schedule(start_date=yest, end_date=yest).empty:
        return
    await context.bot.send_message(chat_id=chat_id, text="🔍 전일 체결 확인 중...")
    for acc in cfg.ACCOUNTS:
        jobs._settlement_one(acc)
    await context.bot.send_message(chat_id=chat_id, text="✅ 체결 확인 완료")


async def _bot_token_renewal(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    for acc in cfg.ACCOUNTS:
        label = _acc_label(acc)
        import os as _os
        old_expire = "없음"
        if _os.path.exists(acc.token_file):
            try:
                old_expire = json.load(open(acc.token_file)).get("expire", "없음")
            except Exception:
                pass
        kis_api.renew_token_if_needed(acc)
        try:
            new_expire = json.load(open(acc.token_file)).get("expire", "?")
            if new_expire != old_expire:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🔑 {label} 토큰 갱신 완료\n이전: {old_expire}\n새만료: {new_expire}")
        except Exception:
            pass


def _register_bot_schedules(job_queue, chat_id: int):
    """봇 job_queue에 스케줄 등록 (기존 작업 초기화 후 재등록)."""
    for job in job_queue.jobs():
        job.schedule_removal()

    kst = pytz.timezone("Asia/Seoul")
    job_queue.run_daily(
        _bot_doublecheck,
        time=datetime.time(0, 10, tzinfo=kst),
        days=(1, 2, 3, 4, 5),   # 화~토 (전날 개장분 더블체크)
        chat_id=chat_id,
    )
    job_queue.run_daily(
        _bot_bil_manage,
        time=datetime.time(18, 25, tzinfo=kst),
        days=(0, 1, 2, 3, 4),
        chat_id=chat_id,
    )
    job_queue.run_daily(
        _bot_daily_trade,
        time=datetime.time(18, 30, tzinfo=kst),
        days=(0, 1, 2, 3, 4),
        chat_id=chat_id,
    )
    job_queue.run_daily(
        _bot_settlement_check,
        time=datetime.time(8, 0, tzinfo=kst),
        days=(0, 1, 2, 3, 4, 5),
        chat_id=chat_id,
    )
    job_queue.run_repeating(_bot_token_renewal,   interval=21600, first=60,  chat_id=chat_id)
    job_queue.run_repeating(_bot_premarket_check, interval=300,   first=30,  chat_id=chat_id)

    log.info(f"[텔레그램] 스케줄 등록 완료 (chat_id={chat_id})")


# ============================================================
#  시작 시 자동 복구
# ============================================================

async def _post_init(application: Application):
    chat_id = _load_chat_id()
    if chat_id:
        try:
            for acc in cfg.ACCOUNTS:
                storage.sync_account(acc)
            _register_bot_schedules(application.job_queue, chat_id)
            await application.bot.send_message(
                chat_id=chat_id,
                text="[시스템] 봇 재시작 완료 — 계좌 동기화 및 스케줄 등록됨")
        except Exception as e:
            log.error(f"[텔레그램] 재시작 초기화 실패: {e}")
    else:
        log.info("[텔레그램] 저장된 chat_id 없음 — /start 를 먼저 보내세요")


async def _error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    import telegram.error as tgerr
    err = context.error
    if isinstance(err, tgerr.Conflict):
        log.warning("[텔레그램] Conflict — 다른 인스턴스와 충돌, 자동 복구")
    elif isinstance(err, tgerr.NetworkError):
        log.warning(f"[텔레그램] NetworkError: {err}")
    elif isinstance(err, tgerr.TimedOut):
        log.warning("[텔레그램] TimedOut — 자동 재시도")
    else:
        log.error(f"[텔레그램] 오류: {type(err).__name__}: {err}")


# ============================================================
#  봇 실행 진입점
# ============================================================

def _build_app() -> Application:
    """Application 객체 생성 및 핸들러 등록."""
    app = (ApplicationBuilder()
           .token(TELEGRAM_TOKEN)
           .post_init(_post_init)
           .build())

    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("status",         cmd_status))
    app.add_handler(CommandHandler("sync",           cmd_sync))
    app.add_handler(CommandHandler("order",          cmd_order))
    app.add_handler(CommandHandler("orders",         cmd_orders))
    app.add_handler(CommandHandler("cash",           cmd_cash))
    app.add_handler(CommandHandler("report",         cmd_report))
    app.add_handler(CommandHandler("reset",          cmd_reset))
    app.add_handler(CommandHandler("profit_history", cmd_profit_history))
    app.add_handler(CommandHandler("profit_reset",   cmd_profit_reset))
    app.add_handler(CommandHandler("preview",        cmd_preview))
    app.add_handler(CommandHandler("set",            cmd_set))
    app.add_handler(CommandHandler("settings",       cmd_settings))
    app.add_handler(CommandHandler("cancel",         cmd_cancel_set))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_set_input))
    app.add_error_handler(_error_handler)
    return app


async def _run_polling_async(app: Application):
    """비동기 polling 루프 — 서브 스레드용 이벤트 루프에서 직접 호출."""
    await app.initialize()
    await app.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
    await app.start()
    log.info("[텔레그램] 봇 polling 시작됨")
    # 종료 시그널 없이 무한 대기 (메인 스레드가 종료되면 daemon 스레드도 종료)
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


# ── /set 대화 상태 ────────────────────────────────────────
_set_state: dict = {}   # {chat_id: {acc_name, ticker, field}}


async def cmd_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """오늘 예상 주문 내역을 상세히 보여줍니다. 실제 주문은 하지 않습니다."""
    if not _is_allowed(update): return
    for acc in _target_accounts(context.args):
        ok, _ = storage.sync_account(acc)
        if not ok:
            await update.message.reply_text(f"❌ {_acc_label(acc)} 동기화 실패"); continue
        await update.message.reply_text(_build_preview_text(acc))


def _build_preview_text(acc) -> str:
    lines = [f"📋 주문 미리보기 — {_acc_label(acc)}\n"]
    for ticker in acc.strategy:
        s   = acc.strategy[ticker]
        ota = s["seed"] / s["total_a"]
        if storage.is_locked(acc, ticker):
            lines.append(f"[{ticker}] 🔒 오늘 주문 완료\n"); continue
        info  = strategy.build_order_info(acc, ticker)
        avg_p = info["avg_price"]
        if avg_p <= 0:
            lines.append(f"[{ticker}] 보유 없음 — 최초 진입 필요\n"); continue
        half_lbl   = "전반전" if info["is_first_half"] else "후반전"
        star_p     = info["star_price"]
        buy_star_p = info.get("buy_star_price", round(star_p - 0.01, 2))
        loc_sell   = info["loc_sell_price"]
        adj_mark   = " ⚠️(보정)" if info["loc_adjusted"] else ""
        lines += [
            f"━━━ [{ticker}] {half_lbl}  T={info['t_val']:.2f} ━━━",
            f"  평단가: ${avg_p:.2f}  |  보유: {info['qty_total']}주",
            f"  별%: {info['star_pct']:+.2f}%  |  별가: ${star_p:.2f}",
            f"  매수별가(-0.01): ${buy_star_p:.2f}",
            f"  목표가: ${info['target_price']:.2f} ({s['target_profit']:+.1f}%)",
            "",
        ]
        if info["force_quarter_sell"]:
            qbp   = info.get("quarter_buy_p", round(avg_p * 0.9, 2))
            qstep = info.get("quarter_step", 0)
            bqty  = math.floor(ota / qbp) if qbp > 0 else 0
            lines.append(f"  ⚠️ 쿼터손절 모드 (step={qstep})")
            lines.append(f"  매수: {bqty}주 @ ${qbp:.2f}  (-{s['target_profit']:.0f}% LOC)")
            if qstep < 10:
                lq = round(avg_p * (1 - s["target_profit"]/100), 2)
                lines += [f"  매도 LOC 1/4: {info['qty_1_4']}주 @ ${lq:.2f}",
                          f"  매도 지정가 3/4: {info['qty_3_4']}주 @ ${info['target_price']:.2f}"]
            else:
                lines.append(f"  매도 MOC 1/4: {info['qty_1_4']}주")
        else:
            buy_lines = []
            if info["b1"] > 0:
                buy_lines.append(f"    매수1 (평단  LOC): {info['b1']}주 @ ${avg_p:.2f}")
            if info["b2"] > 0:
                buy_lines.append(f"    매수2 (별가  LOC): {info['b2']}주 @ ${buy_star_p:.2f}")
            if info["turbo_qty"] > 0:
                buy_lines.append(f"    가속  (LOC):       {info['turbo_qty']}주 @ ${info['turbo_price']:.2f}")
            bq  = math.floor(ota / avg_p) if avg_p > 0 else 0
            gps = [round(ota/(bq+i), 2) for i in range(1, 7) if bq+i > 0 and ota/(bq+i) > 0]
            if gps:
                buy_lines.append(f"    줍줍  (LOC 각 1주): {', '.join(f'${p:.2f}' for p in gps)}")
            req = strategy.estimate_required_amount(info)
            lines += ["  【매수】"] + buy_lines + [
                "  【매도】",
                f"    매도1 (지정가): {info['qty_3_4']}주 @ ${info['target_price']:.2f}",
                f"    매도2 (LOC):    {info['qty_1_4']}주 @ ${loc_sell:.2f}{adj_mark}",
                f"  필요금액: ${req:,.2f}",
            ]
        lines.append("")
    lines += [f"━━━━━━━━━━━━━━━━━━━━━━", f"예수금: ${acc.current_cash:,.2f}"]
    return "\n".join(lines)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """현재 전략 설정을 확인합니다."""
    if not _is_allowed(update): return
    for acc in _target_accounts(context.args):
        lines = [f"⚙️ 전략 설정 — {_acc_label(acc)}\n"]
        for ticker, s in acc.strategy.items():
            ota = s["seed"] / s["total_a"]
            turbo_lbl = "ON ✅" if s.get("use_turbo", True) else "OFF ❌"
            lines += [f"[{ticker}]",
                      f"  예산(시드):   ${s['seed']:,.0f}",
                      f"  분할횟수:     {int(s['total_a'])}회",
                      f"  1회 매수액:   ${ota:,.2f}",
                      f"  목표수익률:   {s['target_profit']:.1f}%",
                      f"  가속매수:     {turbo_lbl}",
                      f"  전반전 기준:  T ≤ {s['total_a']/2:.0f}",
                      f"  쿼터손절:     T ≥ {s['total_a']-1:.0f}", ""]
        await update.message.reply_text("\n".join(lines))


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """전략 설정을 변경합니다. /set [계좌명]"""
    if not _is_allowed(update): return
    accounts = _target_accounts(context.args)
    if len(accounts) != 1:
        await update.message.reply_text(
            f"⚙️ /set 은 계좌를 하나만 지정하세요.  예: /set {cfg.ACCOUNTS[0].name}"); return
    acc = accounts[0]
    keyboard = []
    for ticker in acc.strategy:
        s = acc.strategy[ticker]
        turbo_lbl = "가속✅" if s.get("use_turbo", True) else "가속❌"
        keyboard.append([InlineKeyboardButton(
            f"[{ticker}]  시드 ${s['seed']:,.0f}  /  목표 {s['target_profit']:.1f}%  /  {int(s['total_a'])}회  /  {turbo_lbl}",
            callback_data=f"set:pick:{acc.name}:{ticker}")])
    await update.message.reply_text(
        f"⚙️ 설정 변경 — {_acc_label(acc)}\n변경할 종목을 선택하세요:",
        reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_cancel_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """설정 변경 대화 취소."""
    if not _is_allowed(update): return
    chat_id = update.effective_chat.id
    if chat_id in _set_state:
        del _set_state[chat_id]
        await update.message.reply_text("⚙️ 설정 변경이 취소되었습니다.")
    else:
        await update.message.reply_text("진행 중인 설정 변경이 없습니다.")


async def handle_set_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/set 대화 중 사용자가 입력한 값을 처리합니다."""
    if not _is_allowed(update): return
    chat_id = update.effective_chat.id
    state   = _set_state.get(chat_id)
    if not state: return
    acc_name, ticker, field = state["acc_name"], state["ticker"], state["field"]
    acc = next((a for a in cfg.ACCOUNTS if a.name == acc_name), None)
    if not acc or ticker not in acc.strategy:
        await update.message.reply_text("❌ 계좌/종목을 찾을 수 없습니다.")
        del _set_state[chat_id]; return
    try:
        value = float(update.message.text.strip())
        if field == "seed"    and value <= 0:          raise ValueError("예산은 0 초과")
        if field == "target"  and not (0 < value <= 100): raise ValueError("목표수익률 0~100%")
        if field == "total_a" and value < 10:           raise ValueError("분할횟수 10 이상")
    except ValueError as e:
        await update.message.reply_text(f"❌ 입력값 오류: {e}\n다시 입력하거나 /cancel 로 취소"); return
    s = acc.strategy[ticker]
    old = s.get("target_profit" if field=="target" else field, 0)
    if   field == "seed":    s["seed"] = acc.strategy_config[ticker]["seed"] = value
    elif field == "target":  s["target_profit"] = acc.strategy_config[ticker]["target_profit"] = value
    elif field == "total_a": s["total_a"] = acc.strategy_config[ticker]["total_a"] = value
    units = {"seed": "USD", "target": "%", "total_a": "회"}
    names = {"seed": "예산", "target": "목표수익률", "total_a": "분할횟수"}
    del _set_state[chat_id]
    await update.message.reply_text(
        f"✅ [{ticker}] {names[field]} 변경 완료\n"
        f"  {old} → {value} {units[field]}\n"
        f"  1회 매수액: ${s['seed']/s['total_a']:,.2f}\n\n"
        f"⚠️ 재시작 시 .env 값으로 초기화됩니다.\n"
        f".env 의 {acc_name}_{ticker}_{field.upper()} 를 직접 수정해 영구 저장하세요.")
    log.info(f"[설정변경][{acc_name}][{ticker}] {field}: {old} → {value}")


def run_bot():
    """
    텔레그램 봇 실행.
    - main.py에서 메인 스레드로 호출 시: run_polling() 직접 실행
    - 서브 스레드에서 호출 시: 새 이벤트 루프를 생성해 asyncio.run()으로 실행
      (run_polling은 메인 스레드 시그널 핸들러를 요구하므로 직접 사용 불가)
    """
    if not TELEGRAM_TOKEN:
        log.error("[텔레그램] TELEGRAM_TOKEN 미설정 — .env 에 TELEGRAM_TOKEN 을 추가하세요")
        return

    # 기존 웹훅/세션 정리
    try:
        _requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook",
            json={"drop_pending_updates": True}, timeout=10)
        _requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/close", timeout=10)
        import time as _time; _time.sleep(2)
    except Exception as e:
        log.debug(f"[텔레그램] 사전 정리: {e}")

    import threading as _threading
    app = _build_app()

    if _threading.current_thread() is _threading.main_thread():
        # 메인 스레드: run_polling() 직접 사용 (시그널 핸들러 정상 등록)
        log.info("[텔레그램] 봇 시작 — polling 대기 중... (메인 스레드)")
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    else:
        # 서브 스레드: 새 이벤트 루프 생성 후 비동기 polling 실행
        log.info("[텔레그램] 봇 시작 — polling 대기 중... (서브 스레드)")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run_polling_async(app))
        except Exception as e:
            log.error(f"[텔레그램] 봇 종료: {e}")
        finally:
            loop.close()


if __name__ == "__main__":
    run_bot()
