"""글로벌 유동성 — Fed 자산·TGA·RRP·준비금, ECB·BOJ 자산, 단기 자금시장 금리.

전부 FRED 공개 CSV에서 받는다. API 키가 없어도 된다 (미국 시장 현황 스크립트와 같은 방식).
"""

import csv
import io
import sys
import urllib.request
from datetime import date, timedelta

from common import DATA_DIR, now_kst, write_json

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"


def fred_rows(series_id, n=30):
    """최근 n개 관측치를 (날짜, 값) 리스트로, 최신순으로 돌려준다.

    첫 줄(헤더)은 위치로 건너뛴다 — FRED가 파일 앞에 보이지 않는 BOM을
    붙여 보낼 때가 있어서, "DATE" 문자열과 직접 비교하면 못 걸러진다.
    """
    url = FRED_URL.format(series_id)
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            text = r.read().decode("utf-8-sig")
        reader = list(csv.reader(io.StringIO(text)))
        rows = []
        for d, v in reader[1:]:
            if v in ("", "."):
                continue
            rows.append((d, float(v)))
        rows.sort(key=lambda x: x[0])
        return rows[-n:][::-1]  # 최신이 맨 앞
    except Exception as e:
        print(f"  FRED {series_id} 실패: {e}", file=sys.stderr)
        return []


def week_ago_value(rows, ref_date_str):
    """일별 계열에서, 기준일로부터 6일 이상 전인 값 중 가장 가까운 걸 찾는다.

    주간(수요일) 계열은 바로 앞 관측치가 곧 1주 전이라 이 함수가 필요 없다.
    """
    if not ref_date_str:
        return None
    ref = date.fromisoformat(ref_date_str)
    target = ref - timedelta(days=6)
    for d, v in rows:
        if date.fromisoformat(d) <= target:
            return v
    return None


def component(series_id, label, scale=1 / 1000, weekly=True):
    """잔액 + 1주 변화가 필요한 항목 (Fed 자산, TGA, RRP, 준비금, 국채·MBS 보유)."""
    rows = fred_rows(series_id, n=30)
    if not rows:
        return {"label": label, "value": None, "wow": None, "date": None}
    d0, v0 = rows[0]
    v_prev = rows[1][1] if weekly and len(rows) > 1 else week_ago_value(rows, d0)
    value = round(v0 * scale, 1)
    wow = round((v0 - v_prev) * scale, 1) if v_prev is not None else None
    return {"label": label, "value": value, "wow": wow, "date": d0}


def level(series_id, label, unit, scale=1, decimals=2):
    """최근값만 필요한 항목 (SOFR, IORB, ECB·BOJ 자산)."""
    rows = fred_rows(series_id, n=10)
    if not rows:
        return {"label": label, "value": None, "unit": unit, "date": None}
    d0, v0 = rows[0]
    return {"label": label, "value": round(v0 * scale, decimals), "unit": unit, "date": d0}


def main():
    fed = component("WALCL", "Fed 총자산")
    tga = component("WTREGEN", "TGA · 재무부 일반계정")
    rrp = component("RRPONTSYD", "ON RRP", scale=1, weekly=False)
    resv = component("WRESBAL", "은행 준비금 · 주간 평균")
    ust = component("TREAST", "Fed 국채 보유")
    mbs = component("WSHOMCB", "Fed MBS 보유")
    components = [fed, tga, rrp, resv, ust, mbs]

    net_now, net_wow = None, None
    if all(c["value"] is not None for c in (fed, tga, rrp)):
        net_now = round(fed["value"] - tga["value"] - rrp["value"], 1)
    if all(c["wow"] is not None for c in (fed, tga, rrp)):
        net_wow = round(fed["wow"] - tga["wow"] - rrp["wow"], 1)

    money_market = [
        level("SOFR", "SOFR", "%"),
        level("IORB", "IORB", "%"),
    ]

    glob = [
        level("ECBASSETSW", "ECB 자산", "조 유로", scale=1 / 1_000_000),
        level("JPNASSETS", "BOJ 자산", "조 엔", scale=1 / 10_000),
    ]

    write_json(DATA_DIR / "liquidity.json", {
        "updated_at": now_kst().isoformat(),
        "net_liquidity": {"value": net_now, "wow": net_wow, "date": fed["date"]},
        "components": components,
        "money_market": money_market,
        "global": glob,
    })
    print("저장 완료")


if __name__ == "__main__":
    main()
