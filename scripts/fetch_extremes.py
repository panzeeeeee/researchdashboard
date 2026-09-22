"""52주 신고가·신저가 카드뷰 -- 오늘 딱 극값을 찍은 종목을 카드 하나씩으로.

신고가 스크리너가 남겨둔 가격 캐시(screener/us_px_cache.pkl.gz)에서 종가만
읽어 라인차트를 그린다. 캔들에 필요한 고가/저가는 캐시에 없다 -- 그건 이미
압축하기로 정한 결정이라 다시 안 건드린다.

종목 상세(섹터·산업·시총·기업개요)는 오늘 극값을 찍은 소수 종목에만 개별
조회한다 -- us_breakout.py 의 get_candidate_extra() 와 같은 방식. 전체
~2,000종목에 하면 느리지만, 오늘 극값 찍은 종목은 원래 적어서 괜찮다.

기업개요는 Gemini로 영문 소개를 한국어 불릿 2개로 줄인다. fetch_news.py 의
summarize() 처럼 한 번의 호출로 여러 종목을 한꺼번에 처리한다.

반드시 "신고가 스크리너" 다음, 가격 캐시가 아직 디스크에 있을 때 돌아야 한다.
"""

import sys
import time

import pandas as pd
import requests
import yfinance as yf

from common import DATA_DIR, ask_gemini, env, now_kst, write_json

CACHE_PATH = "screener/us_px_cache.pkl.gz"
LOOKBACK = 252     # 거래일 기준 약 1년
# 상한을 두지 않는다 -- 후보가 많은 날은 그만큼 이 단계가 오래 걸린다
# (종목당 개별 조회 0.15초+API 시간). continue-on-error라 전체 실행은 안 막는다.


def usd_krw_rate():
    try:
        h = yf.Ticker("KRW=X").history(period="5d")
        return float(h["Close"].dropna().iloc[-1])
    except Exception as e:
        print(f"환율 조회 실패: {e}", file=sys.stderr)
        return None


def fetch_universe_market_caps():
    """나스닥 공개 스크리너 -- 미국 상장 전종목 시총을 한 번에 받는다.

    필드 이름이 정확히 확인된 건 아니라 방어적으로 몇 가지 후보를 시도한다.
    """
    url = "https://api.nasdaq.com/api/screener/stocks"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; research-dashboard/1.0)",
        "Referer": "https://www.nasdaq.com/",
        "Accept-Language": "en-US,en;q=0.9",
    }
    caps = {}
    try:
        r = requests.get(url, headers=headers,
                          params={"tableonly": "true", "limit": 10000, "offset": 0},
                          timeout=30)
        r.raise_for_status()
        rows = r.json()["data"]["table"]["rows"]
    except Exception as e:
        print(f"나스닥 스크리너 실패: {e} -- 시총순위는 건너뜁니다.", file=sys.stderr)
        return caps

    for row in rows:
        try:
            symbol = str(row.get("symbol", "")).split(" ")[0].strip()
            raw = row.get("marketCap") or row.get("marketcap")
            if not symbol or raw in (None, "", "NA", "N/A"):
                continue
            caps[symbol] = float(str(raw).replace(",", "").replace("$", ""))
        except Exception:
            continue
    print(f"나스닥 스크리너: {len(caps)}종목 시총 확보")
    return caps


def market_cap_rank(caps, ticker, market_cap):
    """전체 유니버스 중 이 종목의 시총 순위 (1위가 제일 큼)."""
    if not caps:
        return None
    val = market_cap if market_cap is not None else caps.get(ticker)
    if val is None:
        return None
    return sum(1 for v in caps.values() if v > val) + 1


def find_extremes(px):
    """종가 기준으로 오늘이 딱 52주 최고/최저인 종목만 뽑는다."""
    highs, lows = [], []
    for t, df in px.items():
        try:
            c = df["Close"].dropna().tail(LOOKBACK)
            if len(c) < LOOKBACK:
                continue
            last = float(c.iloc[-1])
            hi, lo = float(c.max()), float(c.min())
            if last >= hi - 1e-9:
                highs.append(t)
            elif last <= lo + 1e-9:
                lows.append(t)
        except Exception:
            continue
    return highs, lows


def rank_by_dollar_volume(tickers, px):
    """전부 남기되, 오늘 거래대금이 큰(=더 눈에 띄는) 종목이 카드 앞쪽에 오게 정렬만 한다."""
    scored = []
    for t in tickers:
        try:
            c = px[t]["Close"].dropna()
            v = px[t]["Volume"].dropna()
            scored.append((float(c.iloc[-1]) * float(v.iloc[-1]), t))
        except Exception:
            continue
    scored.sort(reverse=True)
    return [t for _, t in scored]


