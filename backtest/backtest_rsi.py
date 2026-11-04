"""
RSI 단일 지표 백테스트: 미국 대표 지수 레버리지 ETF의 매수 타이밍

- 지표: RSI(14, Wilder). 각 ETF 자신의 가격으로 계산
- 가격: 야후 수정주가(배당/분할 반영)
- 체결: 신호는 종가 기준으로 확인하고, 다음 거래일 시가에 매수 (미래 정보 사용 방지)

진입 방식 두 가지
  DOWN: RSI가 기준값 위에서 아래로 내려오는 첫날 (떨어지는 칼날을 잡는 방식)
  UP  : RSI가 기준값 이하였다가 위로 올라오는 첫날 (반등을 확인하고 진입하는 방식)

1) 이벤트 스터디: 신호 후 5/20/60 거래일 보유했을 때의 수익률, 승률, 보유 중 최대 낙폭
2) 단순 전략: 신호 때 매수, RSI가 청산 기준 이상이 되면 다음날 시가에 매도 (최대 60일 보유)
"""
import math
import os
import sys
import time

import numpy as np
import pandas as pd
import yfinance as yf

# ───────────── 설정 ─────────────
TICKERS = {
    "TQQQ": "나스닥100 3배",
    "QLD": "나스닥100 2배",
    "UPRO": "S&P500 3배",
    "SPXL": "S&P500 3배",
    "SSO": "S&P500 2배",
    "UDOW": "다우 3배",
    "TNA": "러셀2000 3배",
}
RSI_PERIOD = 14
THRESHOLDS = [20, 25, 30, 35, 40]   # 진입 기준 RSI
HORIZONS = [5, 20, 60]              # 보유 기간(거래일)
KINDS = ["DOWN", "UP"]
COOLDOWN = 20                       # 같은 신호가 몰릴 때 중복 제거(거래일)
WARMUP = 40                         # RSI 초기 불안정 구간 제외
STRAT_ENTRY = [25, 30, 35]
STRAT_EXIT = [50, 60, 70]
MAX_HOLD = 60
# ────────────────────────────────

REPORT: list[str] = []


def say(line: str = "") -> None:
    REPORT.append(line)


