"""한국 오늘의 종목 -- 미국판(make_picks.py)과 같은 방식으로 전략별 상위 몇 개씩 뽑아 카드로 만든다.

전략끼리 점수 계산이 달라 합쳐서 줄 세우지 않고 전략별로 뽑아 합친다.
  1. 긴 조정 후 신고가  상위 4  (최종후보 우선 -> 못 넘은 항목이 적은 순 -> 점수 순)   <- kr_breakout.json
  2. 딥밸류             상위 3  (오늘 트리거가 켜진 것 우선 -> 종합점수 순)              <- kr_deepvalue.json
  3. 잠정실적 흑자전환  상위 3  (최근 3일 공시 중 영업이익이 전년 적자에서 흑자로, 거래대금 5억 원 이상, 매출 큰 순)
                                                                                       <- kr_disclosures.json (한국만의 항목)
같은 종목이 여러 전략에 걸리면 카드는 하나로 두고 태그를 모두 단다.
카드마다: 차트, 근거 지표, 기업개요(2줄), 최근 뉴스 3건.

결과: docs/data/kr_picks.json   (kr_deepvalue.yml 워크플로우 안에서, 딥밸류 다음에 돈다: 주가 캐시가 필요하다)
"""

from __future__ import annotations

import sys
import time
from datetime import timedelta

import pandas as pd

from common import DATA_DIR, ROOT, env, now_kst, read_json, write_json_compact

PX_CACHE = ROOT / "screener" / "kr_px_cache.pkl.gz"
TOP_BREAKOUT, TOP_DV, TOP_TURN = 4, 3, 3
NEWS_PER_PICK = 3
MIN_TURNOVER_KRW = 5e8


def breakout_card(x):
    return {"code": x["code"], "name": x["name"], "market": x["market"], "industry": x.get("sector"),
            "price": x.get("price"), "chg": x.get("chg"), "turnover_eok": x.get("turnover_eok"), "mcap_eok": None,
            "chart": x.get("chart") or [], "tags": ["신고가"],
            "meta": {"신고가": {"score": x.get("score"), "gates": x.get("gates"), "drawdown": x.get("drawdown"),
                              "high_age": x.get("high_age"), "vol_mult": x.get("vol_mult"), "rs": x.get("rs"),
                              "missing": x.get("missing"), "final": x.get("final")}}}


def dv_card(x):
    keep = ("fscore", "composite", "value", "turn", "dd_3y", "days_below", "norm_earn_yield", "ev_sales", "p_tbv",
            "fcf_yield", "net_debt_ebitda", "int_cov", "curr_ratio", "rev_yoy_q0", "triggers", "why", "f_parts")
    return {"code": x["code"], "name": x["name"], "market": x["market"], "industry": x.get("industry") or x.get("sector"),
            "price": x.get("price"), "chg": x.get("chg"), "turnover_eok": x.get("turnover_eok"), "mcap_eok": x.get("mcap_eok"),
            "chart": x.get("chart") or [], "tags": ["딥밸류"], "overview": x.get("overview") or [],
            "meta": {"딥밸류": {k: x.get(k) for k in keep}}}


