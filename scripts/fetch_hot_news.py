"""주요뉴스 -- 종목·섹터 뉴스와 별개로, 시장 전체 헤드라인급 뉴스를 따로 모은다.

지금까지 "주요뉴스" 화면은 종목별·섹터별 뉴스(각각 좁은 검색어)를 재활용해서
dup_count 순으로 보여줬는데, 각 검색이 너무 좁아 서로 겹칠 일이 적어 빈약했다.
여기서는 시장 전체를 겨냥한 넓은 검색어로 따로 모은다.

fetch_news.py 의 gather() 를 그대로 재사용한다 -- 새 검색/요약 방식을 만들지 않는다.
"""

import sys

from common import DATA_DIR, ask_gemini, env, now_kst, read_json, write_json
from fetch_news import gather

# 주요 매체로 좁힌다 -- 범용 검색어만 쓰면 인도 Sensex, 일본 개별종목처럼
# 미국 시장과 관련 적은 국제 뉴스가 섞여 들어온다. 건수가 너무 적어서
# 매체를 8개로 넓히고 검색어도 더 다양하게 늘렸다.
_SITES = ("(site:reuters.com OR site:cnbc.com OR site:bloomberg.com OR "
          "site:wsj.com OR site:marketwatch.com OR site:barrons.com OR "
          "site:finance.yahoo.com OR site:investing.com)")
QUERIES = [
    f"{_SITES} stock market today",
    f"{_SITES} S&P 500 Nasdaq",
    f"{_SITES} Fed interest rate",
    f"{_SITES} earnings today",
    f"{_SITES} stocks movers gainers losers",
    f"{_SITES} Wall Street outlook",
]


def build_event_prompt(news_items, market):
    """모아온 헤드라인 + 섹터 등락률을 엮어서 원인-결과로 설명하게 한다."""
    headlines = "\n".join(f"- {it['title']}" for it in news_items[:15])
    sectors = sorted((market or {}).get("sectors") or [],
                      key=lambda r: (r.get("chg") is None, -(r.get("chg") or 0)))
    sector_lines = "\n".join(f"{s['name']}: {s.get('chg')}%" for s in sectors)

    return (
        "아래는 오늘 미국 시장 관련 주요 뉴스 헤드라인과 섹터별 등락률이다.\n\n"
        f"[뉴스 헤드라인]\n{headlines}\n\n"
        f"[섹터별 등락률]\n{sector_lines}\n\n"
        "이 뉴스들과 섹터 등락을 실제로 연결해서 설명해라. 막연한 일반론 말고 "
        "오늘 헤드라인에 나온 내용을 근거로 써라. 다른 말 없이 딱 이 형식으로:\n"
        "[오늘 왜 이렇게 움직였나]\n"
        "어떤 이벤트·뉴스 때문에 어떤 섹터가 좋았고 어떤 섹터는 두들겨 맞았는지, "
        "인과관계를 3문장 이내로.\n"
        "[앞으로 지켜볼 것]\n"
        "이 흐름이 이어지거나 바뀌려면 어떤 이벤트·지표를 지켜봐야 하는지 1~2문장.\n"
        "확실하지 않은 인과관계는 '~영향으로 보인다'처럼 단정하지 말고 완화해서 "
        "말해라. 투자 조언은 하지 마라. 마크다운이나 별표(**) 같은 강조 기호는 "
        "쓰지 말고 평범한 문장으로만 써라."
    )


def parse_event_notes(text):
    notes = {"why": "", "watch": ""}
    key = None
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("[오늘 왜 이렇게 움직였나]"):
            key = "why"; continue
        if line.startswith("[앞으로 지켜볼 것]"):
            key = "watch"; continue
        if key and line:
            notes[key] = (notes[key] + " " + line).strip()
    return notes


def main():
    creds = {
        "naver_id": env("NAVER_CLIENT_ID"),
        "naver_secret": env("NAVER_CLIENT_SECRET"),
        "gemini": env("GEMINI_API_KEY"),
    }
    news = gather(QUERIES, creds, limit=35, market="us")

    api_key = creds["gemini"]
    event_notes = {"why": "", "watch": ""}
    if api_key and news:
        market = read_json(DATA_DIR / "market.json")
        text = ask_gemini(build_event_prompt(news, market), api_key)
        if text:
            event_notes = parse_event_notes(text)
    elif not api_key:
        print("GEMINI_API_KEY 없음 -- 이벤트 해설은 생략합니다.", file=sys.stderr)

    write_json(DATA_DIR / "hot_news.json", {
        "updated_at": now_kst().isoformat(),
        "event_notes": event_notes,
        "items": news,
    })
    print(f"저장 완료 ({len(news)}건)")


if __name__ == "__main__":
    main()
