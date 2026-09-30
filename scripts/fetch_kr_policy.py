"""경제 정책 뉴스 -- 미국 탭에는 없는 한국 전용 화면.

1. 분야별 검색어로 최근 48시간 정책 기사를 모은다 (fetch_news.gather 재사용: 네이버 API HUB 우선, 없으면 구글 뉴스).
2. 정부 부처 공식 보도자료 RSS(정책브리핑)를 받을 수 있으면 함께 담는다(안 되면 조용히 건너뛴다).
3. Gemini 한 번에: 기사마다 '영향을 줄 업종'과 한 줄 영향을 붙이고, 전체를 3줄로 요약한다(자동 생성 -- 화면에 그렇게 표시).

결과: docs/data/kr_policy.json
"""

import re
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from common import DATA_DIR, KST, ask_gemini, env, get, now_kst, write_json
from fetch_news import LOOKBACK_HOURS, gather

CATEGORIES = [
    ("금리·통화", ["한국은행 금통위", "기준금리 인하", "기준금리 동결", "한국은행 총재 발언"]),
    ("재정·세제", ["세제 개편", "기획재정부 발표", "추경 편성", "감세 정책"]),
    ("부동산·가계부채", ["부동산 대책", "가계부채 관리 방안", "대출 규제 DSR", "주택 공급 대책"]),
    ("산업·규제", ["산업통상자원부 발표", "공정거래위원회 제재", "금융위원회 발표", "밸류업 프로그램", "반도체 지원 정책"]),
    ("무역·환율", ["관세 협상", "통상 정책", "외환당국 개입", "수출 지원 대책"]),
]
EXCLUDE = ["부고", "로또", "인사", "채용"]
PER_CAT = 15
IMPACT_TOP = 8           # 분야마다 이만큼에 영향 업종을 붙인다
OFFICIAL_FEEDS = [       # 정책브리핑(korea.kr) 부처별 보도자료 RSS -- 주소가 바뀌었거나 막히면 건너뛴다
    ("기획재정부", "https://www.korea.kr/rss/dept_moef.xml"),
    ("금융위원회", "https://www.korea.kr/rss/dept_fsc.xml"),
    ("산업통상자원부", "https://www.korea.kr/rss/dept_motie.xml"),
    ("국토교통부", "https://www.korea.kr/rss/dept_molit.xml"),
    ("전체 보도자료", "https://www.korea.kr/rss/pressrelease.xml"),
]


def official_releases():
    out = []
    for dept, url in OFFICIAL_FEEDS:
        try:
            r = get(url, timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:      # noqa: BLE001
            print(f"  공식 보도자료 {dept} 실패: {str(e)[:60]}", file=sys.stderr)
            continue
        n = 0
        for it in root.findall("./channel/item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            if not title or not link:
                continue
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(KST).isoformat()
            except Exception:
                pub = None
            out.append({"dept": dept, "title": title, "url": link, "published": pub})
            n += 1
            if n >= 6:
                break
        print(f"  공식 보도자료 {dept}: {n}건")
        time.sleep(0.3)
    seen, uniq = set(), []
    for x in sorted(out, key=lambda x: x["published"] or "", reverse=True):
        if x["url"] not in seen:
            seen.add(x["url"])
            uniq.append(x)
    return uniq[:20]


def parse_numbered(text):
    """'번호. 내용' 줄들 -> {번호(0부터): 내용}"""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "." not in line[:4]:
            continue
        head, _, body = line.partition(".")
        try:
            out[int(head.strip()) - 1] = body.strip()
        except ValueError:
            continue
    return out


def add_impact(cats, key):
    """기사마다 영향 업종·한 줄 영향. 한 번의 호출로 전부."""
    flat = [(c["name"], it) for c in cats for it in c["items"][:IMPACT_TOP]]
    if not key or not flat:
        return
    lines = "\n".join(f"{i+1}. [{n}] {it['title']}" for i, (n, it) in enumerate(flat))
    prompt = ("아래는 한국 경제 정책 뉴스 제목들이다. 각 뉴스가 영향을 줄 주식시장 업종을 최대 3개(예: 은행, 건설, 반도체)와 "
              "한 줄 영향(30자 이내)을 써라. 제목에 근거해서만 쓰고, 확실하지 않으면 '영향 불분명'이라고 써라. "
              "투자 조언은 하지 마라. 설명 없이 '번호. 업종, 업종 | 한 줄 영향' 형식으로만 출력.\n\n" + lines)
    got = parse_numbered(ask_gemini(prompt, key, timeout=90))
    for i, (_, it) in enumerate(flat):
        s = got.get(i)
        if not s:
            continue
        sec, _, imp = s.partition("|")
        it["sectors"] = [x.strip() for x in re.split(r"[,、]", sec) if x.strip()][:3]
        it["impact"] = imp.strip()


def make_digest(cats, key):
    """이번 정책 이슈 3줄 요약 (분야마다 가장 여러 매체가 다룬 기사 위주)."""
    if not key:
        return []
    picks = []
    for c in cats:
        top = sorted(c["items"], key=lambda x: -(x.get("dup_count") or 0))[:3]
        picks += [f"[{c['name']}] {it['title']}" for it in top]
    if not picks:
        return []
    prompt = ("아래는 최근 한국 경제 정책 뉴스 제목들이다. 지금 시장이 알아야 할 정책 이슈를 3줄로 요약해라. "
              "각 줄은 한 문장, 60자 이내, 제목에 나온 내용만 쓰고 지어내지 마라. 투자 조언 금지. "
              "번호 없이 줄바꿈으로만 3줄 출력.\n\n" + "\n".join(picks))
    text = ask_gemini(prompt, key, timeout=90) or ""
    return [ln.strip(" -•·*") for ln in text.splitlines() if ln.strip()][:3]


def main():
    creds = {"naver_id": env("NAVER_CLIENT_ID"), "naver_secret": env("NAVER_CLIENT_SECRET"), "gemini": env("GEMINI_API_KEY")}
    cats = []
    for name, queries in CATEGORIES:
        print(f"정책 뉴스: {name}")
        items = gather(queries, creds, excludes=EXCLUDE, limit=PER_CAT, market="kr")
        cats.append({"name": name, "items": items})
    total = sum(len(c["items"]) for c in cats)
    if not total:
        print("정책 기사를 하나도 못 모았습니다 -- 기존 파일 유지", file=sys.stderr)
        return

    key = env("GEMINI_API_KEY")
    try:
        add_impact(cats, key)
        digest = make_digest(cats, key)
    except Exception as e:      # noqa: BLE001
        print(f"  해설 실패(기사는 그대로 저장): {e}", file=sys.stderr)
        digest = []
    official = official_releases()

    write_json(DATA_DIR / "kr_policy.json", {
        "updated_at": now_kst().isoformat(), "lookback_hours": LOOKBACK_HOURS,
        "digest": digest, "categories": cats, "official": official})
    print(f"저장 완료 (기사 {total}건 · 공식 보도자료 {len(official)}건 · 요약 {len(digest)}줄)")


if __name__ == "__main__":
    main()
