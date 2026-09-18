"""미국 시장 전체 현황 — 지수, 섹터 ETF, 금리, 환율, 원자재, 대형주.

기존 스크리너·포트폴리오 캐시는 건드리지 않는다. 그날그날 값만 받아서 버린다.
"""

import csv
import io
import sys
import time
import urllib.request

import yfinance as yf

from common import DATA_DIR, now_kst, write_json

INDEXES = [("^GSPC", "S&P 500"), ("^IXIC", "나스닥 종합"), ("^RUT", "러셀 2000")]
BREADTH = [("RSP", "RSP"), ("SPY", "SPY")]

SECTORS = [
    ("XLK", "기술"), ("XLY", "경기소비재"), ("XLU", "유틸리티"),
    ("XLE", "에너지"), ("XLB", "소재"), ("XLV", "헬스케어"),
    ("XLRE", "부동산"), ("XLP", "필수소비재"), ("XLI", "산업재"),
    ("XLF", "금융"), ("XLC", "통신서비스"),
]

MEGACAP = [
    ("NVDA", "NVIDIA"), ("AVGO", "Broadcom"), ("TSLA", "Tesla"),
    ("AMZN", "Amazon"), ("MSFT", "Microsoft"), ("AAPL", "Apple"),
    ("META", "Meta"), ("GOOGL", "Alphabet"),
]

FX = [("KRW=X", "달러/원"), ("JPY=X", "달러/엔"), ("EURUSD=X", "유로/달러")]
COMMOD = [("CL=F", "WTI 선물"), ("GC=F", "금 선물"), ("HG=F", "구리 선물")]
CRYPTO = [("BTC-USD", "비트코인")]
LEVELS = [("DX-Y.NYB", "달러지수"), ("^VIX", "VIX")]

ALL_TICKERS = (INDEXES + BREADTH + SECTORS + MEGACAP + FX + COMMOD + CRYPTO + LEVELS)


def download_all(tickers, period="5d", tries=3):
    """전체 티커를 한 번에 받는다.

    포트폴리오 스크립트와 같은 이유 — 따로따로 요청하면 야후가 429로 막는다.
    """
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
    try:
        col = df["Close"] if single else df["Close"][ticker]
        return col.dropna()
    except Exception:
        return None


def snap(df, ticker, name, single):
    s = close_series(df, ticker, single)
    if s is None or len(s) < 2:
        return {"ticker": ticker, "name": name, "price": None, "chg": None}
    last, prev = float(s.iloc[-1]), float(s.iloc[-2])
    chg = round((last / prev - 1) * 100, 2) if prev else None
    return {"ticker": ticker, "name": name, "price": round(last, 2), "chg": chg}


def by_chg_desc(rows):
    return sorted(rows, key=lambda r: (r["chg"] is None, -(r["chg"] or 0)))


def fred_latest(series_id):
    """FRED 공개 CSV. API 키 없이 최근 관측값만 읽는다."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            text = r.read().decode("utf-8")
        rows = list(csv.reader(io.StringIO(text)))
        for date, val in reversed(rows[1:]):
            if val not in ("", "."):
                return round(float(val), 2), date
    except Exception as e:
        print(f"  FRED {series_id} 실패: {e}", file=sys.stderr)
    return None, None


def main():
    tickers = [t for t, _ in ALL_TICKERS]
    df = download_all(tickers)
    if df is None:
        print("가격을 받지 못했습니다. 기존 자료를 그대로 둡니다.", file=sys.stderr)
        return

    single = len(tickers) == 1
    row = lambda t, n: snap(df, t, n, single)

    indexes = [row(t, n) for t, n in INDEXES]

    b_rsp, b_spy = row("RSP", "RSP"), row("SPY", "SPY")
    breadth_spread = None
    if b_rsp["chg"] is not None and b_spy["chg"] is not None:
        breadth_spread = round(b_rsp["chg"] - b_spy["chg"], 2)

    sectors = by_chg_desc([row(t, n) for t, n in SECTORS])
    megacap = by_chg_desc([row(t, n) for t, n in MEGACAP])
    fx = [row(t, n) for t, n in FX]
    commod = [row(t, n) for t, n in COMMOD]
    crypto = [row(t, n) for t, n in CRYPTO]

    dxy = row("DX-Y.NYB", "달러지수")
    vix = row("^VIX", "VIX")
    dgs2, dgs2_date = fred_latest("DGS2")
    dgs10, dgs10_date = fred_latest("DGS10")

    risk = [
        {"label": "미 국채 2년", "value": dgs2, "unit": "%", "date": dgs2_date},
        {"label": "미 국채 10년", "value": dgs10, "unit": "%", "date": dgs10_date},
        {"label": "달러지수", "value": dxy["price"], "unit": "", "date": None},
        {"label": "VIX", "value": vix["price"], "unit": "", "date": None},
    ]

    write_json(DATA_DIR / "market.json", {
        "updated_at": now_kst().isoformat(),
        "indexes": indexes,
        "breadth_spread": breadth_spread,
        "sectors": sectors,
        "risk": risk,
        "megacap": megacap,
        "fx": fx,
        "commodities": commod,
        "crypto": crypto,
    })
    print("저장 완료")


if __name__ == "__main__":
    main()
