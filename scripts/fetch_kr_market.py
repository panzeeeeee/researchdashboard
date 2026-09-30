"""한국 탭 1단계 -- 코스피·코스닥 지수, 업종 온도, 국고채 금리.

- 지수: 야후(yfinance) ^KS11 코스피, ^KQ11 코스닥.
- 업종: 야후에는 KRX 업종지수가 없어서 KODEX 업종 ETF 로 대신한다(업종 흐름의 대용).
  못 받는 티커는 건너뛴다.
- 국고채: 한국은행 ECOS(키: ECOS_API_KEY). 통계표 817Y002(시장금리, 일별).
  항목 코드를 박아두지 않고 항목 목록을 물어서 '국고채(3년)' 같은 이름으로 찾는다.
  키가 없거나 실패하면 금리 칸만 비우고 나머지는 그대로 만든다.
  KRX 는 해외 서버(Actions)에서 막힐 수 있어 쓰지 않는다.

결과: docs/data/kr_market.json
받지 못한 항목은 직전 파일 값을 유지한다.
"""

import re
import sys
from datetime import timedelta

from common import DATA_DIR, env, get, now_kst, read_json, write_json

OUT = DATA_DIR / "kr_market.json"

INDEXES = [("kospi", "코스피", "^KS11"), ("kosdaq", "코스닥", "^KQ11")]

# 업종 대용 ETF (KODEX)
SECTOR_ETFS = [
    ("반도체", "091160.KS"), ("자동차", "091180.KS"), ("은행", "091170.KS"),
    ("증권", "102970.KS"), ("건설", "117700.KS"), ("철강", "117680.KS"),
    ("에너지화학", "117460.KS"), ("2차전지", "305720.KS"), ("바이오", "244580.KS"),
]

ECOS = "https://ecos.bok.or.kr/api"
ECOS_TABLE = "817Y002"                      # 시장금리(일별)
ECOS_YEARS = (3, 5, 10, 30)                 # 화면에 보일 국고채 만기


# ---------------------------------------------------------------- 야후

def from_yahoo(tickers, period="1y"):
    """{티커: [(날짜, 종가), ...]}"""
    out = {}
    try:
        import yfinance as yf
        df = yf.download(tickers, period=period, interval="1d", group_by="ticker",
                         progress=False, threads=False, auto_adjust=False)
    except Exception as e:
        print(f"야후 실패: {e}", file=sys.stderr)
        return out
    # 한국 장(15:30 마감)이 끝나기 전이면 오늘 봉은 장중 값이라 뺀다 -- 항상 종가만 쓴다
    now = now_kst()
    today = str(now.date())
    closed = now.hour * 60 + now.minute >= 15 * 60 + 40
    for t in tickers:
        try:
            s = df[t]["Close"].dropna()
            rows = [(str(i.date()), float(v)) for i, v in s.items()]
            if not closed and rows and rows[-1][0] == today:
                rows = rows[:-1]
            if len(rows) >= 2:
                out[t] = rows
        except Exception:
            pass
    return out


def pct_back(ser, n):
    """n 거래일 전 대비 %. 자료가 모자라면 None."""
    if len(ser) <= n:
        return None
    return round((ser[-1][1] / ser[-1 - n][1] - 1) * 100, 2)


# ---------------------------------------------------------------- ECOS

