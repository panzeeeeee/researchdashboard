"""한국은행 ECOS 에서 경제 지표용 통계표·항목 코드를 조회하는 시험 (파일을 만들지 않는다).

1. 100대 통계지표(KeyStatisticList) -- 기준금리·물가·실업률·경상수지·GDP 등 대표 지표의 이름·최신값·주기
2. 통계표 목록에서 관심 낱말이 들어간 표의 코드 (StatisticTableList)
3. 몇몇 통계표의 항목 코드 (StatisticItemList)
"""

import sys
import time

import requests

from common import env

KEY = env("ECOS_API_KEY")
BASE = "https://ecos.bok.or.kr/api"
UA = {"User-Agent": "Mozilla/5.0 (compatible; research-dashboard/1.0)"}


def call(path):
    t0 = time.time()
    try:
        r = requests.get(f"{BASE}/{path}", headers=UA, timeout=60)
        r.raise_for_status()
        d = r.json()
        print(f"  · {path.split('/')[0]} {time.time() - t0:.1f}초 · {len(r.content):,}바이트", flush=True)
        return d
    except Exception as e:
        print(f"  · {path.split('/')[0]} 실패: {str(e)[:100]}", flush=True)
        return {}


def main():
    if not KEY:
        print("ECOS_API_KEY 없음")
        return

    print("=== 1. 100대 통계지표 (KeyStatisticList) ===", flush=True)
    d = call(f"KeyStatisticList/{KEY}/json/kr/1/200")
    rows = (d.get("KeyStatisticList") or {}).get("row") or []
    print(f"  {len(rows)}개")
    for r in rows:
        print(f"  [{r.get('CLASS_NAME')}] {r.get('KEYSTAT_NAME')} | {r.get('DATA_VALUE')} {r.get('UNIT_NAME')} | {r.get('CYCLE')}")

    print("\n=== 2. 통계표 목록에서 관심 낱말 ===", flush=True)
    d = call(f"StatisticTableList/{KEY}/json/kr/1/2000/")
    rows = (d.get("StatisticTableList") or {}).get("row") or []
    print(f"  전체 {len(rows)}개 (주기 있는 통계표 위주)")
    words = ("기준금리", "소비자물가", "생산자물가", "실업", "경상수지", "국내총생산", "소비자심리", "기업경기", "M2", "광의통화",
             "산업생산", "수출입", "가계대출", "주택매매", "설비투자", "무역", "고용", "취업자", "경제성장", "경기종합", "선행지수")
    seen = set()
    for r in rows:
        nm = str(r.get("STAT_NAME") or "")
        if any(w in nm for w in words) and r.get("STAT_CODE") not in seen and r.get("SRCH_YN") == "Y":
            seen.add(r.get("STAT_CODE"))
            print(f"  {r.get('STAT_CODE')} | {nm} | {r.get('CYCLE')} | 상위 {r.get('P_STAT_CODE')}")

    print("\n=== 3. 통계표 항목 코드 ===", flush=True)
    for code in ("722Y001", "901Y009", "901Y027", "901Y033", "301Y013", "200Y102", "511Y002", "512Y013", "101Y004", "104Y013"):
        d = call(f"StatisticItemList/{KEY}/json/kr/1/60/{code}")
        rows = (d.get("StatisticItemList") or {}).get("row") or []
        print(f"\n  --- {code} ({len(rows)}개 항목)")
        for r in rows[:40]:
            print(f"    {r.get('ITEM_CODE')} | {r.get('ITEM_NAME')} | {r.get('CYCLE')} | {r.get('START_TIME')}~{r.get('END_TIME')} | {r.get('UNIT_NAME')}")
    print("\n시험 끝", flush=True)


if __name__ == "__main__":
    main()
