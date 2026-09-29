"""미국 실적 레이더 1단계 — 밤사이 들어온 실적 8-K(Item 2.02)를 모은다.

어디서 받나
  SEC '최신 공시' 피드(Atom). 방금 접수된 8-K가 최신순으로 나오고,
  각 건의 요약에 Item 번호가 붙어 있어서 실적(2.02)만 바로 골라낼 수 있다.
  회사마다 따로 물어보지 않으므로 요청이 수십 번이면 끝난다.
  (일일 목록 파일은 미국 밤 10시 이후에야 나와서 한국 아침 수집에 늦는다.)

무엇을 남기나
  종목, 회사명, 접수 시각(미국 동부), 장전/장중/장후 구분, 공시 원문 링크.
  주가 반응·숫자 요약은 다음 단계에서 붙인다.

쌓는 방식
  docs/data/earnings.json 에 누적. 이미 있는 건 건너뛰고, 90일 지난 건 버린다.

SEC 요청 예절
  fetch_edgar.py 와 같은 User-Agent(SEC_USER_AGENT)와 요청 간격을 쓴다.
"""

import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from common import DATA_DIR, now_kst, read_json, write_json
from fetch_edgar import HEAD, PAUSE, load_cik_map
from common import get

OUT = DATA_DIR / "earnings.json"
FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
        "&type=8-K&company=&dateb=&owner=include&count=100&start={start}&output=atom")
MAX_PAGES = 30           # 100건 x 30쪽 = 3,000건까지 훑는다
LOOKBACK_HOURS = 40      # 이보다 오래된 접수가 나오면 멈춘다
KEEP_DAYS = 90

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


def main():
    store = read_json(OUT) or {"items": []}
    have = {x["accession"] for x in store["items"]}

    cik_map = load_cik_map()            # 티커 -> CIK
    cik_to_ticker = {}
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

    keep_from = (now_kst() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    store["items"] = [x for x in store["items"] if x.get("date_et", "") >= keep_from]
    store["items"].sort(key=lambda x: x.get("accepted_et", ""), reverse=True)
    store["updated_at"] = now_kst().isoformat()
    write_json(OUT, store)

    print(f"  새로 {added}건 (티커 없어 제외 {no_ticker}건), 보관 {len(store['items'])}건")
    for x in store["items"][:10]:
        print(f"    {x['accepted_et']} {x['session']} {x['ticker']:6} {x['company'][:40]}")


if __name__ == "__main__":
    main()
