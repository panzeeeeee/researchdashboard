"""미국 실적 레이더 1단계 — 밤사이 들어온 실적 8-K(Item 2.02)를 모은다.

어디서 받나
  SEC '최신 공시' 피드(Atom). 방금 접수된 8-K가 최신순으로 나오고,
  각 건의 요약에 Item 번호가 붙어 있어서 실적(2.02)만 바로 골라낼 수 있다.
  회사마다 따로 물어보지 않으므로 요청이 수십 번이면 끝난다.
  (일일 목록 파일은 미국 밤 10시 이후에야 나와서 한국 아침 수집에 늦는다.)

무엇을 남기나
  종목, 회사명, 접수 시각(미국 동부), 장전/장중/장후 구분, 공시 원문 링크.
  발표 후 주가 반응(종목 %, 같은 구간 SPY %, 시장 대비 %p).
    장전·장중 발표: 전날 종가 -> 발표일 종가
    장후 발표:     발표일 종가 -> 다음 거래일 종가 (그 전까지는 '대기')
  반응은 야후에서 한 번에 받는다(종목마다 요청하면 429로 막힌다).
  보도자료 숫자(매출·EPS·전년 수치·가이던스·한 줄 요약) -- Gemini 추출.
    실행당 MAX_SUMMARIES 건까지, 큰 회사부터. 못 한 건 다음 실행으로 넘긴다.
    자동 추출이므로 원문 링크로 확인할 것.

쌓는 방식
  docs/data/earnings.json 에 누적. 이미 있는 건 건너뛰고, 90일 지난 건 버린다.

SEC 요청 예절
  fetch_edgar.py 와 같은 User-Agent(SEC_USER_AGENT)와 요청 간격을 쓴다.
"""

import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from common import DATA_DIR, ask_gemini, env, get, now_kst, read_json, write_json
from fetch_edgar import HEAD, PAUSE, load_cik_map, press_release, sec_get

OUT = DATA_DIR / "earnings.json"
FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
        "&type=8-K&company=&dateb=&owner=include&count=100&start={start}&output=atom")
MAX_PAGES = 30           # 100건 x 30쪽 = 3,000건까지 훑는다
LOOKBACK_HOURS = 40      # 이보다 오래된 접수가 나오면 멈춘다
KEEP_DAYS = 90
REACT_DAYS = 10          # 이 기간 안의 발표만 반응을 채우거나 다시 본다
NY = ZoneInfo("America/New_York")
MAX_SUMMARIES = 20       # 실행당 Gemini 호출 상한
MAX_TRIES = 2            # 이만큼 실패하면 포기
MAX_INDUSTRY = 80        # 실행당 업종 조회 상한 (회사마다 SEC 요청 1번)
PROMPT_CHARS = 30_000    # 보도자료 앞부분만 넘긴다(표가 대개 앞쪽에 있다)
GEMINI_PAUSE = 5         # 무료 등급 분당 한도를 넉넉히 지킨다

NS = {"a": "http://www.w3.org/2005/Atom"}
ITEM_RE = re.compile(r"Item\s+(\d+\.\d+)", re.I)
ACC_RE = re.compile(r"\d{10}-\d{2}-\d{6}")
CIK_RE = re.compile(r"\((\d{4,10})\)")
NAME_RE = re.compile(r"^\s*8-K\s*-\s*(.+?)\s*\(\d{4,10}\)")


def fetch_page(start):
    url = FEED.format(start=start)
    for attempt in range(3):
        try:
            r = get(url, headers=HEAD, timeout=30)
            if r.status_code == 429:
                time.sleep(5)
                continue
            r.raise_for_status()
            time.sleep(PAUSE)
            return r.text
        except Exception as e:
            print(f"  피드 요청 실패(start={start}): {e}", file=sys.stderr)
            time.sleep(2)
    return None


def parse_time(s):
    """'2026-09-28T16:05:12-04:00' -> (동부시각 datetime, 절대시각 datetime)."""
    try:
        dt = datetime.fromisoformat(s.strip())
        return dt.replace(tzinfo=None), dt
    except Exception:
        return None, None