def get_extra(ticker):
    try:
        info = yf.Ticker(ticker).info
    except Exception as e:
        print(f"  {ticker} 정보 실패: {e}", file=sys.stderr)
        return {}
    return {
        "name": info.get("shortName") or info.get("longName") or ticker,
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "marketCap": info.get("marketCap"),
        "summary": info.get("longBusinessSummary"),
    }


def batch_overviews(items, api_key):
    """(이름, 영문소개) 목록을 한 번의 Gemini 호출로 한국어 불릿 2개씩으로."""
    usable = [(i, name, summary) for i, (name, summary) in enumerate(items) if summary]
    result = [[] for _ in items]
    if not api_key or not usable:
        return result

    numbered = "\n\n".join(f"{i+1}. {name}: {summary[:500]}" for i, name, summary in usable)
    prompt = (
        "다음은 여러 기업의 영문 소개다. 각 기업을 한국어로 핵심만 2개의 짧은 불릿으로 "
        "요약해라. 형식은 한 줄에 '번호. 불릿1 | 불릿2', 설명 없이 그것만 출력해라.\n\n"
        + numbered
    )
    text = ask_gemini(prompt, api_key)
    if not text:
        return result

    for line in text.splitlines():
        line = line.strip()
        if not line or "." not in line[:4]:
            continue
        num, _, rest = line.partition(".")
        try:
            idx = int(num.strip()) - 1
        except ValueError:
            continue
        if 0 <= idx < len(items) and rest.strip():
            result[idx] = [b.strip() for b in rest.split("|") if b.strip()][:2]
    return result


def build_cards(tickers, px, kind, api_key, krw_rate, caps):
    extras = []
    for t in tickers:
        extras.append(get_extra(t))
        time.sleep(0.15)

    overviews = batch_overviews(
        [(e.get("name") or t, e.get("summary")) for t, e in zip(tickers, extras)], api_key)

    cards = []
    for t, extra, overview in zip(tickers, extras, overviews):
        c = px[t]["Close"].dropna().tail(LOOKBACK)
        v = px[t]["Volume"].dropna().tail(LOOKBACK)
        chart = [{"date": str(d.date()), "value": round(float(val), 2)} for d, val in c.items()]
        price = float(c.iloc[-1])
        prev = float(c.iloc[-2]) if len(c) > 1 else None
        chg = round((price / prev - 1) * 100, 2) if prev else None
        last_vol = float(v.iloc[-1]) if len(v) else None

        market_cap = extra.get("marketCap")
        trading_value = round(price * last_vol, 0) if last_vol else None

        cards.append({
            "ticker": t,
            "name": extra.get("name") or t,
            "kind": kind,
            "price": round(price, 2),
            "chg": chg,
            "sector": extra.get("sector"),
            "industry": extra.get("industry"),
            "marketCap": market_cap,
            "marketCapKrw": round(market_cap * krw_rate, 0) if market_cap and krw_rate else None,
            "tradingValue": trading_value,
            "tradingValueKrw": round(trading_value * krw_rate, 0) if trading_value and krw_rate else None,
            "capRank": market_cap_rank(caps, t, market_cap),
            "overview": overview,
            "chart": chart,
        })
    return cards


def main():
    try:
        px = pd.read_pickle(CACHE_PATH)
    except Exception as e:
        print(f"가격 캐시를 못 읽었습니다 ({e}) -- 건너뜁니다.", file=sys.stderr)
        return

    highs, lows = find_extremes(px)
    print(f"신고가 후보 {len(highs)}종목, 신저가 후보 {len(lows)}종목")
    highs = rank_by_dollar_volume(highs, px)
    lows = rank_by_dollar_volume(lows, px)

    api_key = env("GEMINI_API_KEY")
    krw_rate = usd_krw_rate()
    caps = fetch_universe_market_caps()
    high_cards = build_cards(highs, px, "high", api_key, krw_rate, caps)
    low_cards = build_cards(lows, px, "low", api_key, krw_rate, caps)

    write_json(DATA_DIR / "extremes.json", {
        "updated_at": now_kst().isoformat(),
        "high": high_cards,
        "low": low_cards,
    })
    print(f"저장 완료 (신고가 {len(high_cards)}장, 신저가 {len(low_cards)}장)")


if __name__ == "__main__":
    main()
