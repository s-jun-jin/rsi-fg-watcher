"""
RSI 단일 지표 감시 -> Slack 알림 (매수 후보 감시용. 매도 판단은 직접 합니다)

동작:
  - 일봉 종가 기준 RSI(14)가 30 이하로 내려오면 "감시 시작" 알림을 1번 보냅니다.
  - 이후 RSI가 다시 30 위로 올라오면 "반등 확인" 알림을 1번 보냅니다.
  - 감시 중에는 같은 알림을 반복해서 보내지 않습니다.

실행:  export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/XXX/YYY/ZZZ"
       python stock_watcher.py            # 한 번 검사
       python stock_watcher.py --dry-run  # Slack 전송 없이 콘솔 출력만
"""
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

# ───────────── 설정 ─────────────
WATCHLIST = {
    "TQQQ": "TQQQ (나스닥100 3배)",
    "QLD": "QLD (나스닥100 2배)",
    "QQQ": "QQQ (나스닥100 1배)",
    "SOXL": "SOXL (반도체 3배)",
    "SOXX": "SOXX (반도체 1배)",
}
RSI_PERIOD = 14
RSI_WATCH = 30          # 이하 -> 감시 시작, 다시 초과 -> 반등 확인
MAX_STALE_DAYS = 6      # 마지막 종가가 이보다 오래되면 데이터 오류로 처리

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
STATE_FILE = Path(__file__).with_name("watcher_state.json")
# ────────────────────────────────


def rsi_series(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder RSI"""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def decide(in_watch: bool, rsi_val: float) -> tuple[str | None, bool]:
    """(이번에 보낼 알림 종류, 새 감시 상태). 종류: ENTER / REBOUND / None"""
    if not in_watch and rsi_val <= RSI_WATCH:
        return "ENTER", True
    if in_watch and rsi_val > RSI_WATCH:
        return "REBOUND", False
    return None, in_watch


def send_slack(text: str, dry_run: bool) -> None:
    if dry_run:
        print("[SLACK]", text, "\n")
        return
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError("SLACK_WEBHOOK_URL 환경변수가 설정되지 않았습니다.")
    requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10).raise_for_status()


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    if os.environ.get("SLACK_TEST") == "true":
        send_slack("✅ RSI 감시 테스트 메시지입니다. Slack 연결이 정상입니다.", dry_run)
        print("[TEST] 테스트 메시지를 보냈습니다.")
        return
    today = date.today().isoformat()
    state = load_state()
    errors = 0

    for ticker, name in WATCHLIST.items():
        try:
            close = yf.Ticker(ticker).history(period="1y")["Close"].dropna()
            if len(close) < RSI_PERIOD * 3:
                raise ValueError("가격 데이터가 부족합니다")
            last_day = close.index[-1].date()
            if date.today() - last_day > timedelta(days=MAX_STALE_DAYS):
                raise ValueError(f"마지막 종가가 오래됨({last_day})")
            rsi_val = float(rsi_series(close).iloc[-1])
            price = float(close.iloc[-1])
        except Exception as e:
            print(f"[ERROR] {ticker}: {e}")
            errors += 1
            continue

        prev = state.get(ticker)
        in_watch = isinstance(prev, dict) and bool(prev.get("in"))
        kind, new_in = decide(in_watch, rsi_val)
        mark = "감시중" if new_in else "-"
        print(f"{ticker:6s} {last_day} 종가 {price:9.2f} | RSI {rsi_val:5.1f} | {mark} | {kind or '알림없음'}")

        if kind:
            if kind == "ENTER":
                text = (f"🟡 *{name}* 감시 시작\n"
                        f"• RSI({RSI_PERIOD}) {rsi_val:.1f} (기준 {RSI_WATCH} 이하)\n"
                        f"• 종가 {price:,.2f} ({last_day})\n"
                        "• 바로 매수가 아니라 지켜보는 구간입니다.")
            else:
                text = (f"🟢 *{name}* 반등 확인\n"
                        f"• RSI({RSI_PERIOD}) {rsi_val:.1f} (기준 {RSI_WATCH} 위로 복귀)\n"
                        f"• 종가 {price:,.2f} ({last_day})")
            try:
                send_slack(text, dry_run)
            except Exception as e:
                print(f"[ERROR] {ticker}: Slack 전송 실패 {type(e).__name__}")
                errors += 1
                continue  # 상태를 바꾸지 않아 다음 실행에서 다시 시도
        state[ticker] = {"in": new_in, "rsi": round(rsi_val, 1), "date": today}

    if errors and state.get("_warn") != today:
        try:
            send_slack(f"⚠️ RSI 감시 오류: {errors}/{len(WATCHLIST)}건 실패 (GitHub Actions 로그 확인).\n"
                       "같은 날 반복 경고는 보내지 않습니다.", dry_run)
            state["_warn"] = today
        except Exception as e:
            print(f"[WARN] 오류 경고 전송 실패: {type(e).__name__}")

    save_state(state)

    if errors == len(WATCHLIST):
        print("[FATAL] 모든 종목 처리에 실패했습니다.")
        sys.exit(1)


if __name__ == "__main__":
    main()
