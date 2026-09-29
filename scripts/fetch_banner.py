"""머리말 배너용 시세 -- 미국채 금리, 유가, 환율.

야후(yfinance)에서 한 번에 받고, 못 받은 항목은 FRED 공개 CSV(키 불필요)로 채운다.
실시간은 아니고 실행 시각 기준 값이다(아침 7시 반·저녁 6시).
결과: docs/data/banner.json -- 받지 못한 항목은 직전 값을 유지하고 '기준일'로 구분한다.
"""

import csv
import io
import json
import sys

import requests

from common import DATA_DIR, now_kst

OUT = DATA_DIR / "banner.json"

# key, 화면 이름, 야후 티커, FRED 대체 시리즈, 종류(yield=금리 bp / px=가격 %), 배율
ITEMS = [
    ("us10y", "미국채 10년", "^TNX",     "DGS10",       "yield", 1),
    ("us2y",  "미국채 2년",  "2YY=F",    "DGS2",        "yield", 1),
    ("wti",   "WTI",        "CL=F",     "DCOILWTICO",  "px",    1),
    ("brent", "브렌트",      "BZ=F",     "DCOILBRENTEU","px",    1),
    ("usdkrw","원/달러",     "KRW=X",    "DEXKOUS",     "px",    1),
    ("usdjpy","엔/달러",     "JPY=X",    "DEXJPUS",     "px",    1),
    ("jpykrw","원/100엔",    "JPYKRW=X", None,          "px",    100),
]


def from_yahoo(tickers):
    """{티커: [(날짜, 종가), ...]} 최근 며칠."""
    out = {}
    try:
        import yfinance as yf
        df = yf.download(tickers, period="10d", interval="1d", group_by="ticker",
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
        vals = [(d, float(v)) for d, v in rows if v not in ("", ".")]
        return vals[-10:]
    except Exception as ex:
        print(f"FRED {series} 실패: {ex}", file=sys.stderr)
        return []


def main():
    prev = {}
    if OUT.exists():
        try:
            prev = {i["key"]: i for i in json.loads(OUT.read_text(encoding="utf-8")).get("items", [])}
        except Exception:
            prev = {}

    yahoo = from_yahoo([i[2] for i in ITEMS])
    items, got = [], 0
    for key, label, yt, fred, kind, mult in ITEMS:
        ser = yahoo.get(yt) or []
        src = "yahoo"
        if len(ser) < 2 and fred:
            ser, src = from_fred(fred), "fred"
        if len(ser) >= 2:
            (d0, v0), (d1, v1) = ser[-2], ser[-1]
            v0, v1 = v0 * mult, v1 * mult
            chg = round((v1 - v0) * 100, 1) if kind == "yield" else round((v1 / v0 - 1) * 100, 2)
            items.append({"key": key, "label": label, "value": round(v1, 3 if kind == "yield" else 2),
                          "chg": chg, "kind": kind, "asof": d1, "src": src})
            got += 1
        elif key in prev:
            items.append(prev[key] | {"stale": True})

    # 장단기 금리차(10년-2년)
    m = {i["key"]: i for i in items}
    if "us10y" in m and "us2y" in m:
        spread = round((m["us10y"]["value"] - m["us2y"]["value"]) * 100)
        items.insert(2, {"key": "spread", "label": "10년−2년", "value": spread, "kind": "spread",
                         "chg": round(m["us10y"]["chg"] - m["us2y"]["chg"], 1), "asof": m["us10y"]["asof"]})

    if not got and not prev:
        print("받은 값이 없습니다 -- 파일을 만들지 않습니다.", file=sys.stderr)
        sys.exit(1)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"updated": now_kst().strftime("%Y-%m-%d %H:%M"), "items": items},
                              ensure_ascii=False), encoding="utf-8")
    print(f"배너 {got}/{len(ITEMS)}개 새로 받음 · " +
          " · ".join(f"{i['label']} {i['value']}" for i in items))


if __name__ == "__main__":
    main()
