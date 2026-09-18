"""US 섹터 브레드스 — S&P500 11개 섹터의 상승/하락 종목수, 20일 거래량 배수.

신고가 스크리너가 이미 받아둔 가격 캐시(screener/us_px_cache.pkl.gz)를 그대로
읽는다. 새로 야후를 부르지 않는다.

섹터 매핑은 위키백과 S&P500 목록의 GICS Sector 열을 쓴다 — us_breakout.py의
get_spx()가 이미 긁는 바로 그 표라, 새 데이터 소스가 아니다. SPDR 섹터 ETF
홀딩스 CSV도 검토했지만 정확한 주소를 확인 못 해서 이 방식으로 갔다.

S&P500 구성종목만 커버한다. 나스닥100·러셀2000 전용 종목(S&P500에 없는)은
섹터가 안 붙는다 — 대표성엔 문제없다고 판단해 범위를 좁혔다.

반드시 "신고가 스크리너" 다음, 워크플로우가 가격 캐시를 다시 저장하기 전에
돌아야 한다 (아래 CACHE_PATH가 아직 디스크에 있어야 함).
"""

import io
import sys

import pandas as pd
import requests

from common import DATA_DIR, now_kst, write_json

CACHE_PATH = "screener/us_px_cache.pkl.gz"
_HDRS = {"User-Agent": "Mozilla/5.0 (screener; personal research)"}

# fetch_market.py 의 섹터 라벨과 통일 — GICS Sector 영문명 -> 한글 라벨
GICS_TO_LABEL = {
    "Information Technology": "기술",
    "Financials": "금융",
    "Health Care": "헬스케어",
    "Consumer Discretionary": "경기소비재",
    "Consumer Staples": "필수소비재",
    "Energy": "에너지",
    "Industrials": "산업재",
    "Materials": "소재",
    "Utilities": "유틸리티",
    "Real Estate": "부동산",
    "Communication Services": "통신서비스",
}


def get_sp500_sectors():
    """위키백과 S&P500 목록에서 티커->섹터 매핑을 받는다 (get_spx()와 같은 표)."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    html = requests.get(url, headers=_HDRS, timeout=30).text
    tables = pd.read_html(io.StringIO(html))
    df = next(t for t in tables
              if "Symbol" in t.columns and "GICS Sector" in t.columns and len(t) > 400)
    tickers = (df["Symbol"].astype(str).str.strip().str.upper()
               .str.replace(".", "-", regex=False))
    sectors = df["GICS Sector"].astype(str).str.strip()
    return dict(zip(tickers, sectors))


def main():
    try:
        px = pd.read_pickle(CACHE_PATH)
    except Exception as e:
        print(f"가격 캐시를 못 읽었습니다 ({e}) — 건너뜁니다.", file=sys.stderr)
        return

    try:
        tick_sector = get_sp500_sectors()
    except Exception as e:
        print(f"S&P500 섹터 목록을 못 받았습니다 ({e}) — 건너뜁니다.", file=sys.stderr)
        return

    rows = []
    for t, sec_en in tick_sector.items():
        label = GICS_TO_LABEL.get(sec_en)
        if not label or t not in px:
            continue
        try:
            df = px[t]
            c, v = df["Close"].dropna(), df["Volume"].dropna()
            if len(c) < 21 or len(v) < 21 or not c.iloc[-2]:
                continue
            chg = float(c.iloc[-1] / c.iloc[-2] - 1)
            vol_avg20 = float(v.iloc[-21:-1].mean())
            vol_mult = float(v.iloc[-1]) / vol_avg20 if vol_avg20 else None
            rows.append({"sector": label, "chg": chg, "vol_mult": vol_mult})
        except Exception:
            continue

    if not rows:
        print("종목 매칭이 하나도 안 됐습니다 — 저장하지 않습니다.", file=sys.stderr)
        return

    df = pd.DataFrame(rows)
    out = []
    for label in GICS_TO_LABEL.values():
        sub = df[df["sector"] == label]
        if sub.empty:
            continue
        vol = sub["vol_mult"].dropna()
        out.append({
            "name": label,
            "count": int(len(sub)),
            "up": int((sub["chg"] > 0).sum()),
            "down": int((sub["chg"] < 0).sum()),
            "vol_mult": round(float(vol.median()), 2) if len(vol) else None,
        })
    out.sort(key=lambda r: -(r["up"] - r["down"]))

    write_json(DATA_DIR / "sector_breadth.json", {
        "updated_at": now_kst().isoformat(),
        "universe": "S&P500",
        "sectors": out,
    })
    print(f"저장 완료 ({len(out)}개 섹터, {len(df)}종목)")


if __name__ == "__main__":
    main()