def ecos_json(url):
    try:
        r = get(url, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  ECOS 요청 실패: {str(e)[:120]}", file=sys.stderr)
        return None


def ecos_rates(key):
    """{연수: [(날짜, 금리), ...]} -- 최근 400일."""
    d = ecos_json(f"{ECOS}/StatisticItemList/{key}/json/kr/1/200/{ECOS_TABLE}")
    rows = ((d or {}).get("StatisticItemList") or {}).get("row") or []
    if not rows:
        print(f"  ECOS 항목 목록을 못 받음: {str(d)[:150]}", file=sys.stderr)
        return {}
    codes = {}
    for r in rows:
        m = re.fullmatch(r"국고채\((\d+)년\)", str(r.get("ITEM_NAME", "")).strip())
        if m and int(m.group(1)) in ECOS_YEARS:
            codes[int(m.group(1))] = r["ITEM_CODE"]

    end = now_kst().date()
    start = end - timedelta(days=400)
    out = {}
    for yrs, code in sorted(codes.items()):
        d = ecos_json(f"{ECOS}/StatisticSearch/{key}/json/kr/1/1000/{ECOS_TABLE}/D/"
                      f"{start:%Y%m%d}/{end:%Y%m%d}/{code}")
        rows = ((d or {}).get("StatisticSearch") or {}).get("row") or []
        ser = []
        for r in rows:
            try:
                t = str(r["TIME"])
                ser.append((f"{t[:4]}-{t[4:6]}-{t[6:8]}", float(r["DATA_VALUE"])))
            except (KeyError, ValueError):
                continue
        ser.sort()
        if len(ser) >= 2:
            out[yrs] = ser
    return out


# ---------------------------------------------------------------- 조립

def summary(key, label, ser, kind):
    (d0, v0), (d1, v1) = ser[-2], ser[-1]
    if kind == "rate":
        chg, nd = round((v1 - v0) * 100, 1), 3          # bp
    else:
        chg, nd = round((v1 / v0 - 1) * 100, 2), 2      # %
    return {"key": key, "label": label, "kind": kind, "value": round(v1, nd),
            "chg": chg, "asof": d1,
            "history": [{"date": d, "value": round(v, nd)} for d, v in ser]}


def main():
    prev = read_json(OUT) or {}
    prev_idx = {i["key"]: i for i in prev.get("indexes", [])}
    prev_rate = {i["key"]: i for i in prev.get("rates", [])}
    prev_sec = {i["label"]: i for i in prev.get("sectors", [])}

    tickers = [t for _, _, t in INDEXES] + [t for _, t in SECTOR_ETFS]
    yahoo = from_yahoo(tickers)

    indexes = []
    for key, label, t in INDEXES:
        if t in yahoo:
            indexes.append(summary(key, label, yahoo[t], "index"))
        elif key in prev_idx:
            indexes.append(prev_idx[key] | {"stale": True})

    sectors = []
    for label, t in SECTOR_ETFS:
        s = yahoo.get(t)
        if s:
            sectors.append({"label": label, "ticker": t, "price": round(s[-1][1]),
                            "asof": s[-1][0], "d1": pct_back(s, 1),
                            "d5": pct_back(s, 5), "m1": pct_back(s, 21)})
        else:
            print(f"  업종 ETF {label}({t}) 시세 없음", file=sys.stderr)
            if label in prev_sec:
                sectors.append(prev_sec[label] | {"stale": True})

    rates, note = [], ""
    key = env("ECOS_API_KEY")
    if not key:
        note = "ECOS_API_KEY 가 없어 국고채를 건너뜁니다."
        print(note)
    else:
        by_year = ecos_rates(key)
        for yrs in ECOS_YEARS:
            if yrs in by_year:
                rates.append(summary(f"kr{yrs}y", f"국고채 {yrs}년", by_year[yrs], "rate"))
        if 3 in by_year and 10 in by_year:              # 장단기 금리차 (10년-3년, bp)
            three = dict(by_year[3])
            spread = [(d, (v - three[d]) * 100) for d, v in by_year[10] if d in three]
            if len(spread) >= 2:
                s = summary("kr_spread", "10년−3년", spread, "spread")
                s["chg"] = round(spread[-1][1] - spread[-2][1], 1)
                s["value"] = round(spread[-1][1], 1)
                rates.append(s)
        if not rates:
            note = "ECOS에서 국고채를 받지 못했습니다."
    if not rates:                                       # 못 받았으면 직전 값 유지
        rates = [v | {"stale": True} for v in prev_rate.values()]

    if not indexes and not sectors and not rates:
        print("받은 값이 없습니다 -- 파일을 만들지 않습니다.", file=sys.stderr)
        sys.exit(1)

    write_json(OUT, {"updated": now_kst().strftime("%Y-%m-%d %H:%M"),
                     "indexes": indexes, "sectors": sectors, "rates": rates,
                     "rates_note": note})
    print(f"지수 {len(indexes)} · 업종 {len(sectors)}/{len(SECTOR_ETFS)} · 국고채 {len(rates)}"
          + (f" · {note}" if note else ""))
    for i in indexes + rates:
        print(f"  {i['label']}: {i['value']} ({i['chg']:+})")


if __name__ == "__main__":
    main()
