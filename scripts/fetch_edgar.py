"""SEC EDGAR에서 분기 실적 보도자료(8-K) 원문을 받아 쌓는다.

미국 정부가 공개하는 공시 문서라 저작권이 없다. 원문을 그대로 저장해도 되고,
컨콜에서 경영진이 읽는 준비된 발언은 대부분 이 보도자료 내용과 겹친다.

받는 것
  8-K 중 Item 2.02(실적 발표)에 해당하는 건의 EX-99 첨부(보도자료 본문).

쌓는 방식
  종목별 파일에 누적한다. 이미 받은 건은 다시 받지 않으므로
  하루하루 조금씩만 늘어난다.

SEC 요청 예절
  User-Agent 에 연락처를 넣어야 하고, 초당 10건을 넘기면 안 된다.
"""

import json
import re
import sys
import time
from datetime import date

from common import DATA_DIR, ROOT, env, get, now_kst, read_json, write_json

EDGAR_DIR = DATA_DIR / "edgar"
TICKER_MAP = ROOT / "screener" / "ticker_cik.json"
YEARS_BACK = 3
MAX_FILINGS = 12
MAX_CHARS = 120_000      # 보도자료 한 건의 저장 상한
PAUSE = 0.15             # 초당 10건 제한을 넉넉히 지킨다

UA = env("SEC_USER_AGENT",
         "research-dashboard/1.0 (personal research; contact via GitHub)")
HEAD = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}

TAG = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
ANY_TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"[ \t\u00a0]+")
BLANK = re.compile(r"\n{3,}")


def sec_get(url, as_json=False):
    try:
        r = get(url, headers=HEAD, timeout=30)
        r.raise_for_status()
        time.sleep(PAUSE)
        return r.json() if as_json else r.text
    except Exception as e:
        print(f"  요청 실패 {url}: {e}", file=sys.stderr)
        return None


def load_cik_map():
    """티커 -> CIK 대응표. 한 번 받아 저장해두고 재사용한다."""
    cached = read_json(TICKER_MAP)
    if cached:
        return cached
    data = sec_get("https://www.sec.gov/files/company_tickers.json", as_json=True)
    if not data:
        return {}
    out = {}
    for row in (data.values() if isinstance(data, dict) else data):
        t = str(row.get("ticker", "")).upper()
        cik = row.get("cik_str") or row.get("cik")
        if t and cik is not None:
            out[t] = f"{int(cik):010d}"
    write_json(TICKER_MAP, out)
    print(f"  CIK 대응표 {len(out)}건 저장")
    return out


def to_text(html):
    s = TAG.sub(" ", html)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", s, flags=re.I)
    s = ANY_TAG.sub(" ", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&rsquo;", "'"),
                 ("&ldquo;", '"'), ("&rdquo;", '"'), ("&mdash;", "—")):
        s = s.replace(a, b)
    s = re.sub(r"&#\d+;", " ", s)
    s = WS.sub(" ", s)
    s = "\n".join(line.strip() for line in s.splitlines())
    return BLANK.sub("\n\n", s).strip()


def earnings_filings(cik):
    """Item 2.02(실적 발표)가 붙은 8-K 목록."""
    data = sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json", as_json=True)
    if not data:
        return []
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    cutoff = date.today().replace(year=date.today().year - YEARS_BACK).isoformat()

    strict, loose = [], []
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        filed = (recent.get("filingDate") or [""] * len(forms))[i]
        if filed < cutoff:
            continue
        acc = (recent.get("accessionNumber") or [""] * len(forms))[i]
        row = {"accession": acc, "date": filed, "folder": acc.replace("-", "")}
        items = (recent.get("items") or [""] * len(forms))[i] or ""
        if "2.02" in items:
            strict.append(row)
        else:
            # items 칸을 비워 내는 제출인이 있다. 나중에 본문으로 걸러낸다.
            loose.append(row)

    if strict:
        return strict[:MAX_FILINGS]
    print("    (item 2.02 표기 없음 — 본문으로 판별합니다)")
    return loose[:MAX_FILINGS * 2]


EX_PAT = re.compile(r"ex[\W_]*99|exhibit[\W_]*99|press[\W_]*release|earnings")


