"""머리말 배너 + '금리·환율' 화면용 시세 -- 미국채 금리, 유가, 환율.

야후(yfinance)에서 5년치 일봉을 한 번에 받고, 못 받은 항목은 FRED 공개 CSV(키 불필요)로 채운다.
실시간은 아니고 실행 시각 기준 값이다(아침 7시 반·저녁 6시).
결과
- docs/data/banner.json   : 머리말 배너 (최신 값과 전일 대비)
- docs/data/rates_fx.json : 금리·환율 화면 (최근 1년은 일별, 그 이전은 주별)
받지 못한 항목은 직전 값을 유지한다.
"""

import csv
import datetime
import io
import json
import sys

import requests

from common import DATA_DIR, now_kst

OUT_BANNER = DATA_DIR / "banner.json"
OUT_RATES = DATA_DIR / "rates_fx.json"

# key, 이름, 야후 티커, FRED 대체, 종류(yield=금리 / px=가격), 배율, 배너에 넣나, 화면 묶음
ITEMS = [
    ("us3m",  "미국채 3개월", "^IRX",      "DGS3MO",      "yield", 1,   False, "금리"),
    ("us2y",  "미국채 2년",   "2YY=F",     "DGS2",        "yield", 1,   True,  "금리"),
    ("us5y",  "미국채 5년",   "^FVX",      "DGS5",        "yield", 1,   False, "금리"),
    ("us10y", "미국채 10년",  "^TNX",      "DGS10",       "yield", 1,   True,  "금리"),
    ("us30y", "미국채 30년",  "^TYX",      "DGS30",       "yield", 1,   False, "금리"),
    ("wti",   "WTI",         "CL=F",      "DCOILWTICO",  "px",    1,   True,  None),
    ("brent", "브렌트",       "BZ=F",      "DCOILBRENTEU","px",    1,   True,  None),
    ("usdkrw","원/달러",      "KRW=X",     "DEXKOUS",     "px",    1,   True,  "환율"),
    ("usdjpy","엔/달러",      "JPY=X",     "DEXJPUS",     "px",    1,   True,  "환율"),
    ("jpykrw","원/100엔",     "JPYKRW=X",  None,          "px",    100, True,  "환율"),
    ("dxy",   "달러인덱스",    "DX-Y.NYB",  None,          "px",    1,   False, "환율"),
]
BANNER_ORDER = ["us10y", "us2y", "spread", "wti", "brent", "usdkrw", "usdjpy", "jpykrw"]


def from_yahoo(tickers):
    """{티커: [(날짜, 종가), ...]} 5년치."""
    out = {}
    try:
        import yfinance as yf
        df = yf.download(tickers, period="5y", interval="1d", group_by="ticker",
                         progress=False, threads=False, auto_adjust=False)
    except Exception as ex:
        print(f"야후 실패: {ex}", file=sys.stderr)
        return out
    for t in tickers:
        try:
            s = df[t]["Close"].dropna()
            out[t] = [(str(i.date()), float(v)) for i, v in s.items()]
        except Exception:
            pass
    return out


def from_fred(series):
    try:
        r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                         params={"id": series}, timeout=30)
        r.raise_for_status()
        rows = [x for x in csv.reader(io.StringIO(r.text))][1:]
        cut = str(datetime.date.today() - datetime.timedelta(days=5 * 366))
        return [(d, float(v)) for d, v in rows if v not in ("", ".") and d >= cut]
    except Exception as ex:
        print(f"FRED {series} 실패: {ex}", file=sys.stderr)
        return []


def thin(ser):
    """최근 1년은 매일, 그 이전은 주마다 한 점 -- 파일 크기를 줄인다."""
    if not ser:
        return []
    cut = str(datetime.date.fromisoformat(ser[-1][0]) - datetime.timedelta(days=366))
    out, last_week = [], None
    for d, v in ser:
        if d >= cut:
            out.append((d, v))
            continue
        wk = datetime.date.fromisoformat(d).isocalendar()[:2]
        if wk != last_week:
            out.append((d, v))
            last_week = wk
    return out


def main():
    prev_b, prev_r = {}, {}
    if OUT_BANNER.exists():
        try:
            prev_b = {i["key"]: i for i in json.loads(OUT_BANNER.read_text(encoding="utf-8")).get("items", [])}
        except Exception:
            pass
    if OUT_RATES.exists():
        try:
            prev_r = {i["key"]: i for g in json.loads(OUT_RATES.read_text(encoding="utf-8")).get("groups", [])
                      for i in g["items"]}
        except Exception:
            pass

    yahoo = from_yahoo([i[2] for i in ITEMS])
    series, got = {}, 0
    for key, label, yt, fred, kind, mult, _, _ in ITEMS:
        ser = yahoo.get(yt) or []
        if len(ser) < 2 and fred:
            ser = from_fred(fred)
        if len(ser) >= 2:
            series[key] = [(d, v * mult) for d, v in ser]
            got += 1

    def summary(key, label, kind, ser):
        (d0, v0), (d1, v1) = ser[-2], ser[-1]
        chg = round((v1 - v0) * 100, 1) if kind in ("yield",) else \
            round(v1 - v0, 1) if kind == "spread" else round((v1 / v0 - 1) * 100, 2)
        nd = 3 if kind == "yield" else 0 if kind == "spread" else 2
        return {"key": key, "label": label, "kind": kind, "value": round(v1, nd), "chg": chg, "asof": d1}

    # 장단기 금리차(10년-2년, bp) -- 같은 날짜끼리
    if "us10y" in series and "us2y" in series:
        two = dict(series["us2y"])
        series["spread"] = [(d, (v - two[d]) * 100) for d, v in series["us10y"] if d in two]

    labels = {i[0]: (i[1], i[4]) for i in ITEMS}
    labels["spread"] = ("10년−2년", "spread")

    # 배너
    banner = []
    for key in BANNER_ORDER:
        label, kind = labels[key]
        if len(series.get(key, [])) >= 2:
            banner.append(summary(key, label, kind, series[key]))
        elif key in prev_b:
            banner.append(prev_b[key] | {"stale": True})

    # 금리·환율 화면
    groups = []
    for gname, keys in [("금리", ["us3m", "us2y", "us5y", "us10y", "us30y", "spread"]),
                        ("환율", ["usdkrw", "usdjpy", "jpykrw", "dxy"])]:
        items = []
        for key in keys:
            label, kind = labels[key]
            ser = series.get(key, [])
            if len(ser) >= 2:
                it = summary(key, label, kind, ser)
                nd = 3 if kind == "yield" else 1 if kind == "spread" else 2
                it["history"] = [{"date": d, "value": round(v, nd)} for d, v in thin(ser)]
                items.append(it)
            elif key in prev_r:
                items.append(prev_r[key] | {"stale": True})
        groups.append({"name": gname, "items": items})

    if not got and not prev_b:
        print("받은 값이 없습니다 -- 파일을 만들지 않습니다.", file=sys.stderr)
        sys.exit(1)
    stamp = now_kst().strftime("%Y-%m-%d %H:%M")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_BANNER.write_text(json.dumps({"updated": stamp, "items": banner}, ensure_ascii=False), encoding="utf-8")
    OUT_RATES.write_text(json.dumps({"updated": stamp, "groups": groups}, ensure_ascii=False,
                                    separators=(",", ":")), encoding="utf-8")
    print(f"시세 {got}/{len(ITEMS)}개 새로 받음 · " + " · ".join(f"{i['label']} {i['value']}" for i in banner))


if __name__ == "__main__":
    main()
