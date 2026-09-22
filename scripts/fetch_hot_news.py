"""주요뉴스 -- 종목·섹터 뉴스와 별개로, 시장 전체 헤드라인급 뉴스를 따로 모은다.

지금까지 "주요뉴스" 화면은 종목별·섹터별 뉴스(각각 좁은 검색어)를 재활용해서
dup_count 순으로 보여줬는데, 각 검색이 너무 좁아 서로 겹칠 일이 적어 빈약했다.
여기서는 시장 전체를 겨냥한 넓은 검색어로 따로 모은다.

fetch_news.py 의 gather() 를 그대로 재사용한다 -- 새 검색/요약 방식을 만들지 않는다.
"""

import sys

from common import DATA_DIR, env, now_kst, write_json
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


def main():
    creds = {
        "naver_id": env("NAVER_CLIENT_ID"),
        "naver_secret": env("NAVER_CLIENT_SECRET"),
        "gemini": env("GEMINI_API_KEY"),
    }
    news = gather(QUERIES, creds, limit=35, market="us")

    write_json(DATA_DIR / "hot_news.json", {
        "updated_at": now_kst().isoformat(),
        "items": news,
    })
    print(f"저장 완료 ({len(news)}건)")


if __name__ == "__main__":
    main()
