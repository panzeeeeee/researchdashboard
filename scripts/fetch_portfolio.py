"""보유 종목 현황과 규칙 트리거까지 남은 거리를 계산한다.

금액이나 수량은 다루지 않는다. 가격 움직임과 기준선까지의 거리만 본다.
"""

import sys

from common import DATA_DIR, load_config, now_kst, write_json


def series(ticker, period="1y"):
    import yfinance as yf
    try:
        df = yf.Ticker(ticker).history(period=period, auto_adjust=False)
        return df["Close"].dropna() if not df.empty else None
    except Exception as e:
        print(f"  {ticker} 실패: {e}", file=sys.stderr)
        return None


def pct(a, b):
    if not a or not b:
        return None
    return round((a / b - 1) * 100, 2)


def snapshot(ticker):
    s = series(ticker)
    if s is None or len(s) < 30:
        return None
    last = float(s.iloc[-1])
    high52 = float(s.max())
    low52 = float(s.min())
    pos = None
    if high52 > low52:
        pos = round((last - low52) / (high52 - low52) * 100)
    return {
        "ticker": ticker,
        "price": round(last, 2),
        "d1": pct(last, float(s.iloc[-2])),
        "m1": pct(last, float(s.iloc[-22])) if len(s) > 22 else None,
        "from_high": pct(last, high52),
        "pos52": pos,
    }


def main():
    cfg = load_config("portfolio.yaml")
    trig = cfg.get("triggers") or {}
    bench = trig.get("benchmark", "SPY")
    levels = trig.get("levels") or [-10]

    rows = []
    for h in cfg.get("holdings") or []:
        t = h.get("ticker")
        if not t:
            continue
        snap = snapshot(t)
        if snap:
            rows.append(snap)
            print(f"  {t}: {snap['price']} ({snap['d1']}%)")

    bench_row = next((r for r in rows if r["ticker"] == bench), None) \
        or snapshot(bench)

    ladder = []
    if bench_row and bench_row["from_high"] is not None:
        dd = bench_row["from_high"]          # 음수
        for lv in sorted(levels, reverse=True):
            ladder.append({
                "level": lv,
                "hit": dd <= lv,
                # 그 단계까지 추가로 더 빠져야 하는 폭
                "gap": round(abs(lv - dd), 2) if dd > lv else 0,
            })

    write_json(DATA_DIR / "portfolio.json", {
        "updated_at": now_kst().isoformat(),
        "benchmark": bench,
        "benchmark_dd": bench_row["from_high"] if bench_row else None,
        "ladder": ladder,
        "holdings": sorted(rows, key=lambda r: r["from_high"] or 0),
    })
    print(f"저장 완료 ({len(rows)}종목)")


if __name__ == "__main__":
    main()