def session_of(local_dt):
    """미국 동부 시각 기준 장전/장중/장후."""
    if local_dt is None:
        return "?"
    hm = local_dt.hour * 60 + local_dt.minute
    if hm < 9 * 60 + 30:
        return "장전"
    if hm < 16 * 60:
        return "장중"
    return "장후"


def parse_entries(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  피드 해석 실패: {e}", file=sys.stderr)
        return []
    rows = []
    for e in root.findall("a:entry", NS):
        title = (e.findtext("a:title", "", NS) or "").strip()
        summary = e.findtext("a:summary", "", NS) or ""
        updated = e.findtext("a:updated", "", NS) or ""
        link_el = e.find("a:link", NS)
        link = link_el.get("href") if link_el is not None else ""

        if not title.upper().startswith("8-K ") and not title.upper().startswith("8-K-"):
            continue        # 8-K/A(정정) 등은 뺀다
        acc = ACC_RE.search(link) or ACC_RE.search(summary) or ACC_RE.search(
            e.findtext("a:id", "", NS) or "")
        cik = CIK_RE.search(title)
        name = NAME_RE.search(title)
        local, absolute = parse_time(updated)
        rows.append({
            "accession": acc.group(0) if acc else "",
            "cik": f"{int(cik.group(1)):010d}" if cik else "",
            "company": name.group(1) if name else title,
            "items": sorted(set(ITEM_RE.findall(summary))),
            "local": local,
            "absolute": absolute,
            "url": link,
        })
    return rows


# ---------------------------------------------------------------- 주가 반응

def _yf():
    try:
        import yfinance as yf
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "yfinance"],
                       check=False)
        import yfinance as yf
    return yf


def download_closes(tickers):
    """{티커: {날짜: 종가}}. 한 번의 요청으로 받는다."""
    yf = _yf()
    try:
        df = yf.download(tickers, period="1mo", interval="1d", auto_adjust=True,
                         group_by="ticker", progress=False, threads=False)
    except Exception as e:
        print(f"  시세 요청 실패: {e}", file=sys.stderr)
        return {}

    # 미국 장이 아직 안 끝났으면 오늘 봉은 미완성이라 뺀다
    now_et = datetime.now(NY)
    today_et = now_et.date()
    closed = now_et.hour * 60 + now_et.minute >= 16 * 60 + 10

    out = {}
    for t in tickers:
        try:
            col = df[t]["Close"] if len(tickers) > 1 else df["Close"]
        except Exception:
            continue
        series = {}
        for idx, v in col.dropna().items():
            d = idx.date() if hasattr(idx, "date") else idx
            if d == today_et and not closed:
                continue
            series[d] = float(v)
        if series:
            out[t] = series
    return out


def window(series, day, after_close):
    """(기준일, 반응일). 반응일 종가가 아직 없으면 반응일은 None."""
    days = sorted(series)
    if after_close:
        base = [d for d in days if d <= day]
        after = [d for d in days if d > day]
    else:
        after = [d for d in days if d >= day]
        base = [d for d in days if after and d < after[0]] if after else \
               [d for d in days if d < day]
    return (base[-1] if base else None), (after[0] if after else None)


def fill_reactions(items):
    today = datetime.now(NY).date()
    todo = []
    for x in items:
        if x.get("react_status") == "확정":
            continue
        try:
            d = date.fromisoformat(x.get("date_et", ""))
        except ValueError:
            continue
        if (today - d).days > REACT_DAYS:
            if not x.get("react_status"):
                x["react_status"] = "데이터 없음"
            continue
        todo.append((x, d))
    if not todo:
        print("  반응 채울 건 없음")
        return

    tickers = sorted({x["ticker"] for x, _ in todo} | {"SPY"})
    print(f"  시세 요청 {len(tickers)}종목 (SPY 포함)")
    closes = download_closes(tickers)
    spy = closes.get("SPY")
    if not spy:
        print("  SPY 시세를 못 받아 반응 계산을 건너뜁니다.", file=sys.stderr)
        return

    done = wait = miss = 0
    for x, d in todo:
        s = closes.get(x["ticker"])
        if not s:
            x["react_status"] = "데이터 없음"
            miss += 1
            continue
        after_close = x.get("session") == "장후"
        b, a = window(s, d, after_close)
        if a is None or b is None:
            x["react_status"] = "대기"
            wait += 1
            continue
        if a not in spy or b not in spy:
            x["react_status"] = "대기"
            wait += 1
            continue
        r = (s[a] / s[b] - 1) * 100
        m = (spy[a] / spy[b] - 1) * 100
        x.update({
            "react_pct": round(r, 1),
            "spy_pct": round(m, 1),
            "rel_pct": round(r - m, 1),
            "react_from": b.isoformat(),
            "react_to": a.isoformat(),
            "react_status": "확정",
        })
        done += 1
    print(f"  반응 확정 {done}건 · 대기 {wait}건 · 시세 없음 {miss}건")


