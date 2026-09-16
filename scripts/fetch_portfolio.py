"""보유 종목 현황과 규칙 트리거까지 남은 거리를 계산한다.

금액이나 수량은 다루지 않는다. 가격 움직임과 기준선까지의 거리만 본다.
"""

import sys

from common import DATA_DIR, load_config, now_kst, write_json


def download_all(tickers, period="1y", tries=3):
    """보유 종목 전체를 한 번에 받는다.

    종목마다 따로 요청하면 야후가 429(요청 과다)로 막는다.
    바로 앞 단계에서 스크리너가 수천 종목을 이미 긁은 뒤라 더 그렇다.
    """
    import time
    import yfinance as yf

    for i in range(tries):
        try:
            df = yf.download(tickers, period=period, auto_adjust=False,
                             progress=False, group_by="column", threads=False)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            print(f"  내려받기 실패 ({i+1}/{tries}): {e}", file=sys.stderr)
        if i < tries - 1:
            wait = 20 * (i + 1)
            print(f"  {wait}초 쉬었다 다시 시도합니다.")
            time.sleep(wait)
    return None


def close_series(df, ticker, single):
    """한 번에 받은 표에서 종목 하나의 종가를 꺼낸다."""
    try:
        col = df["Close"] if single else df["Close"][ticker]
        return col.dropna()
    except Exception:
        return None


def pct(a, b):
    if not a or not b:
        return None
    return round((a / b - 1) * 100, 2)


def snapshot(s, ticker):
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

    tickers = [h["ticker"] for h in (cfg.get("holdings") or []) if h.get("ticker")]
    if bench not in tickers:
        tickers.append(bench)

    df = download_all(tickers)
    if df is None:
        print("가격을 받지 못했습니다. 기존 자료를 그대로 둡니다.", file=sys.stderr)
        return

    single = len(tickers) == 1
    rows = []
    for t in tickers:
        snap = snapshot(close_series(df, t, single), t)
        if snap:
            rows.append(snap)
            print(f"  {t}: {snap['price']} ({snap['d1']}%)")
        else:
            print(f"  {t}: 자료 없음", file=sys.stderr)

    bench_row = next((r for r in rows if r["ticker"] == bench), None)
    rows = [r for r in rows if r["ticker"] in
            {h["ticker"] for h in (cfg.get("holdings") or []) if h.get("ticker")}]

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
