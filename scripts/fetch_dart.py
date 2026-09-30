"""한국 탭 3단계 -- DART 공시(코스피·코스닥 상장사).

전자공시(OpenDART) 공시검색 API 로 최근 며칠치를 받아, 투자 판단에 쓸 만한 종류만
제목의 낱말로 분류해 남긴다(잠정실적·공급계약·자사주·증자/감자·합병/분할·CB/BW·배당·리스크).
정기보고서(사업/분기)는 양이 많고 잠정실적이 먼저 나오므로 뺀다.

키: DART_API_KEY (opendart.fss.or.kr, 무료).  없으면 조용히 끝낸다.
쌓는 방식: docs/data/kr_disclosures.json 에 접수번호로 중복 없이 누적, KEEP_DAYS 지난 건 버린다.
공시 원문 링크: https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호
"""

import sys
import time
from datetime import timedelta

from common import DATA_DIR, env, get, now_kst, read_json, write_json

OUT = DATA_DIR / "kr_disclosures.json"
API = "https://opendart.fss.or.kr/api/list.json"
LOOKBACK_DAYS = 3        # 주말·휴일을 건너뛰어도 빠지지 않게 넉넉히
KEEP_DAYS = 30
PAGE = 100               # API 최대
MAX_PAGES = 15           # 유형별로 이만큼까지만 (1,500건)
TYPES = ("I", "B")       # I 거래소공시(잠정실적·공급계약 등), B 주요사항보고(증자·합병 등)

# 분류: 위에서부터 먼저 맞는 것을 쓴다. (이름, 제목에 들어 있는 낱말들)
CATEGORIES = [
    ("리스크", ("상장폐지", "횡령", "배임", "부도", "회생절차", "감사의견", "거래정지",
              "불성실공시", "관리종목", "파산")),
    ("잠정실적", ("영업(잠정)실적", "잠정실적")),
    ("실적변동", ("매출액또는손익구조", "손익구조")),
    ("공급계약", ("단일판매", "공급계약")),
    ("자사주", ("자기주식",)),
    ("증자·감자", ("유상증자", "무상증자", "감자")),
    ("합병·분할", ("합병", "분할", "영업양수", "영업양도", "주식교환", "주식이전")),
    ("CB·BW", ("전환사채", "신주인수권부사채", "교환사채")),
    ("배당", ("배당결정", "현금ㆍ현물배당", "현금·현물배당")),
]
SKIP_PREFIX = ("[기재정정]", "[첨부정정]", "[첨부추가]")   # 정정은 원공시와 겹치니 뺀다


def category_of(title):
    t = title.replace(" ", "")
    for name, words in CATEGORIES:
        if any(w.replace(" ", "") in t for w in words):
            return name
    return None


def fetch_type(key, ptype, bgn, end):
    rows, page = [], 1
    while page <= MAX_PAGES:
        try:
            r = get(API, timeout=30, params={
                "crtfc_key": key, "bgn_de": bgn, "end_de": end, "pblntf_ty": ptype,
                "page_no": page, "page_count": PAGE})
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            print(f"  DART 요청 실패(유형 {ptype}, {page}쪽): {str(e)[:100]}", file=sys.stderr)
            break
        status = d.get("status")
        if status == "013":            # 조회된 데이터 없음
            break
        if status != "000":
            print(f"  DART 응답 이상(유형 {ptype}): {status} {d.get('message')}", file=sys.stderr)
            break
        rows += d.get("list") or []
        if page >= int(d.get("total_page") or 1):
            break
        page += 1
        time.sleep(0.3)
    return rows


def main():
    key = env("DART_API_KEY")
    if not key:
        print("DART_API_KEY 가 없어 건너뜁니다.")
        return

    today = now_kst().date()
    bgn = (today - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    store = read_json(OUT) or {"items": []}
    have = {x["rcept_no"] for x in store["items"]}

    got = added = 0
    for ptype in TYPES:
        rows = fetch_type(key, ptype, bgn, end)
        got += len(rows)
        for r in rows:
            title = (r.get("report_nm") or "").strip()
            no = r.get("rcept_no")
            if not no or no in have or title.startswith(SKIP_PREFIX):
                continue
            if r.get("corp_cls") not in ("Y", "K") or not r.get("stock_code"):
                continue                # 코스피(Y)·코스닥(K) 상장사만
            cat = category_of(title)
            if not cat:
                continue
            d = str(r.get("rcept_dt") or "")
            store["items"].append({
                "rcept_no": no,
                "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
                "corp": (r.get("corp_name") or "").strip(),
                "code": r["stock_code"],
                "market": "코스피" if r["corp_cls"] == "Y" else "코스닥",
                "title": title,
                "category": cat,
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={no}",
            })
            have.add(no)
            added += 1
    print(f"  공시 {got}건 훑음 → 새로 남긴 {added}건")

    if not got and not store["items"]:
        print("받은 공시가 없습니다 -- 파일을 만들지 않습니다.", file=sys.stderr)
        return

    cut = (today - timedelta(days=KEEP_DAYS)).isoformat()
    store["items"] = [x for x in store["items"] if x["date"] >= cut]
    store["items"].sort(key=lambda x: (x["date"], x["rcept_no"]), reverse=True)
    store["updated_at"] = now_kst().isoformat()
    write_json(OUT, store)

    by = {}
    for x in store["items"]:
        by[x["category"]] = by.get(x["category"], 0) + 1
    print(f"  보관 {len(store['items'])}건 · " + " · ".join(f"{k} {v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
