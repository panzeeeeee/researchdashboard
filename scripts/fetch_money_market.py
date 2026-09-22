"""자금시장 상세 — 단기금리 5개, SOFR 거래금리 분포(25/75/99백분위+거래량), 중앙은행 달러 스와프 잔액.

글로벌 유동성 스크립트와 같은 FRED 공개 CSV 방식. API 키 필요 없음.

BGCR은 뺐다. TGCR과 같은 시기에 FRED에 추가된 계열인데 정확한 시리즈 ID를
확인하지 못했다 (TGCR은 TGCRRATE로 확인됨). 나중에 확인되면 추가한다.
"""

import csv
import io
import sys
import urllib.request

from common import DATA_DIR, now_kst, write_json

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"


def fred_rows(series_id, n=10):
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


def latest(series_id, label, unit="%", decimals=2, scale=1):
    rows = fred_rows(series_id)
    if not rows:
        return {"label": label, "value": None, "unit": unit, "date": None}
    d0, v0 = rows[0]
    return {"label": label, "value": round(v0 * scale, decimals), "unit": unit, "date": d0}


def main():
    rates = [
        latest("SOFR", "SOFR"),
        latest("EFFR", "EFFR"),
        latest("IORB", "IORB"),
        latest("OBFR", "OBFR"),
        latest("TGCRRATE", "TGCR"),
    ]

    # 1년/10년 토글용 SOFR 추이 — 일간이라 10년이면 3,700개면 넉넉하다
    sofr_history = [{"date": d, "value": v} for d, v in reversed(fred_rows("SOFR", n=3700))]

    distribution = [
        latest("SOFR25", "25백분위"),
        latest("SOFR75", "75백분위"),
        latest("SOFR99", "99백분위"),
        latest("SOFRVOL", "거래량", unit="십억$", decimals=0),
    ]

    swap = latest("SWPT", "중앙은행 달러 스와프 잔액", unit="십억$", scale=1 / 1000)

    write_json(DATA_DIR / "money_market.json", {
        "updated_at": now_kst().isoformat(),
        "rates": rates,
        "sofr_history": sofr_history,
        "sofr_distribution": distribution,
        "swap": swap,
    })
    print("저장 완료")


if __name__ == "__main__":
    main()
