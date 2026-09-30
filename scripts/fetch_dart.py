"""한국 탭 3단계 -- DART 공시(코스피·코스닥 상장사).

전자공시(OpenDART) 공시검색 API 로 최근 며칠치를 받아, 투자 판단에 쓸 만한 종류만
제목의 낱말로 분류해 남긴다(잠정실적·공급계약·자사주·증자/감자·합병/분할·CB/BW·배당·리스크).
정기보고서(사업/분기)는 양이 많고 잠정실적이 먼저 나오므로 뺀다.

키: DART_API_KEY (opendart.fss.or.kr, 무료).  없으면 조용히 끝낸다.
쌓는 방식: docs/data/kr_disclosures.json 에 접수번호로 중복 없이 누적, KEEP_DAYS 지난 건 버린다.
공시 원문 링크: https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호
"""

import io
import json
import re
import sys
import time
import zipfile
from datetime import date, timedelta

from common import DATA_DIR, ask_gemini, env, get, now_kst, read_json, write_json

OUT = DATA_DIR / "kr_disclosures.json"
API = "https://opendart.fss.or.kr/api/list.json"
DOC_API = "https://opendart.fss.or.kr/api/document.xml"
MAX_SUMMARIES = 30       # 실행당 Gemini 호출 상한 (코스피 먼저)
MAX_TRIES = 2            # 이만큼 실패하면 포기
NUMBERS_DAYS = 10        # 이 기간 안의 잠정실적만 숫자를 뽑는다
DOC_CHARS = 20_000       # 원문 앞부분만 넘긴다(표가 앞쪽에 있다)
GEMINI_PAUSE = 5         # 무료 등급 분당 한도를 넉넉히 지킨다
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


# ---------------------------------------------------------------- 잠정실적 숫자

PROMPT = """아래는 한국 상장사의 '영업(잠정)실적' 공시 원문이다.
공시에 적힌 숫자만 옮겨라. 계산하거나 추측하지 마라. 없으면 null.
표에 연결과 별도가 다 있으면 연결을 쓴다.

JSON 하나만 출력한다. 설명, 코드블록 표시(```) 없이.
{{
 "period": "보고 기간 (예: 2026년 3분기, 2026년 연간)",
 "basis": "연결" | "별도",
 "unit": "공시에 적힌 금액 단위 그대로: 원 | 천원 | 백만원 | 억원 | 조원",
 "revenue": 이번 실적의 매출액(숫자, 위 단위 기준),
 "revenue_prior": 전년 동기 매출액,
 "op": 이번 영업이익(적자면 음수),
 "op_prior": 전년 동기 영업이익(적자면 음수),
 "net": 이번 당기순이익(적자면 음수),
 "net_prior": 전년 동기 당기순이익(적자면 음수),
 "summary": "한국어 한 문장, 60자 이내. 무엇이 좋았고 무엇이 나빴는지. 공시에 적힌 사유가 있으면 그것."
}}
'전년동기'는 표의 '전년동기' 열 값이다. 직전 분기(전분기) 값과 헷갈리지 마라.

--- 공시 원문 ---
{text}
"""

UNIT_TO_EOK = {"원": 1e-8, "천원": 1e-5, "백만원": 1e-2, "억원": 1.0, "십억원": 10.0, "조원": 1e4}
NUM_KEYS = ("revenue", "revenue_prior", "op", "op_prior", "net", "net_prior")


