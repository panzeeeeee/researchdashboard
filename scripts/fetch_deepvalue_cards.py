"""딥밸류 -- 카드뷰.

신고가·신저가 카드(fetch_extremes.py)와 같은 방식으로, tables.json 의
deepvalue 후보에 차트·기업정보·기업개요를 붙인다. "왜 싼가"는 이미 있는
값을 그대로 가져온다 (새로 만들지 않는다).

기존 표 페이지(#deepvalue, t-deepvalue)는 그대로 둔다 -- 같은 후보를 카드
형식으로도 보여주는 것뿐이다.

"결과 표 생성" 다음에 돌아야 한다. 가격 캐시는 워크플로우가 끝날 때까지
디스크에 남아있어서, 순서는 크게 안 가려도 된다.

후보가 많은 날은 종목마다 개별 조회가 늘어나 오래 걸릴 수 있다 -- 상한을
두지 않기로 했다.
"""

import sys
import time

import pandas as pd
import requests
import yfinance as yf

from common import DATA_DIR, ask_gemini, env, now_kst, read_json, write_json

CACHE_PATH = "screener/us_px_cache.pkl.gz"
LOOKBACK = 252


def usd_krw_rate():
    try:
        h = yf.Ticker("KRW=X").history(period="5d")
        return float(h["Close"].dropna().iloc[-1])
    except Exception as e:
        print(f"환율 조회 실패: {e}", file=sys.stderr)
        return None


def fetch_universe_market_caps():
    """나스닥 공개 스크리너 -- 미국 상장 전종목 시총을 한 번에."""
    url = "https://api.nasdaq.com/api/screener/stocks"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; research-dashboard/1.0)",
               "Referer": "https://www.nasdaq.com/", "Accept-Language": "en-US,en;q=0.9"}
    caps = {}
    try:
        r = requests.get(url, headers=headers,
                         params={"tableonly": "true", "limit": 10000, "offset": 0}, timeout=30)
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
    if not caps:
        return None
    val = market_cap if market_cap is not None else caps.get(ticker)
    if val is None:
        return None
    return sum(1 for v in caps.values() if v > val) + 1


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


def build_card(t, fallback_name, extra, overview, px, meta, krw_rate, caps):
    if t not in px:
        return None
    c = px[t]["Close"].dropna().tail(LOOKBACK)
    if len(c) < 2:
        return None
    v = px[t]["Volume"].dropna().tail(LOOKBACK)
    chart = [{"date": str(d.date()), "value": round(float(val), 2)} for d, val in c.items()]
    price = float(c.iloc[-1])
    prev = float(c.iloc[-2])
    chg = round((price / prev - 1) * 100, 2) if prev else None
    last_vol = float(v.iloc[-1]) if len(v) else None

    market_cap = extra.get("marketCap")
    trading_value = round(price * last_vol, 0) if last_vol else None
    return {
        "ticker": t,
        "name": extra.get("name") or fallback_name or t,
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
        "meta": meta,
    }


def main():
    tables = read_json(DATA_DIR / "tables.json")
    if not tables:
        print("tables.json 이 아직 없습니다. 건너뜁니다.", file=sys.stderr)
        return

    candidates = tables.get("deepvalue") or []
    tickers = [c["code"] for c in candidates if c.get("code")]
    if not tickers:
        write_json(DATA_DIR / "deepvalue_cards.json",
                   {"updated_at": now_kst().isoformat(), "cards": []})
        print("오늘 딥밸류 후보가 없습니다.")
        return

    try:
        px = pd.read_pickle(CACHE_PATH)
    except Exception as e:
        print(f"가격 캐시를 못 읽었습니다 ({e}) -- 건너뜁니다.", file=sys.stderr)
        return

    extras = []
    for t in tickers:
        extras.append(get_extra(t))
        time.sleep(0.15)

    api_key = env("GEMINI_API_KEY")
    krw_rate = usd_krw_rate()
    caps = fetch_universe_market_caps()
    overviews = batch_overviews(
        [(e.get("name") or t, e.get("summary")) for t, e in zip(tickers, extras)], api_key)

    by_code = {c["code"]: c for c in candidates}
    cards = []
    for t, extra, overview in zip(tickers, extras, overviews):
        c = by_code.get(t, {})
        meta = {"score": c.get("score"), "fscore": c.get("fscore"),
                "drawdown": c.get("drawdown"), "below200": c.get("below200"),
                "why": c.get("why"), "triggered": c.get("triggered")}
        card = build_card(t, c.get("name"), extra, overview, px, meta, krw_rate, caps)
        if card:
            cards.append(card)

    write_json(DATA_DIR / "deepvalue_cards.json", {
        "updated_at": now_kst().isoformat(),
        "cards": cards,
    })
    print(f"저장 완료 ({len(cards)}장)")


if __name__ == "__main__":
    main()