# ---------------------------------------------------------------- 업종

# SEC 산업코드(SIC) -> 대분류. 위에서부터 먼저 맞는 규칙을 쓰므로 세부 규칙이 앞에 온다.
SIC_RULES = [
    (100, 999, "필수소비재"), (1000, 1099, "소재"), (1200, 1399, "에너지"),
    (1400, 1499, "소재"), (1500, 1799, "산업재"),
    (2000, 2199, "필수소비재"), (2200, 2399, "경기소비재"), (2400, 2499, "소재"),
    (2500, 2599, "경기소비재"), (2600, 2699, "소재"), (2700, 2799, "커뮤니케이션"),
    (2830, 2839, "헬스케어"), (2840, 2849, "필수소비재"), (2800, 2899, "소재"),
    (2900, 2999, "에너지"), (3000, 3099, "소재"), (3100, 3199, "경기소비재"),
    (3200, 3399, "소재"), (3400, 3499, "산업재"),
    (3570, 3579, "IT"), (3500, 3569, "산업재"), (3580, 3599, "산업재"),
    (3651, 3651, "경기소비재"), (3660, 3679, "IT"), (3600, 3699, "산업재"),
    (3710, 3719, "경기소비재"), (3700, 3799, "산업재"),
    (3826, 3826, "헬스케어"), (3840, 3859, "헬스케어"), (3800, 3899, "산업재"),
    (3900, 3999, "경기소비재"), (4000, 4799, "산업재"), (4800, 4899, "커뮤니케이션"),
    (4900, 4999, "유틸리티"), (5122, 5122, "헬스케어"), (5000, 5199, "산업재"),
    (5400, 5499, "필수소비재"), (5912, 5912, "필수소비재"), (5200, 5999, "경기소비재"),
    (6770, 6770, "기타"), (6798, 6798, "부동산"), (6500, 6599, "부동산"),
    (6000, 6799, "금융"), (7000, 7299, "경기소비재"), (7370, 7379, "IT"),
    (7300, 7399, "산업재"), (7800, 7899, "커뮤니케이션"), (7900, 7999, "경기소비재"),
    (8000, 8099, "헬스케어"), (8731, 8731, "헬스케어"), (8100, 8999, "산업재"),
]


def sector_of(sic):
    try:
        s = int(sic)
    except (TypeError, ValueError):
        return "기타"
    for lo, hi, name in SIC_RULES:
        if lo <= s <= hi:
            return name
    return "기타"


def fill_industry(items):
    """종목마다 SEC 산업코드로 대분류(sector)와 세부 업종(industry)을 붙인다.
    같은 회사(CIK)는 한 번만 묻고, 실행당 MAX_INDUSTRY 건까지."""
    known = {x["cik"]: (x["sic"], x["industry"]) for x in items
             if x.get("cik") and x.get("sic")}
    asked = ok = 0
    for x in items:
        if x.get("sector") or not x.get("cik"):
            continue
        if x["cik"] not in known:
            if asked >= MAX_INDUSTRY:
                continue
            asked += 1
            d = sec_get(f"https://data.sec.gov/submissions/CIK{x['cik']}.json", as_json=True)
            if not d:
                continue
            known[x["cik"]] = (str(d.get("sic") or ""), str(d.get("sicDescription") or ""))
        sic, desc = known[x["cik"]]
        x.update({"sic": sic, "industry": desc, "sector": sector_of(sic)})
        ok += 1
    print(f"  업종 채움 {ok}건 (SEC 조회 {asked}회)")


