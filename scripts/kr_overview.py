"""종목 기업개요 -- 네이버 증권(기업분석, FnGuide 개요)에서 받아 Gemini 로 2줄 요약.

미국판(fetch_deepvalue_cards.batch_overviews)과 같은 방식: 소개글 -> 한국어 짧은 불릿 2개.
  1. 종목마다 한 번만 받아 screener/kr_overview_cache.json 에 저장한다(저장소에 커밋되는 작은 파일). 이미 있으면 다시 안 받는다.
  2. 소개글이 있고 요약이 없는 종목만 모아 Gemini 한 번(40종목씩)에 요약한다.
  3. Gemini 가 안 되면 소개글의 앞 두 문장을 그대로 쓴다(내용은 같고 길 뿐이다).

출처: https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd=종목코드  (class="cmp_comment")
"""

from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from common import ROOT, ask_gemini, env, now_kst

CACHE = ROOT / "screener" / "kr_overview_cache.json"
URL = "https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={code}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; research-dashboard/1.0)"}
BATCH = 40
TEXT_MAX = 500


def _load():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(c):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(c, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def fetch_text(code):
    """소개글. 못 받으면 None."""
    try:
        r = requests.get(URL.format(code=code), headers=UA, timeout=25)
        r.raise_for_status()
        html = r.content.decode(r.encoding or "utf-8", errors="replace")
    except Exception as e:
        print(f"    개요 요청 실패 {code}: {str(e)[:80]}", file=sys.stderr)
        return None
    m = re.search(r'cmp_comment[^>]*>(.*?)</ul>', html, re.S)
    seg = m.group(1) if m else ""
    if not seg:
        i = html.find("cmp_comment")
        seg = html[i:i + 2500] if i >= 0 else ""
    text = re.sub(r"<[^>]+>", " ", seg)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.split("펀더멘털 주요지표")[0].strip()
    return text[:TEXT_MAX] if len(text) > 30 else None


def fallback_bullets(text):
    sents = [s.strip() for s in re.split(r"(?<=[.])\s+", text) if len(s.strip()) > 8]
    return [s if len(s) <= 90 else s[:88] + "…" for s in sents[:2]]


def summarize(entries, key):
    """entries: [(code, name, text)] -> {code: [불릿, 불릿]}"""
    out = {}
    if not key:
        return out
    for i in range(0, len(entries), BATCH):
        part = entries[i:i + BATCH]
        numbered = "\n\n".join(f"{n+1}. {name}: {text}" for n, (_, name, text) in enumerate(part))
        prompt = ("다음은 여러 한국 상장기업의 소개다. 각 기업이 무슨 일을 하는 회사인지 한국어로 핵심만 "
                  "2개의 짧은 불릿으로 요약해라. 각 불릿은 40자 안팎, 소개에 없는 내용은 지어내지 마라. "
                  "형식은 한 줄에 '번호. 불릿1 | 불릿2', 설명 없이 그것만 출력해라.\n\n" + numbered)
        text = ask_gemini(prompt, key, timeout=90)
        if not text:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or "." not in line[:4]:
                continue
            num, _, rest = line.partition(".")
            try:
                j = int(num.strip()) - 1
            except ValueError:
                continue
            if 0 <= j < len(part) and rest.strip():
                bl = [b.strip() for b in rest.split("|") if b.strip()][:2]
                if bl:
                    out[part[j][0]] = bl
        time.sleep(2)
    return out


def attach_overviews(items, workers=4):
    """items: [{'code', 'name', ...}] 각 항목에 'overview'(불릿 리스트)를 붙인다. 캐시를 갱신해 저장한다."""
    cache = _load()
    codes = [x["code"] for x in items if x.get("code")]
    need_text = sorted({c for c in codes if c not in cache or not cache[c].get("text")})
    # 최근에 실패한 종목은 하루 동안 다시 시도하지 않는다
    now = time.time()
    need_text = [c for c in need_text if now - cache.get(c, {}).get("fail", 0) > 86400]
    if need_text:
        print(f"  기업개요 받기 {len(need_text)}종목")

        def job(c):
            t = fetch_text(c)
            time.sleep(0.4)
            return c, t
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for c, t in ex.map(job, need_text):
                if t:
                    cache[c] = {"text": t, "ts": now_kst().isoformat()}
                else:
                    cache[c] = {"fail": now}
    names = {x["code"]: x.get("name", "") for x in items}
    todo = [(c, names.get(c, c), cache[c]["text"]) for c in codes
            if c in cache and cache[c].get("text") and not cache[c].get("bullets")]
    todo = list({c: (c, n, t) for c, n, t in todo}.values())
    if todo:
        print(f"  기업개요 요약 {len(todo)}종목 (Gemini)")
        got = summarize(todo, env("GEMINI_API_KEY"))
        for c, bl in got.items():
            cache[c]["bullets"] = bl
    for x in items:
        e = cache.get(x.get("code"), {})
        x["overview"] = e.get("bullets") or (fallback_bullets(e["text"]) if e.get("text") else [])
    _save(cache)
    print(f"  기업개요 붙임: {sum(1 for x in items if x['overview'])}/{len(items)}종목")
    return items