def rsi_wilder(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def fetch(ticker: str):
    for attempt in range(3):
        try:
            df = yf.Ticker(ticker).history(period="max", auto_adjust=True)
            df = df[["Open", "High", "Low", "Close"]].dropna()
            if len(df) > 300:
                return df
        except Exception as e:
            print(f"[WARN] {ticker}: {type(e).__name__} (재시도 {attempt + 1}/3)")
        time.sleep(3)
    return None


def is_hit(prev: float, cur: float, x: float, kind: str) -> bool:
    if math.isnan(prev) or math.isnan(cur):
        return False
    if kind == "DOWN":
        return prev > x and cur <= x
    return prev <= x and cur > x


def events(v: np.ndarray, x: float, kind: str) -> list[int]:
    out, last = [], -10**9
    for i in range(max(1, WARMUP), len(v)):
        if is_hit(v[i - 1], v[i], x, kind) and i - last >= COOLDOWN:
            out.append(i)
            last = i
    return out


def fwd(O, C, L, i: int, h: int):
    """i일 종가에 신호 -> i+1일 시가 매수 -> h거래일째 종가 평가"""
    e = i + 1
    if e + h - 1 >= len(O):
        return None
    entry = O[e]
    return C[e + h - 1] / entry - 1, L[e:e + h].min() / entry - 1


def summarize(rows):
    if not rows:
        return None
    r = np.array([a for a, _ in rows])
    m = np.array([b for _, b in rows])
    return {
        "n": len(r),
        "win": (r > 0).mean() * 100,
        "mean": r.mean() * 100,
        "med": float(np.median(r)) * 100,
        "p10": float(np.percentile(r, 10)) * 100,
        "mdd": m.mean() * 100,
    }


def run_strategy(df: pd.DataFrame, x: float, y: float, kind: str) -> dict:
    O, C, L = df["Open"].values, df["Close"].values, df["Low"].values
    v = df["RSI"].values
    n = len(df)
    trades, i = [], max(1, WARMUP)
    while i < n - 1:
        if not is_hit(v[i - 1], v[i], x, kind):
            i += 1
            continue
        e = i + 1
        entry = O[e]
        j = e
        while True:
            held = j - e + 1
            if j >= n - 1:
                exit_px, last_day = C[j], j
                break
            if v[j] >= y:
                exit_px, last_day = O[j + 1], j
                break
            if held >= MAX_HOLD:
                exit_px, last_day = C[j], j
                break
            j += 1
        trades.append(
            (exit_px / entry - 1, L[e:last_day + 1].min() / entry - 1, last_day - e + 1)
        )
        i = last_day + 1
    start = max(1, WARMUP) + 1
    bh = C[-1] / O[start] - 1
    peak = np.maximum.accumulate(C[start:])
    bh_mdd = (C[start:] / peak - 1).min()
    if not trades:
        return {"n": 0, "bh": bh * 100, "bh_mdd": bh_mdd * 100}
    r = np.array([t[0] for t in trades])
    return {
        "n": len(trades),
        "win": (r > 0).mean() * 100,
        "mean": r.mean() * 100,
        "med": float(np.median(r)) * 100,
        "hold": float(np.mean([t[2] for t in trades])),
        "worst": r.min() * 100,
        "worst_mdd": min(t[1] for t in trades) * 100,
        "total": (np.prod(1 + r) - 1) * 100,
        "expo": sum(t[2] for t in trades) / (n - start) * 100,
        "bh": bh * 100,
        "bh_mdd": bh_mdd * 100,
    }


def f(x, d=1, sign=False) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def table(headers: list[str], rows: list[list[str]]) -> None:
    say("| " + " | ".join(headers) + " |")
    say("|" + "|".join(["---"] * len(headers)) + "|")
    for r in rows:
        say("| " + " | ".join(r) + " |")
    say()


def main() -> None:
    data = {}
    for t in TICKERS:
        df = fetch(t)
        if df is None:
            print(f"[ERROR] {t}: 데이터 조회 실패")
            continue
        df = df.copy()
        df["RSI"] = rsi_wilder(df["Close"])
        data[t] = df
    if not data:
        print("[FATAL] 모든 종목 조회에 실패했습니다.")
        sys.exit(1)

    say("# RSI 단일 지표 백테스트 (미국 대표 지수 레버리지 ETF)")
    say()
    say(f"RSI({RSI_PERIOD}) 기준, 신호 다음 거래일 시가 매수, 중복 신호 제거 {COOLDOWN}거래일. 수정주가 사용.")
    say()
    table(
        ["ETF", "추종", "데이터 시작", "데이터 끝", "거래일 수"],
        [[t, TICKERS[t], str(d.index[0].date()), str(d.index[-1].date()), str(len(d))] for t, d in data.items()],
    )

    per: dict = {}
    for t, df in data.items():
        O, C, L = df["Open"].values, df["Close"].values, df["Low"].values
        v = df["RSI"].values
        for kind in KINDS:
            for x in THRESHOLDS:
                idx = events(v, x, kind)
                for h in HORIZONS:
                    per[(t, kind, x, h)] = [r for i in idx if (r := fwd(O, C, L, i, h)) is not None]
        for h in HORIZONS:
            per[(t, "ALL", 0, h)] = [
                r for i in range(WARMUP, len(df) - 1) if (r := fwd(O, C, L, i, h)) is not None
            ]

    def pooled(kind, x, h):
        rows = []
        for t in data:
            rows += per[(t, kind, x, h)]
        return summarize(rows)

    say("## 1. 전체 ETF 합산 이벤트 스터디")
    say()
    say("승률=보유 종료 시점 수익 비율, 평균최대낙폭=보유 중 최저가 기준 평균 낙폭, 하위10%=수익률 하위 10% 경계값. 단위 %.")
    say()
    for h in HORIZONS:
        base = pooled("ALL", 0, h)
        for kind in KINDS:
            name = "하향 돌파(DOWN)" if kind == "DOWN" else "상향 돌파(UP)"
            say(f"### 보유 {h}거래일 / 진입 {name}")
            say()
            rows = []
            for x in THRESHOLDS:
                s = pooled(kind, x, h)
                if s:
                    rows.append([f"RSI {x}", str(s["n"]), f(s["win"], 0), f(s["mean"], 1, True),
                                 f(s["med"], 1, True), f(s["p10"], 1, True), f(s["mdd"], 1, True)])
            rows.append(["(기준) 아무 날이나 매수", str(base["n"]), f(base["win"], 0), f(base["mean"], 1, True),
                         f(base["med"], 1, True), f(base["p10"], 1, True), f(base["mdd"], 1, True)])
            table(["진입 기준", "신호 수", "승률", "평균", "중앙값", "하위10%", "평균최대낙폭"], rows)

    say("## 2. ETF별 이벤트 스터디 (셀: 신호수 / 승률% / 중앙값%)")
    say()
    for h in (20, 60):
        for kind in KINDS:
            name = "하향 돌파(DOWN)" if kind == "DOWN" else "상향 돌파(UP)"
            say(f"### 보유 {h}거래일 / 진입 {name}")
            say()
            rows = []
            for t in data:
                row = [t]
                for x in THRESHOLDS:
                    s = summarize(per[(t, kind, x, h)])
                    row.append(f"{s['n']} / {f(s['win'], 0)} / {f(s['med'], 1, True)}" if s else "-")
                b = summarize(per[(t, "ALL", 0, h)])
                row.append(f"{f(b['win'], 0)} / {f(b['med'], 1, True)}" if b else "-")
                rows.append(row)
            table(["ETF"] + [f"RSI {x}" for x in THRESHOLDS] + ["(기준) 아무 날이나: 승률 / 중앙값"], rows)

    say("## 3. 단순 전략 (신호 시 매수 -> RSI가 청산 기준 이상이면 다음날 시가 매도, 최대 60일)")
    say()
    say("ETF 평균: 각 ETF 결과를 단순 평균. 총수익은 매매를 이어서 복리로 계산, 비교용 단순보유는 같은 기간 매수 후 보유.")
    say()
    strat: dict = {}
    for t, df in data.items():
        for kind in KINDS:
            for x in STRAT_ENTRY:
                for y in STRAT_EXIT:
                    strat[(t, kind, x, y)] = run_strategy(df, x, y, kind)

    rows = []
    for kind in KINDS:
        for x in STRAT_ENTRY:
            for y in STRAT_EXIT:
                rs = [strat[(t, kind, x, y)] for t in data if strat[(t, kind, x, y)]["n"] > 0]
                if not rs:
                    continue
                avg = lambda k: float(np.mean([r[k] for r in rs]))
                rows.append([kind, f"{x}", f"{y}", f(avg("n"), 0), f(avg("win"), 0), f(avg("mean"), 1, True),
                             f(avg("med"), 1, True), f(avg("hold"), 0), f(avg("worst"), 1, True),
                             f(avg("worst_mdd"), 1, True), f(avg("expo"), 0), f(avg("total"), 0, True)])
    bh_rows = [strat[(t, "DOWN", 30, 50)] for t in data]
    table(["진입", "진입RSI", "청산RSI", "매매수", "승률", "평균", "중앙값", "평균보유일", "최악매매", "최악낙폭", "시장참여%", "총수익%"], rows)
    say(f"(참고) 단순보유 평균 총수익 {f(float(np.mean([r['bh'] for r in bh_rows])), 0, True)}%, "
        f"평균 최대낙폭 {f(float(np.mean([r['bh_mdd'] for r in bh_rows])), 0, True)}%")
    say()

    say("## 4. ETF별 단순 전략 상세")
    say()
    rows = []
    for t in data:
        for kind in KINDS:
            for x in STRAT_ENTRY:
                for y in STRAT_EXIT:
                    s = strat[(t, kind, x, y)]
                    if s["n"] == 0:
                        continue
                    rows.append([t, kind, str(x), str(y), str(s["n"]), f(s["win"], 0), f(s["mean"], 1, True),
                                 f(s["worst"], 1, True), f(s["worst_mdd"], 1, True), f(s["total"], 0, True),
                                 f(s["bh"], 0, True)])
    table(["ETF", "진입", "진입RSI", "청산RSI", "매매수", "승률", "평균", "최악매매", "최악낙폭", "총수익%", "단순보유%"], rows)

    say("## 한계")
    say()
    say("- 레버리지 ETF는 상장 이후(약 2009~2010년부터)만 있어 대부분 상승장이었고, 생존한 상품만 포함된 결과입니다.")
    say("- 극단적 기준(RSI 20 등)은 신호 수가 적어 통계 신뢰도가 낮습니다. 신호 수를 꼭 함께 보세요.")
    say("- 세금, 수수료, 슬리피지는 반영하지 않았습니다. 과거 성과가 미래를 보장하지 않으며 투자 권유가 아닙니다.")

    text = "\n".join(REPORT)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(text + "\n")


if __name__ == "__main__":
    main()
