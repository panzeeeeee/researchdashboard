"""백테스팅용 일별 데이터 적재 -- append-only.

이미 만들어진 JSON들(market/liquidity/credit/money_market/tables 등)을 읽어
날짜별 CSV로 쌓기만 한다. 기존 파이프라인은 건드리지 않는다. 이 단계는
워크플로우 맨 끝(모든 수집이 끝난 뒤)에 돌아야 한다.

두 파일:
  history/market_daily.csv   -- 하루 한 줄. 시장 국면 지표(넓은 형태).
  history/screener_daily.csv -- 하루 여러 줄. 종목마다 한 줄(긴 형태).

원칙:
  1. append-only. 같은 날짜가 이미 있으면 그 날짜 줄만 갈아끼운다(재실행 대비).
     그 외 과거 줄은 절대 건드리지 않는다.
  2. tidy. 분석 도구가 바로 읽게 CSV. 날짜는 KST 기준 YYYY-MM-DD.
  3. look-ahead 방지. 그날 수집된 값 그대로 박제한다(나중에 수정 안 함).
"""

import csv
import sys

from common import DATA_DIR, now_kst, read_json

HIST_DIR = DATA_DIR.parent.parent / "history"   # 리포지토리 루트/history


def _num(x):
    return x if isinstance(x, (int, float)) else None


def upsert_csv(path, fieldnames, new_rows, key_fields):
    """CSV를 append-only로 갱신. key_fields가 같은 기존 줄은 교체, 나머지는 유지.

    파일이 없으면 헤더와 함께 새로 만든다. 헤더에 없던 컬럼이 생기면
    합쳐서 다시 쓴다(과거 줄은 빈 값으로 둔다) -- 컬럼이 늘어도 안전하게.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    old_fields = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            old_fields = reader.fieldnames or []
            existing = list(reader)

    all_fields = list(dict.fromkeys(list(old_fields) + list(fieldnames)))

    new_keys = {tuple(str(r.get(k, "")) for k in key_fields) for r in new_rows}
    kept = [r for r in existing
            if tuple(str(r.get(k, "")) for k in key_fields) not in new_keys]

    merged = kept + [{k: r.get(k, "") for k in all_fields} for r in new_rows]
    # 날짜 우선 정렬(첫 key_field가 날짜라는 전제)
    merged.sort(key=lambda r: tuple(str(r.get(k, "")) for k in key_fields))

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_fields)
        w.writeheader()
        for r in merged:
            w.writerow({k: r.get(k, "") for k in all_fields})


def collect_market_row(date):
    row = {"date": date}
    m = read_json(DATA_DIR / "market.json") or {}
    for r in (m.get("indexes") or []):
        row[f"idx_{r.get('ticker','')}"] = _num(r.get("chg"))
    row["breadth_spread"] = _num(m.get("breadth_spread"))
    for r in (m.get("sectors") or []):
        row[f"sec_{r.get('name','')}"] = _num(r.get("chg"))
    for r in (m.get("risk") or []):
        row[f"risk_{r.get('label','')}"] = _num(r.get("value"))

    liq = read_json(DATA_DIR / "liquidity.json") or {}
    nl = liq.get("net_liquidity") or {}
    row["net_liquidity"] = _num(nl.get("value"))

    cr = read_json(DATA_DIR / "credit.json") or {}
    row["hy_spread"] = _num((cr.get("high_yield") or {}).get("value"))
    row["ig_spread"] = _num((cr.get("investment_grade") or {}).get("value"))

    mm = read_json(DATA_DIR / "money_market.json") or {}
    for r in (mm.get("rates") or []):
        row[f"rate_{r.get('label','')}"] = _num(r.get("value"))

    tb = read_json(DATA_DIR / "tables.json") or {}
    hist = tb.get("history") or []
    if hist:
        last = hist[-1]
        row["breakout_highs"] = last.get("highs")
        row["breakout_gate3"] = last.get("gate3")
        row["dv_candidates"] = last.get("dv_candidates")
        row["dv_triggered"] = last.get("dv_triggered")

    return row


def collect_screener_rows(date):
    rows = []
    tb = read_json(DATA_DIR / "tables.json") or {}
    for r in (tb.get("breakout") or []):
        rows.append({"date": date, "kind": "breakout", "ticker": r.get("code"),
                     "name": r.get("name"), "universe": r.get("universe"),
                     "score": r.get("score"), "gates": r.get("gates"),
                     "drawdown": r.get("drawdown"), "fscore": "", "triggered": ""})
    for r in (tb.get("deepvalue") or []):
        rows.append({"date": date, "kind": "deepvalue", "ticker": r.get("code"),
                     "name": r.get("name"), "universe": r.get("universe"),
                     "score": r.get("score"), "gates": "",
                     "drawdown": r.get("drawdown"), "fscore": r.get("fscore"),
                     "triggered": r.get("triggered")})

    ex = read_json(DATA_DIR / "extremes.json") or {}
    for kind in ("high", "low"):
        for c in (ex.get(kind) or []):
            rows.append({"date": date, "kind": f"52w_{kind}", "ticker": c.get("ticker"),
                         "name": c.get("name"), "universe": "",
                         "score": "", "gates": "", "drawdown": "",
                         "fscore": "", "triggered": ""})
    return rows


def main():
    date = now_kst().date().isoformat()

    market_row = collect_market_row(date)
    upsert_csv(HIST_DIR / "market_daily.csv",
               list(market_row.keys()), [market_row], key_fields=["date"])
    print(f"market_daily.csv 적재 ({len(market_row)-1}개 컬럼)")

    screener_rows = collect_screener_rows(date)
    if screener_rows:
        fields = ["date", "kind", "ticker", "name", "universe",
                  "score", "gates", "drawdown", "fscore", "triggered"]
        upsert_csv(HIST_DIR / "screener_daily.csv",
                   fields, screener_rows, key_fields=["date", "kind", "ticker"])
        print(f"screener_daily.csv 적재 ({len(screener_rows)}줄)")
    else:
        print("스크리너 결과가 없어 종목 적재는 건너뜁니다.", file=sys.stderr)


if __name__ == "__main__":
    main()
