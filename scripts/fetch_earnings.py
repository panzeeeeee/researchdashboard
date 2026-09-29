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

쌓는 방식
  docs/data/earnings.json 에 누적. 이미 있는 건 건너뛰고, 90일 지난 건 버린다.

SEC 요청 예절
  fetch_edgar.py 와 같은 User-Agent(SEC_USER_AGENT)와 요청 간격을 쓴다.
"""

import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from common import DATA_DIR, now_kst, read_json, write_json
from fetch_edgar import HEAD, PAUSE, load_cik_map
from common import get

OUT = DATA_DIR / "earnings.json"
FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
        "&type=8-K&company=&dateb=&owner=include&count=100&start={start}&output=atom")
MAX_PAGES = 30           # 100건 x 30쪽 = 3,000건까지 훑는다
LOOKBACK_HOURS = 40      # 이보다 오래된 접수가 나오면 멈춘다
KEEP_DAYS = 90
REACT_DAYS = 10          # 이 기간 안의 발표만 반응을 채우거나 다시 본다
ET = ZoneInfo("America/New_York")

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
    now_et = datetime.now(ET)
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
    today = datetime.now(ET).date()
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

    try:
        fill_reactions(store["items"])
    except Exception as e:
        print(f"  반응 계산 중 오류(수집분은 그대로 저장): {e}", file=sys.stderr)

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
