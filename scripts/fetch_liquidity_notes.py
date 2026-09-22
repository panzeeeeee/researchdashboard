"""유동성·자금시장 해설 — 초보자도 알아듣게, 용어부터 설명한다.

liquidity.json · money_market.json 이 이미 있어야 돈다.
글로벌 유동성용 해설(liquidity_notes.json)과 자금시장 상세용 해설
(money_market_notes.json)을 둘 다 만든다. 둘 다 같은 4단락 구조:
[용어] [지금 상황] [왜 중요한지] [과거엔 이랬다]

정책 관련 뉴스는 글로벌 유동성 쪽에만 붙인다 (fetch_news.py 재사용).

투자 전략 제안은 하지 않는다. PROJECT.md 원칙: AI 요약은 검증되지 않은
것으로 취급하고, 판단 근거가 아니라 실마리로만 쓴다.
"""

import sys

from common import DATA_DIR, ask_gemini, env, now_kst, read_json, write_json
from fetch_news import gather

NEWS_QUERIES = [
    "Fed reverse repo reserves",
    "Treasury quarterly refunding QRA",
    "Fed balance sheet QT taper",
]

SECTION_TAGS = ["[용어]", "[지금 상황]", "[왜 중요한지]", "[과거엔 이랬다]"]
SECTION_KEYS = ["terms", "situation", "why", "history"]

COMMON_RULES = (
    "다음 4개 항목을 순서대로, 다른 말 없이 딱 이 형식으로만 출력해라:\n"
    "[용어]\n"
    "방금 나온 지표들이 각각 뭔지 금융을 잘 모르는 사람도 알아듣게 한두 문장씩, "
    "전문용어가 나오면 바로 쉬운 말로 풀어서 설명해라.\n"
    "[지금 상황]\n"
    "오늘 숫자가 정확히 뭘 뜻하는지 쉬운 말로 2문장 이내.\n"
    "[왜 중요한지]\n"
    "이게 왜 신경 쓸 일인지 1~2문장.\n"
    "[과거엔 이랬다]\n"
    "역사적으로 비슷한 상황이 있었을 때 일반적으로 어땠는지 2~3문장. "
    "구체적인 날짜나 정확한 수치를 지어내지 말고, 확실히 아는 범위에서만 "
    "일반적인 패턴으로 말해라.\n"
    "투자 조언이나 '사라/팔아라' 식 제안은 절대 쓰지 마라 — 설명만 해라. "
    "특정 종목명도 쓰지 마라."
)


def build_liquidity_prompt(liq):
    comp = {c["label"]: c for c in (liq.get("components") or [])}
    nl = liq.get("net_liquidity") or {}
    lines = [f"미국 순유동성(Fed자산-TGA-RRP): {nl.get('value')}십억$ "
             f"(1주 변화 {nl.get('wow')})"]
    for label in ("Fed 총자산", "TGA · 재무부 일반계정", "ON RRP", "은행 준비금 · 주간 평균"):
        c = comp.get(label)
        if c and c.get("value") is not None:
            lines.append(f"{label}: {c['value']}십억$ (1주 변화 {c.get('wow')})")
    data_block = "\n".join(lines)

    return (
        "아래는 오늘 자 미국 연준 유동성 지표다. 금융 초보자가 이해할 해설을 써라.\n\n"
        f"{data_block}\n\n" + COMMON_RULES
    )


def build_money_market_prompt(mm):
    rates = {r["label"]: r for r in (mm.get("rates") or [])}
    lines = []
    for label in ("SOFR", "EFFR", "IORB", "OBFR", "TGCR"):
        r = rates.get(label)
        if r and r.get("value") is not None:
            lines.append(f"{label}: {r['value']}%")
    data_block = "\n".join(lines)

    return (
        "아래는 오늘 자 미국 단기 자금시장 금리다. 금융 초보자가 이해할 해설을 써라. "
        "SOFR·EFFR·IORB·OBFR·TGCR가 서로 뭐가 다른 금리인지도 짚어줘라.\n\n"
        f"{data_block}\n\n" + COMMON_RULES
    )


def parse_sections(text):
    sections = {k: "" for k in SECTION_KEYS}
    key = None
    for line in (text or "").splitlines():
        line = line.strip()
        matched_tag = None
        for tag, k in zip(SECTION_TAGS, SECTION_KEYS):
            if line.startswith(tag):
                matched_tag = k
                break
        if matched_tag:
            key = matched_tag
            continue
        if key and line:
            sections[key] = (sections[key] + " " + line).strip()
    return sections


def run_notes(prompt, api_key):
    if not api_key:
        return {k: "" for k in SECTION_KEYS}
    text = ask_gemini(prompt, api_key)
    return parse_sections(text) if text else {k: "" for k in SECTION_KEYS}


def main():
    liq = read_json(DATA_DIR / "liquidity.json")
    mm = read_json(DATA_DIR / "money_market.json")
    api_key = env("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY 없음 — 해설은 생략합니다.", file=sys.stderr)

    if liq:
        notes = run_notes(build_liquidity_prompt(liq), api_key)
        creds = {"naver_id": env("NAVER_CLIENT_ID"),
                 "naver_secret": env("NAVER_CLIENT_SECRET"), "gemini": api_key}
        news = gather(NEWS_QUERIES, creds, limit=6, market="us")
        write_json(DATA_DIR / "liquidity_notes.json", {
            "updated_at": now_kst().isoformat(), "notes": notes, "news": news,
        })
        print(f"글로벌 유동성 해설 저장 완료 (뉴스 {len(news)}건)")
    else:
        print("liquidity.json 이 아직 없습니다. 유동성 해설을 건너뜁니다.", file=sys.stderr)

    if mm:
        notes = run_notes(build_money_market_prompt(mm), api_key)
        write_json(DATA_DIR / "money_market_notes.json", {
            "updated_at": now_kst().isoformat(), "notes": notes,
        })
        print("자금시장 해설 저장 완료")
    else:
        print("money_market.json 이 아직 없습니다. 자금시장 해설을 건너뜁니다.", file=sys.stderr)


if __name__ == "__main__":
    main()
