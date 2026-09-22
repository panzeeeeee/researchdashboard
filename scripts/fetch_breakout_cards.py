"""긴 조정 후 52주 신고가 -- 카드뷰.

신고가·신저가 카드(fetch_extremes.py)와 같은 방식으로, tables.json 의
breakout 후보(물극필반 게이트 통과 종목)에 차트·기업정보·기업개요를 붙인다.

기존 표 페이지(#breakout, t-breakout)는 그대로 둔다 -- 같은 후보를 카드
형식으로도 보여주는 것뿐이다.

"결과 표 생성"(tables.json 을 만드는 단계) 다음에 돌아야 한다. 가격 캐시는
워크플로우가 끝날 때까지 디스크에 남아있어서, 순서는 크게 안 가려도 된다.

후보가 많은 날은 종목마다 개별 조회(야후 정보+Gemini 개요)가 늘어나 오래
걸릴 수 있다 -- 상한을 두지 않기로 했다 (신저가 카드와 같은 이유).
"""

import sys
import time

import pandas as pd
import yfinance as yf

from common import DATA_DIR, ask_gemini, env, now_kst, read_json, write_json

CACHE_PATH = "screener/us_px_cache.pkl.gz"
LOOKBACK = 252


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


def build_card(t, fallback_name, extra, overview, px, meta):
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

    return {
        "ticker": t,
        "name": extra.get("name") or fallback_name or t,
        "price": round(price, 2),
        "chg": chg,
        "sector": extra.get("sector"),
        "industry": extra.get("industry"),
        "marketCap": extra.get("marketCap"),
        "tradingValue": round(price * last_vol, 0) if last_vol else None,
        "overview": overview,
        "chart": chart,
        "meta": meta,  # 점수·게이트 등 스크리너 고유 정보
    }


def main():
    tables = read_json(DATA_DIR / "tables.json")
    if not tables:
        print("tables.json 이 아직 없습니다. 건너뜁니다.", file=sys.stderr)
        return

    candidates = tables.get("breakout") or []
    tickers = [c["code"] for c in candidates if c.get("code")]
    if not tickers:
        write_json(DATA_DIR / "breakout_cards.json",
                   {"updated_at": now_kst().isoformat(), "cards": []})
        print("오늘 신고가 후보가 없습니다.")
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
    overviews = batch_overviews(
        [(e.get("name") or t, e.get("summary")) for t, e in zip(tickers, extras)], api_key)

    by_code = {c["code"]: c for c in candidates}
    cards = []
    for t, extra, overview in zip(tickers, extras, overviews):
        c = by_code.get(t, {})
        meta = {"score": c.get("score"), "gates": c.get("gates"),
                "drawdown": c.get("drawdown"), "missing": c.get("missing")}
        card = build_card(t, c.get("name"), extra, overview, px, meta)
        if card:
            cards.append(card)

    write_json(DATA_DIR / "breakout_cards.json", {
        "updated_at": now_kst().isoformat(),
        "cards": cards,
    })
    print(f"저장 완료 ({len(cards)}장)")


if __name__ == "__main__":
    main()