def fetch_doc_text(key, rcept_no):
    """공시 원문(zip 안의 xml)을 받아 태그를 걷어낸 글로. 실패하면 None."""
    try:
        r = get(DOC_API, params={"crtfc_key": key, "rcept_no": rcept_no}, timeout=40)
        r.raise_for_status()
        if not r.content.startswith(b"PK"):
            print(f"    원문 응답이 zip 이 아님: {r.text[:100]}", file=sys.stderr)
            return None
        z = zipfile.ZipFile(io.BytesIO(r.content))
        raw = b"".join(z.read(n) for n in z.namelist() if n.lower().endswith((".xml", ".html", ".htm")))
    except Exception as e:
        print(f"    원문 받기 실패({rcept_no}): {str(e)[:100]}", file=sys.stderr)
        return None
    for enc in ("utf-8", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            text = None
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(str(v).replace(",", "").replace("△", "-").replace("▲", "-").strip())
    except ValueError:
        return None


def yoy(cur, prior):
    """(증감률 %, 라벨). 기준이 흑자면 %, 부호가 바뀌면 흑자전환/적자전환, 둘 다 적자면 적자지속."""
    if cur is None or prior is None:
        return None, ""
    if prior > 0:
        return (round((cur / prior - 1) * 100, 1), "적자전환" if cur <= 0 else "")
    if prior <= 0 and cur > 0:
        return None, "흑자전환"
    if prior < 0 and cur <= 0:
        return None, "적자지속"
    return None, ""


def parse_answer(raw):
    if not raw:
        return None
    raw = raw.replace("```json", "").replace("```", "")
    a, b = raw.find("{"), raw.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        d = json.loads(raw[a:b + 1])
    except json.JSONDecodeError:
        return None
    factor = UNIT_TO_EOK.get(str(d.get("unit") or "").strip())
    if factor is None:
        return None                      # 단위를 모르면 숫자를 믿을 수 없다
    out = {}
    for k in NUM_KEYS:
        v = _num(d.get(k))
        out[k] = round(v * factor, 2) if v is not None else None   # 억원으로 통일
    if all(out[k] is None for k in NUM_KEYS):
        return None
    out["period"] = str(d.get("period") or "")[:30]
    out["basis"] = "별도" if str(d.get("basis")) == "별도" else "연결"
    out["summary"] = str(d.get("summary") or "")[:120]
    for name, cur, pri in (("revenue", "revenue", "revenue_prior"),
                           ("op", "op", "op_prior"), ("net", "net", "net_prior")):
        out[name + "_yoy"], out[name + "_tag"] = yoy(out[cur], out[pri])
    return out


def fill_numbers(key, gkey, items):
    if not gkey:
        print("  GEMINI_API_KEY 없음 -- 잠정실적 숫자 추출을 건너뜁니다.")
        return
    cut = (now_kst().date() - timedelta(days=NUMBERS_DAYS)).isoformat()
    todo = [x for x in items if x["category"] == "잠정실적" and x["date"] >= cut
            and not x.get("numbers") and x.get("num_tries", 0) < MAX_TRIES]
    # 코스피(큰 회사가 많다) 먼저, 같으면 최근 것 먼저
    todo.sort(key=lambda x: (x["market"] != "코스피", -int(x["rcept_no"])))
    if not todo:
        print("  숫자 추출할 잠정실적 없음")
        return
    print(f"  숫자 추출 대기 {len(todo)}건 중 이번에 {min(len(todo), MAX_SUMMARIES)}건")
    ok = fail = 0
    for x in todo[:MAX_SUMMARIES]:
        x["num_tries"] = x.get("num_tries", 0) + 1
        text = fetch_doc_text(key, x["rcept_no"])
        if not text:
            fail += 1
            continue
        raw = ask_gemini(PROMPT.format(text=text[:DOC_CHARS]), gkey, timeout=90)
        time.sleep(GEMINI_PAUSE)
        nums = parse_answer(raw)
        if not nums:
            print(f"    {x['corp']}: 답을 해석하지 못함", file=sys.stderr)
            fail += 1
            continue
        x["numbers"] = nums
        ok += 1
        op = nums["op_yoy"]
        print(f"    {x['corp'][:10]:10} {nums['period']:14} 영업이익 YoY "
              f"{nums['op_tag'] or ('—' if op is None else f'{op:+.1f}%')}")
    print(f"  숫자 추출 성공 {ok}건 · 실패 {fail}건")


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

    try:
        fill_numbers(key, env("GEMINI_API_KEY"), store["items"])
    except Exception as e:      # 숫자 추출이 잘못돼도 수집분은 그대로 저장한다
        print(f"  숫자 추출 중 오류(수집분은 그대로 저장): {e}", file=sys.stderr)

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
