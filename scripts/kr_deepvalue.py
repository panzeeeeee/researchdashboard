#!/usr/bin/env python3
"""한국 딥밸류 -- 미국 딥밸류(scripts/deepvalue)의 판정 로직을 그대로 쓰고, 재무는 DART 에서 받는다.

단계 (미국판과 같다)
  0  가격·유동성·소외 필터 (dv_core.price_features + dv_metrics.stage0_pass)   -- 전 종목, 캐시된 주가로
  1  생존·희석 하드컷 (stage1_pass)                                          -- 재무 필요, 0단계 통과 종목만
  3  피오트로스키 F-Score 게이트 (piotroski, F>=6)
  4  종합 점수 (score_frame)
  5  일별 트리거 (apply_triggers)

한국용으로 바꾼 것 (숫자는 원 단위)
  - 최소 종가 1,000원, 20일 평균 거래대금 5억 원(한국 스크리너와 같은 기준), 시총 1,000억 원 이상 (미국판 $3 / $2M / $200M 을 옮긴 것에서 거래대금·시총을 한국 실정에 맞춰 낮춤)
  - 재무: 야후 대신 DART 전체 재무제표(kr_dv_data.py). 이자비용은 손익계산서 -> 현금흐름표 -> 금융비용 순으로 찾는다
  - 애널리스트 추정치 수정(revision_breadth, eps_trend)은 한국에 출처가 없어 비어 있다 -> 점수에서 빠지고 나머지로 재정규화
  - 미국판처럼 'ebit' 는 넣지 않는다 -> 이자보상배율이 정상화 EBIT(5년 중앙 마진 x 매출)로 계산된다(같은 기준 비교)

DART 가 느려서(연결 하나당 초당 약 15KB) 종목마다 몇 번씩 요청한다. 스레드 여러 개로 동시에 받고,
결과는 캐시에 남겨 다음 실행은 새 공시가 나온 종목만 받는다. 시간 예산(--budget-min)을 넘으면 남은 종목은
다음 실행으로 미룬다('fundamentals_pending').

결과: docs/data/kr_deepvalue.json
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "deepvalue"))
sys.path.insert(0, str(HERE))

from dv_core import Config, price_features                                        # noqa: E402
from dv_metrics import (apply_triggers, derive_metrics, piotroski, score_frame,   # noqa: E402
                        stage0_pass, stage1_pass)
from dv_run import load_prev_snapshot, save_snapshot                               # noqa: E402

import kr_dv_data as kd                                                            # noqa: E402
from common import DATA_DIR, ROOT, ask_gemini, env, now_kst, write_json           # noqa: E402
from kr_breakout import group_of                                                   # noqa: E402

PX_CACHE = ROOT / "screener" / "kr_px_cache.pkl.gz"
UNIV_DIR = ROOT / "screener" / "universe_cache"
OUT = DATA_DIR / "kr_deepvalue.json"
TOP_CAND = 60
TOP_NEAR = 60
CHART_DAYS = 252


def kr_cfg():
    return Config(
        min_price=1000.0,                 # 원
        min_dollar_vol_20d=5e8,           # 20일 평균 거래대금(원) -- 한국 스크리너와 같은 5억 원 (한국 소형주 거래 규모 반영)
        min_market_cap=1e11,              # 시총(원) 1,000억 -- 한국 소형주 실정에 맞춰 미국 환산값(2,700억)보다 낮춤
        max_market_cap=1e15,
        cache_dir=str(kd.CACHE_DIR),
        snapshot_dir=str(ROOT / "screener" / "dv_kr_snapshots"),
    )


def _f(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def r(x, nd=2, mult=1.0, cap=None):
    v = _f(x)
    if v is None:
        if isinstance(x, float) and x == float("inf"):
            return cap
        return None
    v *= mult
    if cap is not None and v > cap:
        v = cap
    return round(v, nd)


def write_compact(path, obj):
    """들여쓰기 없이 저장 -- 차트 좌표가 많아서 들여쓰기를 하면 파일이 3배로 커진다."""
    import json
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def load_universe():
    """KIND 상장법인 목록 (한국 스크리너와 같은 함수). 2초 안팎이라 매번 받고, 실패하면 캐시."""
    from kr_breakout import get_universe
    return get_universe(str(ROOT / "screener"))


def item(row, m, close_series):
    """JSON 에 담을 종목 한 건."""
    chart = None
    if close_series is not None:
        c = close_series.dropna().tail(CHART_DAYS)
        chart = [[str(d.date()), int(round(float(v)))] for d, v in c.items()]
    return {
        "code": row["코드"], "name": row["종목명"], "market": row["시장"], "sector": m.get("sector"),
        "industry": m.get("industry"), "price": r(m.get("price"), 0),
        "mcap_eok": r(m.get("market_cap"), 0, 1e-8),
        "composite": r(m.get("composite"), 3), "value": r(m.get("value_score"), 3), "turn": r(m.get("turn_score"), 3),
        "fscore": int(m["fscore"]) if _f(m.get("fscore")) is not None else None,
        "sector_rank": r(m.get("sector_rank"), 0),
        "dd_3y": r(m.get("dd_3y_high"), 0, 100), "days_below": m.get("days_below_200dma_1y"),
        "dist_200": r(m.get("dist_200dma"), 0, 100),
        "norm_earn_yield": r(m.get("norm_earn_yield"), 1, 100), "ev_sales": r(m.get("ev_sales"), 2),
        "ev_ebitda": r(m.get("ev_ebitda"), 1), "fcf_yield": r(m.get("fcf_yield"), 1, 100),
        "p_tbv": r(m.get("p_tangible_bv"), 2), "pb": r(m.get("pb"), 2),
        "net_debt_ebitda": r(m.get("net_debt_ebitda"), 1), "int_cov": r(m.get("interest_coverage"), 1, cap=999),
        "curr_ratio": r(m.get("current_ratio"), 2), "share_growth_3y": r(m.get("share_growth_3y"), 1, 100),
        "rev_cagr_5y": r(m.get("rev_cagr_5y"), 1, 100), "rev_yoy_q0": r(m.get("rev_yoy_q0"), 1, 100),
        "gm_q0": r(m.get("gm_q0"), 1, 100), "om_q0": r(m.get("om_q0"), 1, 100),
        "triggers": m.get("triggers") or "", "trigger_count": int(m.get("trigger_count") or 0),
        "f_parts": {k: int(m[k]) for k in m if str(k).startswith("f") and str(k)[1:2].isdigit() and "_" in str(k)
                    and _f(m.get(k)) is not None},
        "near_reason": m.get("near_reason"),
        "chart": chart,
    }


def add_why(rows):
    """'왜 싼가' 한 줄 -- 미국판과 같은 방식(Gemini 한 번 호출)."""
    key = env("GEMINI_API_KEY")
    if not key or not rows:
        return
    lines = "\n".join(
        f"{i+1}. {x['code']} {x['name']} / {x['industry']} / 3년고점대비 {x['dd_3y']}% / 200일선 아래 {x['days_below']}일 / "
        f"F-Score {x['fscore']} / 정상화이익수익률 {x['norm_earn_yield']}%" for i, x in enumerate(rows))
    prompt = ("아래는 딥밸류 스크리너가 뽑은 한국 주식이다. 각 종목이 왜 이렇게 싸게 거래되는지 한 문장으로 써라. "
              "35자 이내, 단정적 투자 의견은 쓰지 말고 통상 알려진 업황·구조 요인만. 확실하지 않으면 '확인 필요'라고 써라. "
              "설명 없이 '번호. 내용' 형식으로만 출력.\n\n" + lines)
    text = ask_gemini(prompt, key)
    if not text:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or "." not in line[:4]:
            continue
        head, _, body = line.partition(".")
        try:
            i = int(head.strip()) - 1
        except ValueError:
            continue
        if 0 <= i < len(rows) and body.strip():
            rows[i]["why"] = body.strip()


def main():
    ap = argparse.ArgumentParser(description="한국 딥밸류")
    ap.add_argument("--budget-min", type=float, default=45, help="재무 수집에 쓸 시간(분). 넘으면 남은 종목은 다음 실행으로")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="시험용: 0단계 통과 종목 중 앞에서 N개만")
    ap.add_argument("--debug", type=int, default=0, help="처음 N종목의 재무 요약을 probe_out/dv_debug.txt 에")
    args = ap.parse_args()

    cfg = kr_cfg()
    if not kd.env("DART_API_KEY"):
        print("DART_API_KEY 가 없습니다.")
        return 0
    if not PX_CACHE.exists():
        print(f"주가 캐시가 없습니다: {PX_CACHE} -- 한국 스크리너를 먼저 돌리세요.")
        return 1
    t0 = time.time()
    cache = pd.read_pickle(PX_CACHE)
    univ = load_universe()
    print(f"[0단계] 종목 {len(univ)}개 · 주가 캐시 {len(cache)}개")

    # ---- 0단계: 가격 필터
    px_map, rejects, closes = {}, [], {}
    for _, u in univ.iterrows():
        t = u["티커"]
        df = cache.get(t)
        if df is None or df["Close"].dropna().shape[0] < 260:
            rejects.append({"ticker": t, "stage": 0, "reason": "no_price_history"})
            continue
        c = df["Close"].dropna().astype(float)
        v = df["Volume"].reindex(c.index).fillna(0).astype(float)
        try:
            px = price_features(c, v)
        except Exception:
            px = None
        ok, why = stage0_pass(px, None, cfg)
        if not ok:
            rejects.append({"ticker": t, "stage": 0, "reason": why})
            continue
        px_map[t] = px
        closes[t] = c
    n_with_hist = len(univ) - sum(1 for x in rejects if x["reason"] == "no_price_history")
    print(f"      가격 히스토리 충분 {n_with_hist} -> 0단계 통과 {len(px_map)}")

    order = sorted(px_map, key=lambda t: px_map[t]["dd_3y_high"])         # 낙폭 큰 종목부터
    if args.limit:
        order = order[:args.limit]
    urow = univ.set_index("티커")

    # ---- 재무 수집 (스레드, 시간 예산)
    deadline = t0 + args.budget_min * 60
    print(f"[재무] {len(order)}종목 수집 (스레드 {args.workers}, 예산 {args.budget_min:.0f}분)")
    done = [0]

    def job(t):
        if time.time() > deadline:
            return t, None
        u = urow.loc[t]
        fd = kd.build_fd(u["코드"], u["종목명"], group_of(u["업종"]), u["업종"], px_map[t]["price"])
        done[0] += 1
        if done[0] % 20 == 0:
            print(f"      재무 {done[0]}/{len(order)} · {(time.time() - t0) / 60:.1f}분 · 요청 {kd.STATS['calls']} 캐시 {kd.STATS['cache_hits']}", flush=True)
        return t, fd

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        fmap = dict(ex.map(job, order))
    pending = sum(1 for v in fmap.values() if v is None)
    print(f"      수집 끝 · {(time.time() - t0) / 60:.1f}분 · 요청 {kd.STATS['calls']} 캐시 {kd.STATS['cache_hits']} 실패 {kd.STATS['fail']} · 미룬 종목 {pending}")

    # ---- 1~3단계
    rows, near_rows = [], []
    have_fd = []
    for t in order:
        fd = fmap.get(t)
        if fd is None:
            rejects.append({"ticker": t, "stage": 1, "reason": "fundamentals_pending"})
            continue
        if not fd.get("ok"):
            rejects.append({"ticker": t, "stage": 1, "reason": "fundamentals_unavailable:" + str(fd.get("error", ""))[:20]})
            continue
        have_fd.append(fd)
        px = px_map[t]
        if _f(fd.get("market_cap")) is None:          # 시총을 못 구하면 시총 필터를 그냥 통과하게 두지 않는다
            rejects.append({"ticker": t, "stage": 0, "reason": "no_market_cap"})
            continue
        ok, why = stage0_pass(px, fd.get("market_cap"), cfg)
        if not ok:
            rejects.append({"ticker": t, "stage": 0, "reason": why})
            continue
        m = derive_metrics(fd, px)
        f, comps = piotroski(fd)
        m["fscore"] = f
        m.update(comps)
        m["ticker"] = t
        m["_code"] = urow.loc[t]["코드"]
        ok, why = stage1_pass(m, cfg)
        if not ok:
            rejects.append({"ticker": t, "stage": 1, "reason": why})
            continue
        if f < cfg.min_fscore:
            m["near_reason"] = f"fscore_{int(f)}"
            near_rows.append(m)
            continue
        rows.append(m)
    print(f"[1~3단계] 재무 확보 {len(have_fd)} -> 후보 {len(rows)}, 근접(F<{cfg.min_fscore}) {len(near_rows)}")

    # ---- 재무 값 확보율 (검증용)
    cov = {}
    if have_fd:
        for k in ("rev_ttm", "gross_profit", "op_income", "ebitda", "net_income", "interest_exp", "total_assets",
                  "cur_assets", "cur_liab", "total_debt", "lt_debt", "cash", "equity", "inventory",
                  "cfo", "fcf_avg3", "shares", "shares_p1", "shares_p3", "market_cap", "rev_yoy_q0",
                  "ebit_margin_med5"):
            cov[k] = round(100 * sum(1 for x in have_fd if _f(x.get(k)) is not None) / len(have_fd))
        for k in ("q_gross_margin", "q_op_margin", "q_inventory"):
            cov[k] = round(100 * sum(1 for x in have_fd if x.get(k)) / len(have_fd))
        print("      재무 값 확보율(%): " + " · ".join(f"{k} {v}" for k, v in cov.items()))
        src = {}
        for x in have_fd:
            src[x.get("interest_exp_src")] = src.get(x.get("interest_exp_src"), 0) + 1
        print(f"      이자비용 출처: {src} · 연결 {sum(1 for x in have_fd if x.get('fs_div') == 'CFS')} 개별 {sum(1 for x in have_fd if x.get('fs_div') == 'OFS')}")

    if args.debug:
        p = ROOT / "probe_out"
        p.mkdir(exist_ok=True)
        keys = ["name", "fs_div", "fy", "q_latest", "rev_hist", "rev_ttm", "gross_profit", "op_income", "ebitda", "net_income",
                "interest_exp", "interest_exp_src", "total_assets", "cur_assets", "cur_liab", "total_debt", "lt_debt",
                "cash", "equity", "goodwill_intang", "inventory", "cfo", "capex", "fcf", "fcf_avg3", "buyback",
                "shares", "shares_p1", "shares_p3", "market_cap", "pb", "ebit_margin_med5", "q_rev", "q_ni",
                "q_gross_margin", "q_op_margin", "q_inventory", "rev_yoy_q0", "rev_yoy_q1"]
        with open(p / "dv_debug.txt", "w", encoding="utf-8") as fh:
            for fd in have_fd[:args.debug]:
                fh.write(f"=== {fd.get('ticker')} {fd.get('name')}\n")
                for k in keys:
                    fh.write(f"  {k}: {fd.get(k)}\n")
                m = derive_metrics(fd, px_map[urow.index[urow['코드'] == fd['ticker']][0]])
                f, comps = piotroski(fd)
                fh.write(f"  F-Score {f} {comps}\n")
                fh.write("  " + " · ".join(f"{k}={m.get(k)}" for k in (
                    "norm_earn_yield", "ev_sales", "ev_ebitda", "fcf_yield", "p_tangible_bv", "net_debt_ebitda",
                    "interest_coverage", "current_ratio", "share_growth_3y", "rev_cagr_5y", "equity_positive")) + "\n")
            fh.write(f"\n확보율: {cov}\n")
        print(f"      디버그 기록 -> probe_out/dv_debug.txt")

    # ---- 4~5단계
    cand = pd.DataFrame()
    trig_n = 0
    if rows:
        cand = score_frame(pd.DataFrame(rows), cfg)
        prev = load_prev_snapshot("kr", cfg)
        cand = apply_triggers(cand, prev, cfg)
        save_snapshot("kr", cand, cfg)
        trig_n = int((cand["trigger_count"] > 0).sum())
        print(f"[4~5단계] 후보 {len(cand)} · 트리거 발동 {trig_n}")

    def to_items(df, limit):
        out = []
        for _, row in df.head(limit).iterrows():
            m = row.to_dict()
            u = urow.loc[m["ticker"]]
            out.append(item(u.to_dict() | {"코드": u["코드"]}, m, closes.get(m["ticker"])))
        return out

    cand_items = to_items(cand, TOP_CAND) if not cand.empty else []
    near_df = pd.DataFrame(near_rows)
    if not near_df.empty:
        near_df = near_df.sort_values("fscore", ascending=False)
    near_items = to_items(near_df, TOP_NEAR) if not near_df.empty else []
    add_why(cand_items)

    rej = pd.DataFrame(rejects)
    rej_list = ([] if rej.empty else
                rej.groupby(["stage", "reason"]).size().reset_index(name="n").sort_values("n", ascending=False)
                .to_dict("records"))
    funnel = [["전체 종목", len(univ)], ["가격 히스토리 충분", n_with_hist], ["0단계 가격 필터 통과", len(px_map)],
              ["재무 확보", len(have_fd)], ["재무 수집 미룸(다음 실행)", pending],
              [f"생존·희석 통과 + F-Score {cfg.min_fscore}+ (후보)", len(rows)],
              ["근접(F-Score 미달)", len(near_rows)], ["오늘 트리거 발동", trig_n]]
    asof = str(closes[next(iter(closes))].index[-1].date()) if closes else None
    write_compact(OUT, {
        "updated_at": now_kst().isoformat(), "asof": asof, "funnel": funnel, "reject_reasons": rej_list[:25],
        "coverage": cov, "notes": {
            "cfg": {"min_price": cfg.min_price, "min_turnover_eok": cfg.min_dollar_vol_20d / 1e8,
                    "min_mcap_eok": cfg.min_market_cap / 1e8, "dd_3y": cfg.dd_from_3y_high, "min_fscore": cfg.min_fscore,
                    "days_below_200": cfg.min_days_below_200dma},
            "missing": "애널리스트 추정치 수정·공매도 자료는 한국에 출처가 없어 점수에서 빠집니다."},
        "candidates": cand_items, "near": near_items,
        "triggered": [x for x in cand_items if x["trigger_count"] > 0],
    })
    print(f"\n저장 완료 ({(time.time() - t0) / 60:.1f}분): 후보 {len(cand_items)} · 근접 {len(near_items)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
