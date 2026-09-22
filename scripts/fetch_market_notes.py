"""미국 시황 해설 — 초보자도 알아듣게, 오늘 시장이 무슨 뜻인지 짧게 설명한다.

market.json 이 이미 있어야 돈다. fetch_liquidity_notes.py 와 같은 4단락
구조 — [용어] [지금 상황] [왜 중요한지] [과거엔 이랬다] — 를 그대로 따른다.

투자 전략 제안은 하지 않는다. PROJECT.md 원칙: AI 요약은 검증되지 않은
것으로 취급하고, 판단 근거가 아니라 실마리로만 쓴다.
"""

import sys

from common import DATA_DIR, ask_gemini, env, now_kst, read_json, write_json

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


def build_prompt(m):
    idx = {r["name"]: r for r in (m.get("indexes") or [])}
    lines = []
    for label in ("S&P 500", "나스닥 종합", "러셀 2000"):
        r = idx.get(label)
        if r and r.get("chg") is not None:
            lines.append(f"{label}: {r['chg']}%")

    spread = m.get("breadth_spread")
    if spread is not None:
        lines.append(f"동일가중 RSP - 시총가중 SPY 스프레드: {spread}%p")

    sectors = sorted((m.get("sectors") or []),
                      key=lambda r: (r.get("chg") is None, -(r.get("chg") or 0)))
    if sectors:
        best, worst = sectors[0], sectors[-1]
        lines.append(f"가장 강한 섹터: {best['name']} {best.get('chg')}%")
        lines.append(f"가장 약한 섹터: {worst['name']} {worst.get('chg')}%")

    risk = {r["label"]: r for r in (m.get("risk") or [])}
    vix = risk.get("VIX")
    if vix and vix.get("value") is not None:
        lines.append(f"VIX: {vix['value']}")

    data_block = "\n".join(lines)
    return (
        "아래는 오늘 자 미국 주식시장 현황이다. 금융 초보자가 이해할 해설을 써라.\n\n"
        f"{data_block}\n\n" + COMMON_RULES
    )


def parse_sections(text):
    sections = {k: "" for k in SECTION_KEYS}
    key = None
    for line in (text or "").splitlines():
        line = line.strip()
        matched = None
        for tag, k in zip(SECTION_TAGS, SECTION_KEYS):
            if line.startswith(tag):
                matched = k
                break
        if matched:
            key = matched
            continue
        if key and line:
            sections[key] = (sections[key] + " " + line).strip()
    return sections


def main():
    m = read_json(DATA_DIR / "market.json")
    if not m:
        print("market.json 이 아직 없습니다. 건너뜁니다.", file=sys.stderr)
        return

    api_key = env("GEMINI_API_KEY")
    notes = {k: "" for k in SECTION_KEYS}
    if api_key:
        text = ask_gemini(build_prompt(m), api_key)
        if text:
            notes = parse_sections(text)
    else:
        print("GEMINI_API_KEY 없음 — 해설은 생략합니다.", file=sys.stderr)

    write_json(DATA_DIR / "market_notes.json", {
        "updated_at": now_kst().isoformat(),
        "notes": notes,
    })
    print("저장 완료")


if __name__ == "__main__":
    main()
