"""내 종목 소식 -- 보유 종목(미국 11개)과 동국제약의 최근 뉴스·공시를 종목별로 모은다.

종목 목록
  미국: config/portfolio.yaml 의 holdings (보유 종목이 곧 관심종목)
  한국: config/watchlist.yaml 의 kr (동국제약)

수집
  뉴스  fetch_news.gather 재사용 (미국=구글 뉴스 영문, 한국=네이버 API HUB). 최근 7일, 종목당 최대 6건. Gemini 가 한 줄 요약.
  공시  미국=SEC 제출 목록(8-K 항목 번호를 한국어로 풀이, 10-Q/10-K, 대량보유, 증자 등. Form 4 내부자 거래는
        '포트폴리오 종목 수급' 화면에 이미 있어 뺀다), 한국=DART 공시검색(동국제약 고유번호로 최근 45일).
  중요 표시: 제목·공시 항목에 실적·가이던스·인수·소송·임원 변경 같은 낱말이 있으면 hot=true (화면에서 🔥와 함께 위로 올림).

결과: docs/data/my_stocks.json
"""

import sys
from datetime import timedelta

import fetch_news
from common import DATA_DIR, ROOT, env, get, load_config, now_kst, read_json, write_json
from fetch_edgar import load_cik_map, sec_get

NEWS_DAYS = 7
NEWS_MAX = 6
FILING_DAYS = 45
FILING_MAX = 6

# 회사명이 티커와 달라 검색이 빗나가는 종목의 검색어를 직접 지정한다
QUERY_OVERRIDE = {
    "SPY": ["SPY ETF S&P 500"],
    "DEA": ["Easterly Government Properties"],
    "CRM": ["Salesforce stock"],
    "CPNG": ["Coupang stock"],
    "META": ["Meta Platforms stock"],
    "NKE": ["Nike stock"],
    "PFE": ["Pfizer stock"],
    "ENPH": ["Enphase Energy stock"],
    "GOOGL": ["Alphabet Google stock"],
    "AMZN": ["Amazon stock"],
    "MSFT": ["Microsoft stock"],
}

HOT_EN = ("earnings", "guidance", "forecast", "outlook", "acquire", "acquisition", "merger", "buyout",
          "lawsuit", "sues", "settlement", "downgrade", "upgrade", "price target", "ceo", "cfo", "resign",
          "layoff", "job cuts", "recall", "fda", "approval", "bankruptcy", "investigation", "probe",
          "buyback", "repurchase", "dividend", "offering", "plunge", "surge", "soar", "tumble", "beats", "misses")
HOT_KO = ("실적", "가이던스", "인수", "합병", "소송", "하향", "상향", "목표가", "대표", "사임", "구조조정",
          "리콜", "승인", "허가", "상장폐지", "횡령", "배임", "조사", "자사주", "배당", "유상증자", "급락", "급등")

# SEC 8-K 항목 번호 -> (한국어 설명, 중요 여부)
ITEMS = {
    "1.01": ("중요 계약 체결", True), "1.02": ("중요 계약 해지", True), "1.03": ("파산·회생", True),
    "1.05": ("사이버 보안 사고", True),
    "2.01": ("자산 인수·처분 완료", True), "2.02": ("실적 발표", True), "2.03": ("채무 발생", False),
    "2.04": ("채무 조기상환 사유", True), "2.05": ("구조조정 비용", True), "2.06": ("자산 손상", True),
    "3.01": ("상장 유지 요건 통보", True), "3.02": ("비등록 주식 발행", False),
    "4.01": ("감사인 변경", True), "4.02": ("재무제표 재작성", True),
    "5.01": ("지배권 변동", True), "5.02": ("임원·이사 변경", True), "5.03": ("정관 변경", False),
    "5.07": ("주주총회 결과", False), "7.01": ("공정공시(Reg FD)", False), "8.01": ("기타 주요 사건", False),
    "9.01": ("첨부 자료", False),
}
FORM_LABEL = {
    "10-Q": ("분기보고서(10-Q)", False), "10-K": ("연간보고서(10-K)", False),
    "S-3": ("증권 발행 신고(S-3)", True), "S-3ASR": ("증권 발행 신고(S-3)", True),
    "DEF 14A": ("주주총회 소집(위임장)", False),
    "SC 13D": ("5% 이상 지분 보유 신고(13D)", True), "SC 13D/A": ("5% 이상 지분 정정(13D)", True),
    "SC 13G": ("5% 이상 지분 보유 신고(13G)", False), "SC 13G/A": ("5% 이상 지분 정정(13G)", False),
    "SCHEDULE 13D": ("5% 이상 지분 보유 신고(13D)", True), "SCHEDULE 13D/A": ("5% 이상 지분 정정(13D)", True),
    "SCHEDULE 13G": ("5% 이상 지분 보유 신고(13G)", False), "SCHEDULE 13G/A": ("5% 이상 지분 정정(13G)", False),
    "144": ("내부자 주식 매도 예정(144)", False), "8-K12B": ("승계 8-K", False),
}


def is_hot(title, ko=False):
    t = title if ko else title.lower()
    return any(w in t for w in (HOT_KO if ko else HOT_EN))


def make_creds():
    return {"naver_id": env("NAVER_CLIENT_ID"), "naver_secret": env("NAVER_CLIENT_SECRET"),
            "gemini": env("GEMINI_API_KEY")}


