"""유동성 해석 — 숫자만 있으면 뭘 봐야 할지 모르니, 맥락과 과거 사례를 붙인다.

liquidity.json · money_market.json 이 이미 있어야 돈다.
워크플로우에서 "글로벌 유동성"·"자금시장 상세" 다음 순서로 돌려야 한다.

Gemini로 "왜 이런지 / 과거 비슷한 국면엔 어땠는지"를 설명체로 만들고,
정책 관련 뉴스 몇 건을 같이 붙인다. fetch_news.py 의 검색·요약 로직을
그대로 재사용한다 — 새 방식을 하나 더 만들지 않는다.

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


def build_prompt(liq, mm):
    comp = {c["label"]: c for c in (liq.get("components") or [])}
    nl = liq.get("net_liquidity") or {}
    rates = {r["label"]: r for r in (mm.get("rates") or [])}

    lines = [f"미국 순유동성(Fed자산-TGA-RRP): {nl.get('value')}십억$ "
             f"(1주 변화 {nl.get('wow')})"]
    for label in ("Fed 총자산", "TGA · 재무부 일반계정", "ON RRP", "은행 준비금 · 주간 평균"):
        c = comp.get(label)
        if c and c.get("value") is not None:
            lines.append(f"{label}: {c['value']}십억$ (1주 변화 {c.get('wow')})")
    for label in ("SOFR", "IORB"):
        r = rates.get(label)
        if r and r.get("value") is not None:
            lines.append(f"{label}: {r['value']}%")
    data_block = "\n".join(lines)

    return (
        "아래는 오늘 자 미국 유동성 지표다. 개인 투자자가 참고할 해설을 써라.\n\n"
        f"{data_block}\n\n"
        "다음 형식으로, 다른 말 없이 딱 이 구조로만 출력해라:\n"
        "[국면]\n"
        "지금 상태를 2문장 이내로 — 왜 이런 숫자가 나왔는지, 무엇이 눈에 띄는지.\n"
        "[과거 사례]\n"
        "역사적으로 이런 조합(지준금 급감, RRP 소진, TGA 재충전 등)이 나타났을 때 "
        "일반적으로 어떤 흐름이 있었는지 2~3문장. 구체적인 날짜나 정확한 수치를 "
        "지어내지 말고, 확실히 아는 범위에서만 일반적인 패턴으로 말해라.\n"
        "[과거 섹터 흐름]\n"
        "이런 유동성 국면에서 상대적으로 강했던/약했던 섹터 유형을 한 문장으로 "
        "(예: 방어주 대 경기민감주, 성장주 대 가치주). 특정 종목명은 쓰지 마라.\n"
        "투자 조언이나 '사라/팔아라' 식 제안은 절대 쓰지 마라 — 설명만 해라."
    )


def parse_sections(text):
    sections = {"regime": "", "history": "", "sectors": ""}
    key = None
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("[국면]"):
            key = "regime"; continue
        if line.startswith("[과거 사례]"):
            key = "history"; continue
        if line.startswith("[과거 섹터 흐름]"):
            key = "sectors"; continue
        if key and line:
            sections[key] = (sections[key] + " " + line).strip()
    return sections


def main():
    liq = read_json(DATA_DIR / "liquidity.json")
    mm = read_json(DATA_DIR / "money_market.json")
    if not liq or not mm:
        print("liquidity.json / money_market.json 이 아직 없습니다. 건너뜁니다.",
              file=sys.stderr)
        return

    api_key = env("GEMINI_API_KEY")
    notes = {"regime": "", "history": "", "sectors": ""}
    if api_key:
        text = ask_gemini(build_prompt(liq, mm), api_key)
        if text:
            notes = parse_sections(text)
    else:
        print("GEMINI_API_KEY 없음 — 해설은 생략합니다.", file=sys.stderr)

    creds = {"naver_id": env("NAVER_CLIENT_ID"),
             "naver_secret": env("NAVER_CLIENT_SECRET"),
             "gemini": api_key}
    news = gather(NEWS_QUERIES, creds, limit=6, market="us")

    write_json(DATA_DIR / "liquidity_notes.json", {
        "updated_at": now_kst().isoformat(),
        "notes": notes,
        "news": news,
    })
    print(f"저장 완료 (뉴스 {len(news)}건)")


if __name__ == "__main__":
    main()
