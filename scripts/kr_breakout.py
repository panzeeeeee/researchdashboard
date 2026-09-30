#!/usr/bin/env python3
"""한국 스크리너 -- 코스피·코스닥 52주 신고가 x 물극필반 (us_breakout.py 의 판정 로직 재사용).

미국판과 같은 것  : 신고가 판정, 게이트(G1~G3), 트리거(거래량·정배열·RS·신선도), 점수 -- 전부 us_breakout 의 함수를 그대로 부른다.
한국용으로 바꾼 것
  - 종목 목록 : KRX KIND 상장법인 목록(시장구분·업종 포함). 유가증권=.KS, 코스닥=.KQ. 코넥스는 뺀다.
  - 시장 최종 거래일 : SPY 대신 코스피 지수(^KS11)
  - 하드필터 : 최소 종가 1,000원, 20일 평균 거래대금 5억 원 (시총 필터는 뺐다 -- 시총 자료가 없다)
  - RS 벤치마크 : 코스피는 ^KS11, 코스닥은 ^KQ11 (시장별로 따로 순위)
  - 실적 임박 배제는 뺐다 (야후의 한국 실적일이 부정확)

결과 : docs/data/kr_breakout.json  (긴 조정 후 신고가 후보 + 52주 신고가·신저가 요약)
주가 캐시 : <workdir>/kr_px_cache.pkl.gz  (워크플로우가 Actions 캐시로 보관)
장 마감(한국시간 16시) 전에 돌리면 오늘 봉이 미완성이라 멈춘다. 손으로 시험하려면 KR_FORCE=1.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import sys
import time

import numpy as np
import pandas as pd
import requests

import us_breakout as ub
from common import DATA_DIR, env, now_kst, write_json

KIND_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
UA = {"User-Agent": "Mozilla/5.0 (compatible; research-dashboard/1.0)"}
MARKETS = {"유가": ("코스피", ".KS", "^KS11"), "코스닥": ("코스닥", ".KQ", "^KQ11")}
BENCHES = ["^KS11", "^KQ11"]
CACHE_NAME = "kr_px_cache.pkl.gz"

MIN_PRICE_KRW = 1000
MIN_TURNOVER_KRW = 5e8          # 20일 평균 거래대금
NEAR_MISS = 2                   # 미달 항목이 이 이하면 근접후보로 보여준다
MAX_CANDIDATES = 80
MAX_EXTREME_NAMES = 60
CHART_DAYS = 252


# ---------------------------------------------------------------- 종목 목록

def get_universe(workdir):
    """KIND 에서 받고, 실패하면 가장 최근 캐시를 쓴다."""
    folder = os.path.join(workdir, "universe_cache")
    os.makedirs(folder, exist_ok=True)
    today = os.path.join(folder, f"kr_{dt.date.today():%Y%m%d}.csv")
    if os.path.exists(today):
        return pd.read_csv(today, dtype={"코드": str})
    try:
        r = requests.get(KIND_URL, headers=UA, timeout=40)
        r.raise_for_status()
        raw = pd.read_html(io.StringIO(r.content.decode("euc-kr", errors="replace")))[0]
        df = raw[raw["시장구분"].isin(MARKETS)].copy()
        df["코드"] = df["종목코드"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
        df = df[df["코드"].str.fullmatch(r"\d{6}")]
        df["시장"] = df["시장구분"].map(lambda m: MARKETS[m][0])
        df["티커"] = [c + MARKETS[m][1] for c, m in zip(df["코드"], df["시장구분"])]
        df["종목명"] = df["회사명"].astype(str).str.strip()
        df["업종"] = df["업종"].astype(str).str.strip()
        out = df[["티커", "코드", "종목명", "시장", "업종"]].drop_duplicates("티커").reset_index(drop=True)
        if len(out) < 1500:
            raise RuntimeError(f"종목이 너무 적음({len(out)})")
        out.to_csv(today, index=False)
        return out
    except Exception as e:
        olds = sorted(f for f in os.listdir(folder) if f.startswith("kr_"))
        if not olds:
            raise RuntimeError(f"KIND 목록 실패, 캐시도 없음: {e}") from e
        print(f"  ⚠ KIND 실패({type(e).__name__}) -- 캐시 {olds[-1]} 사용")
        return pd.read_csv(os.path.join(folder, olds[-1]), dtype={"코드": str})


# ---------------------------------------------------------------- 미국판 함수 교체

def kr_last_session() -> dt.date:
    import yfinance as yf
    d = yf.download("^KS11", period="15d", auto_adjust=True, progress=False, threads=False)
    if d is None or d.empty:
        raise RuntimeError("코스피 지수 조회 실패 -- 네트워크/yfinance 상태 확인 필요")
    return pd.Timestamp(d.index[-1]).date()


def kr_is_tradable(df: pd.DataFrame):
    c, v = df["Close"], df["Volume"]
    if len(c) < 60:
        return False, "데이터부족"
    if float(c.iloc[-1]) < MIN_PRICE_KRW:
        return False, f"저가주({float(c.iloc[-1]):,.0f}원)"
    if int((v.iloc[-60:].fillna(0) <= 0).sum()) > ub.CFG["MAX_ZERO_VOL"]:
        return False, "거래정지(거래량0)"
    if int(c.iloc[-60:].round(4).nunique()) < ub.CFG["MIN_UNIQ_CLOSE"]:
        return False, "가격정지(종가일직선)"
    turnover = float((c.iloc[-20:] * v.iloc[-20:]).mean())
    if not np.isfinite(turnover) or turnover < MIN_TURNOVER_KRW:
        return False, f"저유동성({turnover/1e8:.1f}억)"
    return True, ""


# ---------------------------------------------------------------- 결과 정리

def _f(x, nd=2):
    try:
        v = float(x)
        return round(v, nd) if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def turnover_eok(df):
    return _f(float((df["Close"].iloc[-20:] * df["Volume"].iloc[-20:]).mean()) / 1e8, 1)


def last_chg(df):
    c = df["Close"].dropna()
    return _f((c.iloc[-1] / c.iloc[-2] - 1) * 100, 2) if len(c) >= 2 else None


def candidate(row, meta, df):
    c = df["Close"].dropna().tail(CHART_DAYS)
    return {
        "code": meta["코드"], "name": meta["종목명"], "market": meta["시장"], "sector": meta["업종"],
        "price": _f(row["종가"], 0), "chg": last_chg(df), "turnover_eok": turnover_eok(df),
        "score": _f(row["점수"], 3), "gates": int(row["게이트통과수"]),
        "drawdown": _f(row["최대낙폭"] * 100, 0), "high_age": None if not np.isfinite(row["신고가나이"]) else int(row["신고가나이"]),
        "vol_mult": _f(row["거래량배수"], 1), "rs": _f(row["RS백분위"] * 100, 0), "fresh": _f(row["돌파신선도"], 2),
        "missing": row["미달항목"], "missing_n": int(row["미달개수"]), "final": bool(row["최종후보"]),
        "chart": [[str(d.date()), int(round(float(v)))] for d, v in c.items()],
    }


def extremes(px_by_market, info):
    """52주 신고가·신저가 -- 유동성 필터를 통과한 종목만."""
    win, tol = ub.CFG["HIGH_WIN"], ub.CFG["HIGH_TOL"]
    highs, lows = [], []
    for t, df in px_by_market.items():
        c = df["Close"].dropna()
        if len(c) < win + 5:
            continue
        ok, _ = kr_is_tradable(df.loc[c.index])
        if not ok:
            continue
        last = float(c.iloc[-1])
        w = c.iloc[-win:]
        row = {"code": info[t]["코드"], "name": info[t]["종목명"], "market": info[t]["시장"],
               "sector": info[t]["업종"], "price": _f(last, 0), "chg": last_chg(df),
               "turnover_eok": turnover_eok(df)}
        if last >= float(w.max()) * (1 - tol):
            highs.append(row)
        elif last <= float(w.min()) * (1 + tol):
            lows.append(row)
    return highs, lows


def sector_counts(highs, lows):
    by = {}
    for kind, rows in (("high", highs), ("low", lows)):
        for r in rows:
            by.setdefault(r["sector"], {"sector": r["sector"], "high": 0, "low": 0})[kind] += 1
    return sorted(by.values(), key=lambda x: -(x["high"] + x["low"]))


def sector_breadth(px_all, info, highs, lows):
    """KIND 업종별 섹터 온도 -- 종목별 수익률의 중앙값, 상승 종목 비율, 신고가·신저가 수, 상승/하락 대표 종목.
    유동성 필터를 통과한 종목만. 업종 이름은 KIND 의 것 그대로(세분화돼 있다)."""
    rows = []
    for t, df in px_all.items():
        c = df["Close"].dropna()
        if len(c) < 64:
            continue
        ok, _ = kr_is_tradable(df.loc[c.index])
        if not ok:
            continue
        last = float(c.iloc[-1])

        def r(k):
            return (last / float(c.iloc[-1 - k]) - 1) * 100
        rows.append({"name": info[t]["종목명"], "code": info[t]["코드"], "market": info[t]["시장"],
                     "sector": info[t]["업종"], "d1": r(1), "d5": r(5), "m1": r(21), "m3": r(63),
                     "turnover_eok": turnover_eok(df)})
    if not rows:
        return None

    def med(v):
        return _f(np.median(v), 2)

    n_hi, n_lo = {}, {}
    for x in highs:
        n_hi[x["sector"]] = n_hi.get(x["sector"], 0) + 1
    for x in lows:
        n_lo[x["sector"]] = n_lo.get(x["sector"], 0) + 1

    markets = {}
    for m in ("코스피", "코스닥"):
        sub = [x for x in rows if x["market"] == m]
        markets[m] = {"n": len(sub), "up": sum(1 for x in sub if x["d1"] > 0),
                      "down": sum(1 for x in sub if x["d1"] < 0),
                      "flat": sum(1 for x in sub if x["d1"] == 0),
                      "n_high": sum(1 for x in highs if x["market"] == m),
                      "n_low": sum(1 for x in lows if x["market"] == m)}

    by = {}
    for x in rows:
        by.setdefault(x["sector"], []).append(x)
    sectors = []
    for name, v in by.items():
        if len(v) < 3:
            continue
        up, down = sum(1 for x in v if x["d1"] > 0), sum(1 for x in v if x["d1"] < 0)
        srt = sorted(v, key=lambda x: x["d1"], reverse=True)
        pick = lambda x: {"name": x["name"], "code": x["code"], "chg": _f(x["d1"], 2)}
        sectors.append({
            "name": name, "n": len(v), "up": up, "down": down,
            "adv_pct": _f(up / (up + down) * 100, 0) if up + down else None,
            "d1": med([x["d1"] for x in v]), "d5": med([x["d5"] for x in v]),
            "m1": med([x["m1"] for x in v]), "m3": med([x["m3"] for x in v]),
            "n_high": n_hi.get(name, 0), "n_low": n_lo.get(name, 0),
            "top": [pick(x) for x in srt[:3]], "bottom": [pick(x) for x in srt[::-1][:3] if x["d1"] < 0],
        })
    sectors.sort(key=lambda s: -(s["d1"] or 0))
    return {"markets": markets, "sectors": sectors}


# ---------------------------------------------------------------- 실행

def main():
    ap = argparse.ArgumentParser(description="한국 52주 신고가 x 물극필반 스크리너")
    ap.add_argument("-w", "--workdir", default="screener")
    ap.add_argument("--limit", type=int, default=0, help="시험용: 종목을 앞에서 N개만")
    ap.add_argument("--full-refresh", action="store_true")
    args = ap.parse_args()

    kst = now_kst()
    if not env("KR_FORCE") and kst.weekday() < 5 and kst.hour < 16:
        print(f"장 마감 전({kst:%H:%M} KST)이라 오늘 봉이 미완성입니다 -- 건너뜁니다. (시험: KR_FORCE=1)")
        return 0

    ub._setup_paths(args.workdir)
    ub.PATHS["cache"] = os.path.join(ub.PATHS["work"], CACHE_NAME)
    ub.is_tradable = kr_is_tradable              # scan_prices 가 이 이름을 다시 찾아 쓴다
    ub.market_last_session = kr_last_session     # fetch_prices 도 마찬가지

    t0 = time.time()
    print("[종목 목록]")
    univ = get_universe(ub.PATHS["work"])
    if args.limit:
        # KIND 목록은 상장일 최신순이라 앞에서 자르면 신규 상장만 남는다 -- 무작위로 뽑는다
        univ = pd.concat([univ[univ["시장"] == m].sample(min(args.limit // 2, int((univ["시장"] == m).sum())), random_state=1)
                          for m in ("코스피", "코스닥")])
    info = univ.set_index("티커")[["코드", "종목명", "시장", "업종"]].to_dict("index")
    print(f"  코스피 {int((univ['시장'] == '코스피').sum())} · 코스닥 {int((univ['시장'] == '코스닥').sum())}")

    print("\n[가격 수집]")
    tickers = sorted(set(univ["티커"]) | set(BENCHES))
    px, mkt_last = ub.fetch_prices(tickers, full_refresh=args.full_refresh)

    # 장 마감 전(강제 실행)이면 오늘 봉은 장중 값이다 -- 빼서 어제 종가까지로 계산하고 캐시에도 어제까지만 남긴다.
    # (캐시 마지막 날짜가 '시장 최종 거래일'이면 다시 안 받으므로, 장중 값이 남으면 종가로 안 고쳐진다)
    if kst.weekday() < 5 and kst.hour < 16:
        today = kst.date()
        px = {t: d[[pd.Timestamp(i).date() != today for i in d.index]] for t, d in px.items()}
        px = {t: d for t, d in px.items() if len(d)}
        ub._save_cache(px)
        if "^KS11" in px:
            mkt_last = pd.Timestamp(px["^KS11"].index[-1]).date()
        print(f"  장중 실행 -- 오늘({today}) 봉을 빼고 {mkt_last} 종가 기준으로 계산합니다.")

    # 진단: 종목별 봉 수 분포 (히스토리가 900봉 미만이면 스크리너가 판정하지 않는다)
    lens = pd.Series({t: len(px[t]) for t in tickers if t in px and t not in BENCHES})
    if len(lens):
        print(f"  봉 수: 최소 {int(lens.min())} · 중앙 {int(lens.median())} · 최대 {int(lens.max())} · "
              f"{ub.CFG['MIN_BARS']}봉 이상 {int((lens >= ub.CFG['MIN_BARS']).sum())}/{len(lens)}")
        for t, n in lens[lens < ub.CFG["MIN_BARS"]].head(10).items():
            print(f"    짧음: {t} {info.get(t, {}).get('종목명', '')} {n}봉 · 첫날 {px[t].index[0].date()} · 끝 {px[t].index[-1].date()}")

    frames, funnel, dropped_all = [], [], 0
    by_market = {}
    for m, (label, _, bench) in {v[0]: v for v in MARKETS.values()}.items():
        sub = univ[univ["시장"] == label]
        px_m = {t: px[t] for t in sub["티커"] if t in px}
        long_px = {t: d for t, d in px_m.items() if len(d) >= ub.CFG["MIN_BARS"]}
        by_market[label] = long_px
        print(f"\n{label}: 가격 {len(px_m)}/{len(sub)} · 히스토리 충분 {len(long_px)}")
        bench_close = px[bench]["Close"] if bench in px else None
        scan = ub.scan_prices(long_px, bench_close, verbose=True)
        dropped_all += len(scan.attrs.get("dropped", []))
        funnel.append([f"{label} 전체", len(sub)])
        funnel.append([f"  └ 가격·히스토리 확보", len(long_px)])
        funnel.append([f"  └ 오늘 52주 신고가(유동성 통과)", len(scan)])
        if not scan.empty:
            frames.append(scan)

    breakout = []
    if frames:
        res = ub.apply_gates(pd.concat(frames, ignore_index=True))
        res = res.sort_values(["미달개수", "점수"], ascending=[True, False])
        funnel.append(["  └ 게이트 3개 이상", int((res["게이트통과수"] >= ub.CFG["GATE_MIN"]).sum())])
        funnel.append(["  └ 트리거 전부 통과", int(res["트리거통과"].sum())])
        funnel.append(["최종후보", int(res["최종후보"].sum())])
        near = res[res["미달개수"] <= NEAR_MISS].head(MAX_CANDIDATES)
        for _, row in near.iterrows():
            t = row["티커"]
            breakout.append(candidate(row, info[t], px[t]))
        print(f"\n근접후보 {len(breakout)}개 / 최종후보 {int(res['최종후보'].sum())}개")
    else:
        funnel.append(["오늘 52주 신고가", 0])

    all_px = {t: d for m in by_market.values() for t, d in m.items()}
    highs, lows = extremes(all_px, info)
    print(f"52주 신고가 {len(highs)} · 신저가 {len(lows)}")

    try:
        sb = sector_breadth(all_px, info, highs, lows)
        if sb:
            write_json(DATA_DIR / "kr_sectors.json", {"updated_at": now_kst().isoformat(),
                                                      "asof": str(mkt_last), **sb})
            print(f"섹터 온도: 업종 {len(sb['sectors'])}개 (종목 3개 이상)")
    except Exception as e:      # 섹터 온도가 잘못돼도 스크리너 결과는 그대로 저장한다
        print(f"  섹터 온도 계산 실패: {type(e).__name__}: {e}", file=sys.stderr)

    def trim(rows):
        return sorted(rows, key=lambda r: -(r["turnover_eok"] or 0))[:MAX_EXTREME_NAMES]

    write_json(DATA_DIR / "kr_breakout.json", {
        "updated_at": now_kst().isoformat(),
        "asof": str(mkt_last),
        "funnel": funnel,
        "criteria": {"min_price": MIN_PRICE_KRW, "min_turnover_eok": MIN_TURNOVER_KRW / 1e8,
                     "gate_min": ub.CFG["GATE_MIN"], "near_miss": NEAR_MISS},
        "breakout": breakout,
        "extremes": {"n_high": len(highs), "n_low": len(lows),
                     "high": trim(highs), "low": trim(lows), "sectors": sector_counts(highs, lows)[:25]},
    })
    print(f"\n저장 완료 (기준일 {mkt_last}, {time.time()-t0:.0f}초)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
