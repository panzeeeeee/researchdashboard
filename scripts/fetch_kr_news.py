"""한국 탭 2단계 -- 한국 시장 주요 뉴스.

fetch_hot_news.py 의 짝. 시장 전체를 겨냥한 넓은 검색어로 한국 기사를 모으고
(fetch_news.gather 재사용: 네이버 API 키가 있으면 네이버, 없으면 구글 RSS),
같은 사건끼리 묶고(cluster_by_event 재사용), 오늘 지수·업종 등락과 엮어
"왜 움직였나 / 지켜볼 것" 해설을 붙인다.

읽는 파일: docs/data/kr_market.json (없어도 되고, 있으면 해설에 등락률을 쓴다)
만드는 파일: docs/data/kr_news.json
뉴스를 하나도 못 모으면 기존 파일을 건드리지 않는다.
"""

import sys

from common import DATA_DIR, ask_gemini, env, now_kst, read_json, write_json
from fetch_hot_news import cluster_by_event, parse_event_notes
from fetch_news import gather

OUT = DATA_DIR / "kr_news.json"

QUERIES = [
    "코스피 마감",
    "코스닥 마감",
    "증시 외국인 기관 순매수",
    "원달러 환율 마감",
    "한국은행 기준금리",
    "수출 반도체 실적",
    "증시 전망",
]


def build_prompt(news_items, market):
    heads = "\n".join(f"- {it['title']}" for it in news_items[:15])
    idx = "\n".join(f"{i['label']}: {i['chg']:+}%" for i in (market or {}).get("indexes") or []
                    if i.get("chg") is not None)
    secs = "\n".join(f"{s['label']}: {s['d1']:+}%" for s in
                     sorted((market or {}).get("sectors") or [],
                            key=lambda s: -(s.get("d1") or 0)) if s.get("d1") is not None)
    return (
        "아래는 오늘 한국 증시 관련 주요 뉴스 헤드라인과 지수·업종(ETF) 등락률이다.\n\n"
        f"[뉴스 헤드라인]\n{heads}\n\n[지수 등락률]\n{idx or '없음'}\n\n"
        f"[업종 등락률(1일)]\n{secs or '없음'}\n\n"
        "이 뉴스들과 등락을 실제로 연결해서 설명해라. 막연한 일반론 말고 "
        "오늘 헤드라인에 나온 내용을 근거로 써라. 다른 말 없이 딱 이 형식으로:\n"
        "[오늘 왜 이렇게 움직였나]\n"
        "어떤 이벤트·뉴스 때문에 어떤 업종이 좋았고 어떤 업종이 약했는지, 인과관계를 3문장 이내로.\n"
        "[앞으로 지켜볼 것]\n"
        "이 흐름이 이어지거나 바뀌려면 어떤 이벤트·지표를 지켜봐야 하는지 1~2문장.\n"
        "확실하지 않은 인과관계는 '~영향으로 보인다'처럼 완화해서 말해라. 투자 조언은 하지 마라. "
        "마크다운이나 별표(**) 같은 강조 기호는 쓰지 말고 평범한 문장으로만 써라."
    )


def main():
    creds = {
        "naver_id": env("NAVER_CLIENT_ID"),
        "naver_secret": env("NAVER_CLIENT_SECRET"),
        "gemini": env("GEMINI_API_KEY"),
    }
    if not creds["naver_id"]:
        print("네이버 키 없음 -- 구글 뉴스 RSS로 동작합니다.", file=sys.stderr)

    news = gather(QUERIES, creds, limit=35, market="kr")
    if not news:
        print("한국 뉴스를 하나도 못 모았습니다 -- 기존 파일 유지", file=sys.stderr)
        return

    key = creds["gemini"]
    notes = {"why": "", "watch": ""}
    if key:
        text = ask_gemini(build_prompt(news, read_json(DATA_DIR / "kr_market.json")), key)
        if text:
            notes = parse_event_notes(text)
    else:
        print("GEMINI_API_KEY 없음 -- 해설과 사건 묶기는 생략합니다.", file=sys.stderr)

    clusters = cluster_by_event(news, key, topic="한국 증시")

    write_json(OUT, {
        "updated_at": now_kst().isoformat(),
        "event_notes": notes,
        "clusters": clusters,
        "items": news,          # 클러스터가 비면 화면이 이걸로 폴백한다
    })
    print(f"저장 완료 (기사 {len(news)}건 -> 사건 {len(clusters)}개)")


if __name__ == "__main__":
    main()