# ---------------------------------------------------------------- 숫자 추출

PROMPT ="""아래는 미국 상장사의 분기 실적 보도자료(SEC 8-K 첨부)다.
보도자료에 적힌 숫자만 옮겨라. 계산하거나 추측하지 마라. 없으면 null.

JSON 하나만 출력한다. 설명, 코드블록 표시(```) 없이.
{{
 "period": "보고 분기 (예: Q3 FY2026)",
 "revenue": 이번 분기 매출(백만 달러, 숫자),
 "revenue_prior": 전년 같은 분기 매출(백만 달러, 숫자),
 "eps_gaap": 이번 분기 GAAP 희석 EPS(달러),
 "eps_gaap_prior": 전년 같은 분기 GAAP 희석 EPS,
 "eps_adj": 회사가 제시한 조정(Non-GAAP) EPS, 없으면 null,
 "eps_adj_prior": 전년 같은 분기 조정 EPS,
 "guidance": "상향" | "유지" | "하향" | "첫 제시" | "없음",
 "summary": "한국어 한 문장, 60자 이내. 무엇이 좋았고 무엇이 나빴는지."
}}
guidance 판정:
 - 이전 전망치보다 올렸다고 적혀 있으면 "상향", 내렸다고 적혀 있으면 "하향"
 - 이전 전망치를 그대로 유지한다고 적혀 있으면 "유지"
 - 이번에 처음 전망치를 내놓았거나, 전망치는 있는데 이전과 비교가 없으면 "첫 제시"
 - 전망 언급이 아예 없으면 "없음"
분기가 아니라 연간·반기 보고면 해당 기간 숫자를 쓰고 period 에 그렇게 적어라.

--- 보도자료 ---
{text}
"""

NUM_KEYS = ("revenue", "revenue_prior", "eps_gaap", "eps_gaap_prior",
            "eps_adj", "eps_adj_prior")
GUIDE = {"상향", "유지", "하향", "첫 제시", "없음"}


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


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
    out = {k: _num(d.get(k)) for k in NUM_KEYS}
    out["period"] = str(d.get("period") or "")[:30]
    g = str(d.get("guidance") or "없음").strip()
    out["guidance"] = g if g in GUIDE else "없음"
    out["summary"] = str(d.get("summary") or "")[:120]

    rv, rp = out["revenue"], out["revenue_prior"]
    out["revenue_yoy"] = round((rv / rp - 1) * 100, 1) if rv and rp and rp > 0 else None
    if not any(out[k] is not None for k in NUM_KEYS) and not out["summary"]:
        return None
    return out


def fill_summaries(items, size_rank):
    key = env("GEMINI_API_KEY")
    if not key:
        print("  GEMINI_API_KEY 없음 -- 숫자 추출을 건너뜁니다.")
        return

    today = datetime.now(NY).date()
    todo = []
    for x in items:
        if x.get("numbers") or x.get("num_tries", 0) >= MAX_TRIES:
            continue
        try:
            if (today - date.fromisoformat(x.get("date_et", ""))).days > REACT_DAYS:
                continue
        except ValueError:
            continue
        todo.append(x)
    # 큰 회사부터 (SEC 티커 목록 순서 = 대략 시가총액 순)
    todo.sort(key=lambda x: size_rank.get(x["ticker"], 10 ** 6))
    if not todo:
        print("  숫자 추출할 건 없음")
        return
    print(f"  숫자 추출 대기 {len(todo)}건 중 이번에 {min(len(todo), MAX_SUMMARIES)}건")

    ok = fail = 0
    for x in todo[:MAX_SUMMARIES]:
        x["num_tries"] = x.get("num_tries", 0) + 1
        folder = x["accession"].replace("-", "")
        text, pr_url = press_release(x["cik"], folder, x["ticker"])
        if not text:
            fail += 1
            continue
        raw = ask_gemini(PROMPT.format(text=text[:PROMPT_CHARS]), key, timeout=90)
        time.sleep(GEMINI_PAUSE)
        nums = parse_answer(raw)
        if not nums:
            print(f"    {x['ticker']}: 답을 해석하지 못함", file=sys.stderr)
            fail += 1
            continue
        x["numbers"] = nums
        x["pr_url"] = pr_url
        ok += 1
        yoy = nums["revenue_yoy"]
        print(f"    {x['ticker']:6} {nums['period']:12} 매출 YoY "
              f"{'—' if yoy is None else f'{yoy:+.1f}%':>7}  가이던스 {nums['guidance']}")
    print(f"  숫자 추출 성공 {ok}건 · 실패 {fail}건")