def us_filings(ticker, cik_map):
    cik = cik_map.get(ticker)
    if not cik:
        return None, []          # ETF 등 CIK 가 없으면 공시는 건너뛴다
    data = sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json", as_json=True)
    if not data:
        return None, []
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    n = len(forms)

    def col(k):
        v = recent.get(k) or []
        return v + [""] * (n - len(v))
    cutoff = (now_kst() - timedelta(days=FILING_DAYS)).strftime("%Y-%m-%d")
    out = []
    for i, form in enumerate(forms):
        filed = col("filingDate")[i]
        if filed and filed < cutoff:
            break                          # 제출 목록은 최신순이라 여기서부터는 모두 오래된 것
        label, title, hot = None, None, False
        if form == "8-K":
            codes = [c.strip() for c in (col("items")[i] or "").split(",") if c.strip()]
            real = [c for c in codes if c not in ("9.01",)]
            parts = [ITEMS[c][0] for c in real if c in ITEMS] or ["8-K 공시"]
            hot = any(ITEMS.get(c, ("", False))[1] for c in real)
            label, title = "8-K", ", ".join(parts[:3])
        elif form in FORM_LABEL:
            title, hot = FORM_LABEL[form]
            label = form
        else:
            continue                      # Form 4(내부자 거래) 등은 뺀다
        acc, doc = col("accessionNumber")[i], col("primaryDocument")[i]
        url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
               if acc and doc else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}")
        out.append({"date": filed, "form": form, "label": label, "title": title, "hot": hot, "url": url})
    out.sort(key=lambda x: x["date"], reverse=True)
    return data.get("name"), out[:FILING_MAX]


def kr_filings(code):
    key = env("DART_API_KEY")
    if not key:
        return []
    cm = read_json(ROOT / "screener" / "dart_corp_codes.json") or {}
    corp = (cm.get(code) or [None])[0]
    if not corp:
        print(f"  DART 고유번호 없음: {code}", file=sys.stderr)
        return []
    from fetch_dart import category_of
    bgn = (now_kst() - timedelta(days=FILING_DAYS)).strftime("%Y%m%d")
    try:
        r = get("https://opendart.fss.or.kr/api/list.json", timeout=40,
                params={"crtfc_key": key, "corp_code": corp, "bgn_de": bgn,
                        "end_de": now_kst().strftime("%Y%m%d"), "page_count": 40})
        r.raise_for_status()
        d = r.json()
    except Exception as e:      # noqa: BLE001
        print(f"  DART 요청 실패: {str(e)[:100]}", file=sys.stderr)
        return []
    if d.get("status") not in ("000", "013"):
        print(f"  DART 응답 이상: {d.get('status')} {d.get('message')}", file=sys.stderr)
        return []
    out = []
    for x in d.get("list") or []:
        title = (x.get("report_nm") or "").strip()
        if title.startswith(("[첨부정정]", "[첨부추가]")):
            continue
        cat = category_of(title)
        compact = title.replace(" ", "")
        if "소유상황보고서" in compact:
            label, hot = "임원·주요주주 지분 변동", False
        elif any(w in compact for w in ("사업보고서", "분기보고서", "반기보고서")):
            label, hot = "정기보고서", False
        elif cat:
            label, hot = cat, True
        else:
            label, hot = "기타 공시", False
        dt = x.get("rcept_dt") or ""
        out.append({"date": f"{dt[:4]}-{dt[4:6]}-{dt[6:]}", "form": title, "label": label, "hot": hot,
                    "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={x.get('rcept_no')}",
                    "title": title})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:FILING_MAX]


def news_for(queries, creds, market):
    items = fetch_news.gather(queries, creds, limit=NEWS_MAX, market=market)
    for it in items:
        it["hot"] = is_hot(it["title"], ko=(market == "kr"))
    return items


def main():
    creds = make_creds()
    fetch_news.LOOKBACK_HOURS = NEWS_DAYS * 24
    cik_map = load_cik_map()
    stocks = []

    holdings = [h["ticker"] for h in (load_config("portfolio.yaml") or {}).get("holdings", []) if h.get("ticker")]
    for tk in holdings:
        print(f"내 종목: {tk}")
        name, filings = us_filings(tk, cik_map)
        queries = QUERY_OVERRIDE.get(tk) or [f"{tk} stock"]
        news = news_for(queries, creds, "us")
        stocks.append({"market": "us", "ticker": tk, "name": name or tk, "news": news, "filings": filings})

    for k in (load_config("watchlist.yaml") or {}).get("kr", []):
        print(f"내 종목: {k['name']}")
        news = news_for([k["name"], f"{k['name']} 주가"], creds, "kr")
        stocks.append({"market": "kr", "ticker": k["code"], "name": k["name"], "news": news,
                       "filings": kr_filings(k["code"])})

    for s in stocks:
        s["hot"] = sum(1 for x in s["news"] if x.get("hot")) + sum(1 for x in s["filings"] if x.get("hot"))
    write_json(DATA_DIR / "my_stocks.json", {
        "updated_at": now_kst().isoformat(),
        "news_days": NEWS_DAYS, "filing_days": FILING_DAYS,
        "stocks": stocks,
    })
    print(f"저장: {len(stocks)}종목 · 뉴스 {sum(len(s['news']) for s in stocks)}건 · "
          f"공시 {sum(len(s['filings']) for s in stocks)}건 · 중요 {sum(s['hot'] for s in stocks)}건")


if __name__ == "__main__":
    main()