def candidate_files(files):
    """보도자료일 법한 첨부를 우선순위대로 고른다.

    회사마다 파일 이름 방식이 제각각이라(ex-99.1, ex991, exhibit99, 회사명xex991…)
    이름으로 한 번 거르고, 못 찾으면 덩치 큰 문서부터 본문으로 판별한다.
    """
    docs = []
    for f in files:
        name = str(f.get("name", ""))
        low = name.lower()
        if not low.endswith((".htm", ".html", ".txt")):
            continue
        size = int(f.get("size") or 0)
        if size < 1500:
            continue
        docs.append((name, low, size))

    named = [d for d in docs if EX_PAT.search(d[1])]
    named.sort(key=lambda d: -d[2])
    rest = [d for d in docs if d not in named]
    rest.sort(key=lambda d: -d[2])
    return [d[0] for d in named] + [d[0] for d in rest[:3]]


def press_release(cik, folder, label=""):
    """제출 폴더에서 실적 보도자료 본문을 찾아 평문으로 돌려준다."""
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{folder}"
    listing = sec_get(f"{base}/index.json", as_json=True)
    if not listing:
        return None, None

    files = ((listing.get("directory") or {}).get("item")) or []
    names = candidate_files(files)
    if not names:
        print(f"    {label}: 첨부 문서 없음", file=sys.stderr)
        return None, None

    tried = []
    for name in names[:4]:
        raw = sec_get(f"{base}/{name}")
        if not raw:
            continue
        text = to_text(raw)
        tried.append(f"{name}({len(text)}자)")
        if len(text) >= 400 and looks_like_earnings(text):
            return text[:MAX_CHARS], f"{base}/{name}"

    print(f"    {label}: 실적 본문 못 찾음 — 검토 {', '.join(tried) or '없음'}",
          file=sys.stderr)
    return None, None


EARNINGS_WORDS = ("quarter", "fiscal", "full year", "results", "earnings",
                  "revenue", "eps")


def looks_like_earnings(text):
    low = text[:4000].lower()
    hits = sum(1 for w in EARNINGS_WORDS if w in low)
    return hits >= 3


def headline(text):
    for line in text.splitlines():
        line = line.strip()
        if 20 <= len(line) <= 160 and not line.isupper():
            return line
    return text[:90].strip()


def main():
    picks = (read_json(DATA_DIR / "picks.json") or {}).get("picks", [])
    targets = [p for p in picks if p.get("market") == "us"][:8]
    if not targets:
        print("대상 종목이 없습니다.")
        return

    cik_map = load_cik_map()
    if not cik_map:
        print("CIK 대응표를 받지 못했습니다.", file=sys.stderr)
        return

    for p in targets:
        t = p["code"].upper()
        cik = cik_map.get(t)
        if not cik:
            print(f"  {t}: CIK 없음")
            continue

        path = EDGAR_DIR / f"{t}.json"
        store = read_json(path) or {"ticker": t, "name": p.get("name") or t,
                                    "filings": []}
        have = {f["accession"] for f in store["filings"]}

        filings = earnings_filings(cik)
        print(f"  {t}: CIK {cik}, 최근 3년 8-K {len(filings)}건")

        added = 0
        for f in filings:
            if f["accession"] in have:
                continue
            text, url = press_release(cik, f["folder"], f"{t} {f['date']}")
            if not text:
                continue
            store["filings"].append({
                "accession": f["accession"],
                "date": f["date"],
                "title": headline(text),
                "url": url,
                "text": text,
            })
            added += 1

        store["filings"].sort(key=lambda x: x["date"], reverse=True)
        store["filings"] = store["filings"][:MAX_FILINGS]
        store["name"] = p.get("name") or store.get("name") or t
        store["updated_at"] = now_kst().isoformat()
        write_json(path, store)
        print(f"  {t}: 보관 {len(store['filings'])}건 (새로 {added}건)")

    index = []
    for path in sorted(EDGAR_DIR.glob("*.json")):
        d = read_json(path) or {}
        if d.get("filings"):
            index.append({
                "ticker": d.get("ticker") or path.stem,
                "name": d.get("name") or path.stem,
                "count": len(d["filings"]),
                "latest": d["filings"][0]["date"],
            })
    write_json(DATA_DIR / "edgar.json",
               {"updated_at": now_kst().isoformat(), "stocks": index})
    print(f"저장 완료 ({len(index)}종목)")


if __name__ == "__main__":
    main()
