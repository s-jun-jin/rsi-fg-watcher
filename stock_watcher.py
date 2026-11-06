"""
RSI 단일 지표 감시 -> Slack 알림 (매수 후보 감시용. 매도 판단은 직접 합니다)

동작:
  - 일봉 종가 기준 RSI(14)가 30 이하로 내려오면 "감시 시작" 알림을 1번 보냅니다.
  - 이후 RSI가 다시 32 위로 올라오면 "반등 확인" 알림을 1번 보냅니다.
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
    # ── 지수·레버리지 ETF (기존) ──
    "TQQQ": "TQQQ (나스닥100 3배)",
    "QLD": "QLD (나스닥100 2배)",
    "QQQ": "QQQ (나스닥100 1배)",
    "SOXL": "SOXL (반도체 3배)",
    "SOXX": "SOXX (반도체 1배)",
    "122630.KS": "KODEX 레버리지 (코스피200 2배)",
    "233740.KS": "KODEX 코스닥150레버리지 (2배)",

    # ── 정보기술 ──
    "NVDA": "엔비디아 [정보기술]",
    "AAPL": "애플 [정보기술]",
    "MSFT": "마이크로소프트 [정보기술]",
    "AVGO": "브로드컴 [정보기술]",
    "ORCL": "오라클 [정보기술]",

    # ── 커뮤니케이션 서비스 ──
    "GOOGL": "알파벳 [커뮤니케이션]",
    "META": "메타 [커뮤니케이션]",
    "NFLX": "넷플릭스 [커뮤니케이션]",
    "TMUS": "T-모바일 [커뮤니케이션]",
    "DIS": "디즈니 [커뮤니케이션]",

    # ── 경기소비재 ──
    "AMZN": "아마존 [경기소비재]",
    "TSLA": "테슬라 [경기소비재]",
    "HD": "홈디포 [경기소비재]",
    "MCD": "맥도날드 [경기소비재]",
    "BKNG": "부킹홀딩스 [경기소비재]",

    # ── 필수소비재 ──
    "WMT": "월마트 [필수소비재]",
    "COST": "코스트코 [필수소비재]",
    "PG": "P&G [필수소비재]",
    "KO": "코카콜라 [필수소비재]",
    "PM": "필립모리스 [필수소비재]",

    # ── 금융 ──
    "BRK-B": "버크셔해서웨이 [금융]",
    "JPM": "JP모건 [금융]",
    "V": "비자 [금융]",
    "MA": "마스터카드 [금융]",
    "BAC": "뱅크오브아메리카 [금융]",

    # ── 헬스케어 ──
    "LLY": "일라이릴리 [헬스케어]",
    "JNJ": "존슨앤존슨 [헬스케어]",
    "ABBV": "애브비 [헬스케어]",
    "MRK": "머크 [헬스케어]",
    "UNH": "유나이티드헬스 [헬스케어]",

    # ── 산업재 ──
    "GE": "GE에어로스페이스 [산업재]",
    "CAT": "캐터필러 [산업재]",
    "RTX": "RTX [산업재]",
    "GEV": "GE버노바 [산업재]",
    "UNP": "유니언퍼시픽 [산업재]",

    # ── 에너지 ──
    "XOM": "엑슨모빌 [에너지]",
    "CVX": "셰브론 [에너지]",
    "COP": "코노코필립스 [에너지]",
    "EOG": "EOG리소시스 [에너지]",
    "SLB": "슐럼버거 [에너지]",

    # ── 소재 ──
    "LIN": "린데 [소재]",
    "NEM": "뉴몬트 [소재]",
    "FCX": "프리포트맥모란 [소재]",
    "SHW": "셔윈윌리엄스 [소재]",
    "ECL": "이콜랩 [소재]",

    # ── 유틸리티 ──
    "NEE": "넥스트에라에너지 [유틸리티]",
    "SO": "서던컴퍼니 [유틸리티]",
    "DUK": "듀크에너지 [유틸리티]",
    "CEG": "컨스텔레이션에너지 [유틸리티]",
    "VST": "비스트라 [유틸리티]",

    # ── 부동산 ──
    "WELL": "웰타워 [부동산]",
    "PLD": "프롤로지스 [부동산]",
    "EQIX": "에퀴닉스 [부동산]",
    "AMT": "아메리칸타워 [부동산]",
    "SPG": "사이먼프로퍼티 [부동산]",
}

RSI_PERIOD = 14
RSI_WATCH = 30          # 이하 -> 감시 시작
RSI_RELEASE = 32        # 초과 -> 반등 확인 (30 근처에서 오르내릴 때 알림이 반복되지 않도록 여유를 둠)
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
    if in_watch and rsi_val > RSI_RELEASE:
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
        prev_day = prev.get("day") if isinstance(prev, dict) else None
        if prev_day and str(last_day) < prev_day:
            # 야후가 이전 실행보다 오래된 데이터를 돌려준 경우: 잘못된 RSI로 알림이 나가지 않게 건너뜀
            print(f"[STALE] {ticker}: 이전에 본 {prev_day}보다 오래된 데이터({last_day}) -> 이번 실행은 건너뜀")
            continue
        in_watch = isinstance(prev, dict) and bool(prev.get("in"))
        kind, new_in = decide(in_watch, rsi_val)
        mark = "감시중" if new_in else "-"
        print(f"{ticker:6s} {last_day} 종가 {price:9.2f} | RSI {rsi_val:5.1f} | {mark} | {kind or '알림없음'}")

        if kind:
            if kind == "ENTER":
                text = (f"🟡 *{name}* 감시 시작\n"
                        f"• RSI({RSI_PERIOD}) {rsi_val:.1f} (기준 {RSI_WATCH} 이하)\n"
                        f"• 현재가 {price:,.2f} ({last_day}, 장중 값은 마감 때 달라질 수 있음)\n"
                        "• 바로 매수가 아니라 지켜보는 구간입니다.")
            else:
                text = (f"🟢 *{name}* 반등 확인\n"
                        f"• RSI({RSI_PERIOD}) {rsi_val:.1f} (기준 {RSI_RELEASE} 위로 복귀)\n"
                        f"• 현재가 {price:,.2f} ({last_day}, 장중 값은 마감 때 달라질 수 있음)")
            try:
                send_slack(text, dry_run)
            except Exception as e:
                print(f"[ERROR] {ticker}: Slack 전송 실패 {type(e).__name__}")
                errors += 1
                continue  # 상태를 바꾸지 않아 다음 실행에서 다시 시도
        state[ticker] = {"in": new_in, "rsi": round(rsi_val, 1), "date": today, "day": str(last_day)}

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