def price_info(cache, ticker):
    """주가 캐시에서 차트·현재가·등락·20일 평균 거래대금(억). 없으면 None."""
    df = cache.get(ticker)
    if df is None or len(df) < 30:
        return None
    c = df["Close"].dropna().astype(float)
    v = df["Volume"].reindex(c.index).fillna(0).astype(float)
    if len(c) < 30:
        return None
    turnover = float((c.iloc[-20:] * v.iloc[-20:]).mean())
    return {"price": int(round(float(c.iloc[-1]))),
            "chg": round((float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100, 2) if float(c.iloc[-2]) else None,
            "turnover": turnover, "turnover_eok": round(turnover / 1e8),
            "chart": [[str(d.date()), int(round(float(x)))] for d, x in c.tail(252).items()]}


def turnaround_cards(items, cache, tick_of, today):
    cut = (today - timedelta(days=3)).isoformat()
    rows = [x for x in items if x.get("category") == "잠정실적" and x.get("date", "") >= cut
            and (x.get("numbers") or {}).get("op_tag") == "흑자전환"]
    rows.sort(key=lambda x: -((x["numbers"].get("revenue")) or 0))
    out = []
    for x in rows:
        t = tick_of.get(x["code"])
        info = price_info(cache, t) if t else None
        if not info or info["turnover"] < MIN_TURNOVER_KRW:
            continue
        n = x["numbers"]
        out.append({"code": x["code"], "name": x["corp"], "market": x["market"], "industry": None,
                    "price": info["price"], "chg": info["chg"], "turnover_eok": info["turnover_eok"], "mcap_eok": None,
                    "chart": info["chart"], "tags": ["실적 흑자전환"],
                    "meta": {"실적 흑자전환": {"date": x["date"], "period": n.get("period"), "basis": n.get("basis"),
                                            "revenue": n.get("revenue"), "revenue_yoy": n.get("revenue_yoy"),
                                            "op": n.get("op"), "op_prior": n.get("op_prior"),
                                            "net": n.get("net"), "summary": n.get("summary"), "url": x.get("url")}}})
        if len(out) >= TOP_TURN:
            break
    return out


def attach_news(picks):
    try:
        from fetch_news import gather
    except Exception as e:      # noqa: BLE001
        print(f"  뉴스 모듈 실패: {e}", file=sys.stderr)
        return
    creds = {"naver_id": env("NAVER_CLIENT_ID"), "naver_secret": env("NAVER_CLIENT_SECRET"),
             "gemini": env("GEMINI_API_KEY")}
    for p in picks:
        try:
            items = gather([p["name"]], creds, limit=NEWS_PER_PICK + 2, market="kr")
        except Exception as e:      # noqa: BLE001
            print(f"  {p['name']} 뉴스 실패: {str(e)[:60]}", file=sys.stderr)
            items = []
        p["news"] = [{k: it.get(k) for k in ("title", "url", "source", "published", "summary")}
                     for it in items[:NEWS_PER_PICK]]
        time.sleep(0.3)


def main():
    bo = (read_json(DATA_DIR / "kr_breakout.json") or {}).get("breakout") or []
    dv = (read_json(DATA_DIR / "kr_deepvalue.json") or {}).get("candidates") or []
    dis = (read_json(DATA_DIR / "kr_disclosures.json") or {}).get("items") or []
    print(f"입력: 신고가 후보 {len(bo)} · 딥밸류 후보 {len(dv)} · 공시 {len(dis)}")

    picks, by_code = [], {}

    def add(card):
        old = by_code.get(card["code"])
        if old:                                   # 이미 뽑힌 종목이면 태그·근거만 합친다
            old["tags"] += [t for t in card["tags"] if t not in old["tags"]]
            old["meta"].update(card["meta"])
            return
        by_code[card["code"]] = card
        picks.append(card)

    for x in sorted(bo, key=lambda x: (not x.get("final"), x.get("missing_n", 9), -(x.get("score") or 0)))[:TOP_BREAKOUT]:
        add(breakout_card(x))
    for x in sorted(dv, key=lambda x: (0 if x.get("trigger_count") else 1, -(x.get("composite") or 0)))[:TOP_DV]:
        add(dv_card(x))

    cache, tick_of = {}, {}
    if PX_CACHE.exists():
        try:
            cache = pd.read_pickle(PX_CACHE)
            from kr_breakout import get_universe
            u = get_universe(str(ROOT / "screener"))
            tick_of = dict(zip(u["코드"], u["티커"]))
        except Exception as e:      # noqa: BLE001
            print(f"  주가 캐시/종목 목록 실패: {e}", file=sys.stderr)
    for card in turnaround_cards(dis, cache, tick_of, now_kst().date()):
        add(card)

    # 신고가 카드에 빠진 값(등락 등)은 주가 캐시에서 채운다
    for p in picks:
        if p.get("chg") is None and tick_of.get(p["code"]):
            info = price_info(cache, tick_of[p["code"]])
            if info:
                p["chg"] = info["chg"]
                p["price"] = p["price"] or info["price"]

    try:
        from kr_overview import attach_overviews
        attach_overviews(picks)
    except Exception as e:      # noqa: BLE001
        print(f"  기업개요 실패: {type(e).__name__}: {e}", file=sys.stderr)
    attach_news(picks)

    groups = {}
    for p in picks:
        for t in p["tags"]:
            groups[t] = groups.get(t, 0) + 1
    write_json_compact(DATA_DIR / "kr_picks.json", {
        "updated_at": now_kst().isoformat(), "picks": picks, "groups": groups})
    print(f"저장 완료: 오늘의 종목 {len(picks)}개 · " + " · ".join(f"{k} {v}" for k, v in groups.items()))


if __name__ == "__main__":
    main()
