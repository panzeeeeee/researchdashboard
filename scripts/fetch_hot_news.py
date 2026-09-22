"""주요뉴스 -- 종목·섹터 뉴스와 별개로, 시장 전체 헤드라인급 뉴스를 따로 모은다.

지금까지 "주요뉴스" 화면은 종목별·섹터별 뉴스(각각 좁은 검색어)를 재활용해서
dup_count 순으로 보여줬는데, 각 검색이 너무 좁아 서로 겹칠 일이 적어 빈약했다.
여기서는 시장 전체를 겨냥한 넓은 검색어로 따로 모은다.

fetch_news.py 의 gather() 를 그대로 재사용한다 -- 새 검색/요약 방식을 만들지 않는다.
"""

import sys

from common import DATA_DIR, env, now_kst, write_json
from fetch_news import gather

QUERIES = [
    "stock market today",
    "Wall Street rally sell-off",
    "S&P 500 Nasdaq today",
    "Fed interest rate stocks",
    "earnings today stocks",
]


def main():
    creds = {
        "naver_id": env("NAVER_CLIENT_ID"),
        "naver_secret": env("NAVER_CLIENT_SECRET"),
        "gemini": env("GEMINI_API_KEY"),
    }
    news = gather(QUERIES, creds, limit=20, market="us")

    write_json(DATA_DIR / "hot_news.json", {
        "updated_at": now_kst().isoformat(),
        "items": news,
    })
    print(f"저장 완료 ({len(news)}건)")


if __name__ == "__main__":
    main()
