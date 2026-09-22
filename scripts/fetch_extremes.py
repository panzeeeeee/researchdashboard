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
import yfinance as yf

from common import DATA_DIR, ask_gemini, env, now_kst, write_json

CACHE_PATH = "screener/us_px_cache.pkl.gz"
LOOKBACK = 252     # 거래일 기준 약 1년
# 상한을 두지 않는다 -- 후보가 많은 날은 그만큼 이 단계가 오래 걸린다
# (종목당 개별 조회 0.15초+API 시간). continue-on-error라 전체 실행은 안 막는다.


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


def build_cards(tickers, px, kind, api_key):
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
        last_vol = float(v.iloc[-1]) if len(v) else None

        cards.append({
            "ticker": t,
            "name": extra.get("name") or t,
            "kind": kind,
            "price": round(price, 2),
            "sector": extra.get("sector"),
            "industry": extra.get("industry"),
            "marketCap": extra.get("marketCap"),
            "tradingValue": round(price * last_vol, 0) if last_vol else None,
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
    high_cards = build_cards(highs, px, "high", api_key)
    low_cards = build_cards(lows, px, "low", api_key)

    write_json(DATA_DIR / "extremes.json", {
        "updated_at": now_kst().isoformat(),
        "high": high_cards,
        "low": low_cards,
    })
    print(f"저장 완료 (신고가 {len(high_cards)}장, 신저가 {len(low_cards)}장)")


if __name__ == "__main__":
    main()
