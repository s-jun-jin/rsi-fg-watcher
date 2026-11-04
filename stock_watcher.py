"""
RSI + 공포탐욕지수 기반 종목 감시 → Slack 알림

설치:  pip install yfinance pandas requests
실행:  export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/XXX/YYY/ZZZ"
       python stock_watcher.py            # 한 번 검사
       python stock_watcher.py --dry-run  # Slack 전송 없이 콘솔 출력만

감시 종목:
  환경변수 WATCHLIST_JSON 이 있으면 그것을 사용하고, 없으면 아래 예시 종목을 사용합니다.
  형식: {"티커": ["표시이름", "stock 또는 crypto"], ...}
  예)   {"005930.KS": ["삼성전자", "stock"], "NVDA": ["NVIDIA", "stock"]}
  WATCHLIST_JSON 을 쓸 때는 공개 로그에 종목이 드러나지 않도록 종목명 대신 #번호로 출력합니다.
"""
import json
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

# ───────────── 설정 ─────────────
# ticker: (표시이름, 공포탐욕 소스)   소스: "stock" = CNN, "crypto" = alternative.me
DEFAULT_WATCHLIST = {
    "005930.KS": ("삼성전자", "stock"),
    "NVDA": ("NVIDIA", "stock"),
    "COIN": ("Coinbase", "crypto"),
}

RSI_PERIOD = 14
RSI_OVERSOLD = 30      # 이하 → 과매도
RSI_OVERBOUGHT = 70    # 이상 → 과매수
FG_FEAR = 25           # 이하 → 극단적 공포
FG_GREED = 75          # 이상 → 극단적 탐욕
REQUIRE_BOTH = True    # True: RSI AND 공포탐욕 동시 충족 / False: 둘 중 하나만 충족해도 알림

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
STATE_FILE = Path(__file__).with_name("watcher_state.json")  # 같은 날 중복 알림 방지
# ────────────────────────────────


def load_watchlist() -> tuple[dict, bool]:
    """(감시목록, 비공개 여부). WATCHLIST_JSON 이 있으면 비공개 모드."""
    raw = os.environ.get("WATCHLIST_JSON", "").strip()
    if not raw:
        return DEFAULT_WATCHLIST, False
    try:
        data = json.loads(raw)
        watchlist = {}
        for ticker, (name, src) in data.items():
            if src not in ("stock", "crypto"):
                raise ValueError
            watchlist[str(ticker)] = (str(name), src)
        if not watchlist:
            raise ValueError
    except Exception:
        # 값이 공개 로그에 남지 않도록 내용은 출력하지 않음
        print("[FATAL] WATCHLIST_JSON 형식이 올바르지 않습니다.")
        sys.exit(2)
    return watchlist, True


def rsi(close: pd.Series, period: int = RSI_PERIOD) -> float:
    """Wilder RSI (최신값)"""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss
    return float((100 - 100 / (1 + rs)).iloc[-1])


def fear_greed_stock() -> tuple[float, str]:
    r = requests.get(
        "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
    )
    r.raise_for_status()
    fg = r.json()["fear_and_greed"]
    return float(fg["score"]), fg["rating"]


def fear_greed_crypto() -> tuple[float, str]:
    r = requests.get("https://api.alternative.me/fng/", timeout=10)
    r.raise_for_status()
    d = r.json()["data"][0]
    return float(d["value"]), d["value_classification"]


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def judge(rsi_val: float, fg_val: float) -> str | None:
    rsi_buy, rsi_sell = rsi_val <= RSI_OVERSOLD, rsi_val >= RSI_OVERBOUGHT
    fg_buy, fg_sell = fg_val <= FG_FEAR, fg_val >= FG_GREED
    combine = (lambda a, b: a and b) if REQUIRE_BOTH else (lambda a, b: a or b)
    if combine(rsi_buy, fg_buy):
        return "OVERSOLD"
    if combine(rsi_sell, fg_sell):
        return "OVERBOUGHT"
    return None


def send_slack(text: str, dry_run: bool) -> None:
    if dry_run:
        print("[SLACK]", text, "\n")
        return
    if not SLACK_WEBHOOK_URL:
        # 시크릿 누락을 조용히 넘기지 않고 실행을 실패시켜 GitHub 실패 알림을 받게 함
        raise RuntimeError("SLACK_WEBHOOK_URL 환경변수가 설정되지 않았습니다.")
    requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10).raise_for_status()


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    today = date.today().isoformat()
    state = load_state()
    watchlist, private = load_watchlist()

    fg_cache: dict[str, tuple[float, str]] = {}
    fg_fetchers = {"stock": fear_greed_stock, "crypto": fear_greed_crypto}
    errors = 0

    for i, (ticker, (name, fg_src)) in enumerate(watchlist.items(), 1):
        label = f"#{i}" if private else name  # 비공개 모드: 로그에 종목명을 남기지 않음

        try:
            if fg_src not in fg_cache:
                fg_cache[fg_src] = fg_fetchers[fg_src]()
        except Exception as e:  # 종목 정보가 없는 오류라 그대로 출력해도 안전
            print(f"[ERROR] 공포탐욕지수({fg_src}) 조회 실패: {e}")
            errors += 1
            continue
        fg_val, fg_label = fg_cache[fg_src]

        try:
            close = yf.Ticker(ticker).history(period="6mo")["Close"].dropna()
            if len(close) < RSI_PERIOD + 1:
                raise ValueError("가격 데이터가 부족합니다")
            rsi_val = rsi(close)
            price = float(close.iloc[-1])
        except Exception as e:  # 한 종목 실패가 전체를 막지 않도록
            detail = type(e).__name__ if private else f"{ticker}: {e}"
            print(f"[ERROR] {label}: {detail}")
            errors += 1
            continue

        signal = judge(rsi_val, fg_val)
        print(f"{label:12s} RSI {rsi_val:5.1f} | F&G({fg_src}) {fg_val:5.1f} {fg_label} | {signal or '-'}")

        if not signal or state.get(ticker) == f"{today}:{signal}":
            continue

        emoji, text = ("🟢", "과매도 + 공포 (매수 감시)") if signal == "OVERSOLD" else ("🔴", "과매수 + 탐욕 (과열 주의)")
        send_slack(
            f"{emoji} *{name}* ({ticker}) {text}\n"
            f"• 현재가: {price:,.2f}\n"
            f"• RSI({RSI_PERIOD}): {rsi_val:.1f}\n"
            f"• 공포탐욕지수({fg_src}): {fg_val:.0f} ({fg_label})",
            dry_run,
        )
        state[ticker] = f"{today}:{signal}"

    # 일부라도 조회에 실패했다면 하루 한 번 Slack으로 알려서 "전부 감시 중"이라는 착각을 막음
    if errors and state.get("_warn") != today:
        try:
            send_slack(
                f"⚠️ 종목 감시 오류: {errors}/{len(watchlist)}건 조회 실패 (GitHub Actions 로그 확인).\n"
                "같은 날 반복 경고는 보내지 않습니다.",
                dry_run,
            )
            state["_warn"] = today
        except Exception as e:
            print(f"[WARN] 오류 경고 전송 실패: {type(e).__name__}")

    save_state(state)

    # 전 종목이 실패하면(데이터 소스 차단·형식 변경 등) 실행을 실패 처리 → GitHub가 메일로 알려줌
    if errors == len(watchlist):
        print("[FATAL] 모든 종목 조회에 실패했습니다.")
        sys.exit(1)


if __name__ == "__main__":
    main()
