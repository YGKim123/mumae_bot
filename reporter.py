"""
reporter.py — 계좌별 이메일 리포트 생성 및 발송
"""

import os
import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import pytz

import config as cfg
import kis_api
import storage
from config import Account, setup_logging

log = setup_logging()


def build_html(acc: Account, report_data: dict) -> str:
    TD  = "border:1px solid black;border-collapse:collapse;padding:3px 6px;text-align:right;font-size:11px;"
    TDC = "border:1px solid black;border-collapse:collapse;padding:3px 6px;text-align:center;font-size:11px;"
    TH  = "border:1px solid black;border-collapse:collapse;padding:3px 6px;text-align:center;background-color:#F0F0F0;font-size:11px;"

    now    = report_data["now_kst"]
    cash   = report_data["cash"]
    ti     = report_data["total_invested"]
    tv     = report_data["total_value"]
    tp     = report_data["total_pnl"]
    tpp    = report_data["total_pnl_pct"]
    cp     = report_data["cumul_profit"]
    orders = report_data.get("order_log", [])

    trigger_label = {
        "daily": "📅 정규장 자동매매", "settlement": "🔍 체결 확인",
        "premarket": "⚡ 프리마켓 익절", "manual": "🖐️ 수동 실행",
    }.get(report_data.get("trigger", ""), "📊 현황 리포트")

    ticker_rows = ""
    for t in report_data["tickers"]:
        pnl_color  = "color:blue;" if t["pnl_pct"] >= 0 else "color:red;"
        half_label = "전반전" if t["t_val"] <= t["total_a"] / 2 else "후반전"
        target_gap = (t["target_p"] - t["curr"]) / t["curr"] * 100 if t["curr"] > 0 else 0
        ticker_rows += f"""
        <tr>
          <td style="{TDC}"><b>{t['ticker']}</b></td>
          <td style="{TD}">${t['seed']:,.0f}</td>
          <td style="{TDC}">{t['t_val']:.1f}T/{int(t['total_a'])}<br><small style="color:gray;">({half_label})</small></td>
          <td style="{TD}">${t['invested']:,.2f}</td>
          <td style="{TDC}">{t['qty']}주</td>
          <td style="{TD}">${t['avg']:.4f}</td>
          <td style="{TD}">${t['curr']:.4f}</td>
          <td style="{TD};{pnl_color}"><b>{t['pnl_pct']:+.2f}%</b></td>
          <td style="{TD}">${t['target_p']:.4f}<br><small style="color:gray;">({target_gap:+.1f}%)</small></td>
          <td style="{TD}">{t['star_pct']:.2f}%</td>
          <td style="{TD}">${t['curr_value']:,.2f}</td>
        </tr>"""

    order_html = ""
    for line in orders:
        line = line.strip()
        if not line:
            continue
        color = ("color:blue;" if "✅" in line or "성공" in line else
                 "color:red;"  if "❌" in line or "실패" in line else
                 "color:#555;font-weight:bold;" if line.startswith("[") else "")
        order_html += f'<p style="font-size:11px;line-height:19px;margin:1px 0;{color}">{line}</p>'
    if not order_html:
        order_html = '<p style="font-size:11px;color:gray;">주문 내역 없음</p>'

    cs = cp.get("SOXL", 0.0); ct = cp.get("TQQQ", 0.0); ctot = cs + ct
    tp_color = "blue" if tp >= 0 else "red"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body{{font-family:'Malgun Gothic',Arial,sans-serif;font-size:12px;color:#222;margin:10px 20px;}}
  p{{margin:2px 0;}} h3{{margin:16px 0 5px;font-size:13px;border-left:4px solid #4472C4;padding-left:8px;}}
  table{{border:2px solid black;border-collapse:collapse;}}
</style></head>
<body>
  <h3>{trigger_label} 리포트</h3>
  <p style="font-size:11px;color:gray;">계좌: [{acc.name}] {acc.cano} | 발송시각: {now} KST</p>

  <h3>💰 계좌 요약</h3>
  <table>
    <tr><th style="{TH}">항목</th><th style="{TH}">금액 (USD)</th></tr>
    <tr><td style="{TDC}">주문가능금액</td><td style="{TD};color:blue;"><b>${cash:,.2f}</b></td></tr>
    <tr><td style="{TDC}">총 투자금</td><td style="{TD}">${ti:,.2f}</td></tr>
    <tr><td style="{TDC}">총 평가금</td><td style="{TD}">${tv:,.2f}</td></tr>
    <tr><td style="{TDC}">평가 손익</td>
        <td style="{TD};color:{tp_color};"><b>${tp:+,.2f} ({tpp:+.2f}%)</b></td></tr>
  </table>

  <h3>📊 종목별 현황</h3>
  <table>
    <tr>
      <th style="{TH}">종목</th><th style="{TH}">시드($)</th>
      <th style="{TH}">진행</th><th style="{TH}">매입금</th>
      <th style="{TH}">보유량</th><th style="{TH}">평단가</th>
      <th style="{TH}">현재가</th><th style="{TH}">수익률</th>
      <th style="{TH}">목표가(gap)</th><th style="{TH}">별%</th>
      <th style="{TH}">평가금</th>
    </tr>{ticker_rows}
  </table>

  <h3>📋 주문 내역</h3>
  <div style="background:#f9f9f9;padding:10px;border:1px solid #ddd;border-radius:4px;">{order_html}</div>

  <h3>💵 누적 실현수익</h3>
  <table>
    <tr><th style="{TH}">종목</th><th style="{TH}">누적 수익 (USD)</th></tr>
    <tr><td style="{TDC}">SOXL</td><td style="{TD};color:{'blue' if cs>=0 else 'red'};">${cs:+,.2f}</td></tr>
    <tr><td style="{TDC}">TQQQ</td><td style="{TD};color:{'blue' if ct>=0 else 'red'};">${ct:+,.2f}</td></tr>
    <tr><td style="{TDC}"><b>합계</b></td><td style="{TD};color:{'blue' if ctot>=0 else 'red'};"><b>${ctot:+,.2f}</b></td></tr>
  </table>
  <p style="margin-top:24px;font-size:10px;color:#aaa;">자동 발송</p>
</body></html>"""


def collect_report_data(acc: Account, trigger: str, order_log: list = None) -> dict:
    kst     = pytz.timezone("Asia/Seoul")
    now_kst = datetime.datetime.now(kst).strftime("%Y-%m-%d %H:%M")
    tickers_data   = []
    total_invested = total_value = 0.0

    for ticker in acc.strategy:
        s     = acc.strategy[ticker]
        d     = s["data"]
        avg_p = d.get("avg", 0.0); qty_t = int(d.get("qty", 0))
        ota   = s["seed"] / s["total_a"]
        t_val = round(d.get("cumul", 0) / ota, 2) if ota > 0 and d.get("cumul", 0) > 0 else 0.0
        star_pct = round(s["target_profit"] * (1.0 - 2.0 * t_val / s["total_a"]), 2) if t_val > 0 else s["target_profit"]
        invested = avg_p * qty_t; total_invested += invested
        try:
            curr_p = kis_api.get_current_price(acc, ticker) if qty_t > 0 else 0.0
        except Exception:
            curr_p = 0.0
        curr_value   = curr_p * qty_t
        total_value += curr_value if curr_value > 0 else invested
        pnl_pct      = (curr_p - avg_p) / avg_p * 100 if avg_p > 0 else 0.0
        target_p     = avg_p * (1 + s["target_profit"] / 100) if avg_p > 0 else 0.0
        tickers_data.append({
            "ticker": ticker, "seed": s["seed"], "total_a": s["total_a"],
            "t_val": t_val, "avg": avg_p, "qty": qty_t, "curr": curr_p,
            "pnl_pct": pnl_pct, "target_p": target_p, "star_pct": star_pct,
            "invested": invested, "curr_value": curr_value,
        })

    total_pnl     = total_value - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0.0
    cp            = storage.load_cumul(acc)
    status_parts  = [f"{t['ticker']} {t['t_val']:.1f}T({t['pnl_pct']:+.1f}%)" for t in tickers_data]

    return {
        "subject_summary": " | ".join(status_parts),
        "now_kst": now_kst, "cash": acc.current_cash,
        "total_invested": total_invested, "total_value": total_value,
        "total_pnl": total_pnl, "total_pnl_pct": total_pnl_pct,
        "cumul_profit": {"SOXL": cp.get("SOXL", 0.0), "TQQQ": cp.get("TQQQ", 0.0)},
        "tickers": tickers_data, "order_log": order_log or [], "trigger": trigger,
    }


def send_email(acc: Account, subject: str, report_data: dict):
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    mail_to   = os.getenv("MAIL_TO", "")
    if not smtp_user or not smtp_pass or not mail_to:
        log.warning(f"[메일][{acc.name}] SMTP 설정 없음")
        return
    try:
        html_body = build_html(acc, report_data)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = smtp_user
        msg["To"]      = mail_to
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.ehlo(); server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [a.strip() for a in mail_to.split(",")], msg.as_string())
        log.info(f"[메일][{acc.name}] 발송 완료 → {mail_to}")
    except Exception as e:
        log.error(f"[메일][{acc.name}] 실패: {e}")


def send_report(acc: Account, trigger: str = "daily", order_log: list = None):
    data   = collect_report_data(acc, trigger, order_log)
    prefix = {"daily": "[자동매매]", "settlement": "[체결확인]",
               "premarket": "[프리마켓익절]", "manual": "[수동리포트]"}.get(trigger, "[리포트]")
    arrow  = "▲" if data["total_pnl"] >= 0 else "▼"
    subject = (f"{prefix}[{acc.name}] 잔고 ${data['cash']:,.0f} | "
               f"{data['subject_summary']} | 손익 {arrow}{abs(data['total_pnl_pct']):.1f}%")
    send_email(acc, subject, data)