def main():
    store = read_json(OUT) or {"items": []}
    have = {x["accession"] for x in store["items"]}

    cik_map = load_cik_map()            # 티커 -> CIK
    cik_to_ticker = {}
    size_rank = {t: i for i, t in enumerate(cik_map)}   # SEC 목록 순서
    for t, c in sorted(cik_map.items(), key=lambda kv: len(kv[0])):
        cik_to_ticker.setdefault(c, t)  # 여러 종류 주식이면 짧은 티커 하나만

    now_abs = datetime.now().astimezone()
    cutoff = now_abs - timedelta(hours=LOOKBACK_HOURS)

    seen, found, pages, stop = set(), [], 0, False
    for page in range(MAX_PAGES):
        text = fetch_page(page * 100)
        if not text:
            break
        rows = parse_entries(text)
        pages += 1
        if not rows:
            break
        for r in rows:
            if r["absolute"] and r["absolute"] < cutoff:
                stop = True
                continue
            if not r["accession"] or r["accession"] in seen:
                continue            # 같은 공시가 제출인/대상 두 줄로 나오기도 한다
            seen.add(r["accession"])
            if "2.02" in r["items"]:
                found.append(r)
        if stop:
            break

    print(f"  피드 {pages}쪽, 8-K {len(seen)}건 중 실적(2.02) {len(found)}건")

    added, no_ticker = 0, 0
    for r in found:
        if r["accession"] in have:
            continue
        ticker = cik_to_ticker.get(r["cik"], "")
        if not ticker:
            no_ticker += 1          # 비상장·채권 발행사 등
            continue
        store["items"].append({
            "accession": r["accession"],
            "ticker": ticker,
            "company": r["company"],
            "cik": r["cik"],
            "accepted_et": r["local"].strftime("%Y-%m-%d %H:%M") if r["local"] else "",
            "date_et": r["local"].strftime("%Y-%m-%d") if r["local"] else "",
            "session": session_of(r["local"]),
            "url": r["url"],
        })
        have.add(r["accession"])
        added += 1

    try:
        fill_reactions(store["items"])
    except Exception as e:
        print(f"  반응 계산 중 오류(수집분은 그대로 저장): {e}", file=sys.stderr)

    try:
        fill_industry(store["items"])
    except Exception as e:
        print(f"  업종 채우기 중 오류(나머지는 그대로 저장): {e}", file=sys.stderr)

    try:
        fill_summaries(store["items"], size_rank)
    except Exception as e:
        print(f"  숫자 추출 중 오류(나머지는 그대로 저장): {e}", file=sys.stderr)

    keep_from = (now_kst() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    store["items"] = [x for x in store["items"] if x.get("date_et", "") >= keep_from]
    store["items"].sort(key=lambda x: x.get("accepted_et", ""), reverse=True)
    store["updated_at"] = now_kst().isoformat()
    write_json(OUT, store)

    print(f"  새로 {added}건 (티커 없어 제외 {no_ticker}건), 보관 {len(store['items'])}건")
    for x in store["items"][:10]:
        if x.get("react_status") == "확정":
            r = f"{x['react_pct']:+.1f}% (시장 대비 {x['rel_pct']:+.1f}%p)"
        else:
            r = x.get("react_status", "")
        print(f"    {x['accepted_et']} {x['session']} {x['ticker']:6} {r:28} {x['company'][:36]}")


if __name__ == "__main__":
    main()
