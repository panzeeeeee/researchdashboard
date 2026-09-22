"""커뮤니티 언급 종목 — 레딧(주로 r/wallstreetbets)에서 많이 언급된 종목.

ApeWisdom 공개 API를 쓴다. 키 없이 되는 무료 서비스인데, 정확한 주소를
문서로 완전히 확인하지 못했다 — 안 되면 Actions 로그를 보고 주소를 고쳐야
할 수도 있다.

다른 스크립트들과 마찬가지로 실패해도 조용히 건너뛴다 (전체 실행을 막지 않음).
"""

import sys

from common import DATA_DIR, get, now_kst, write_json

URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1"
LIMIT = 15


def main():
    try:
        r = get(URL, timeout=20)
        r.raise_for_status()
        rows = r.json().get("results", [])
    except Exception as e:
        print(f"ApeWisdom 실패: {e}", file=sys.stderr)
        return

    out = []
    for row in rows[:LIMIT]:
        ticker = row.get("ticker")
        if not ticker:
            continue
        try:
            mentions = int(row.get("mentions") or 0)
            prev = int(row.get("mentions_24h_ago") or 0)
        except (TypeError, ValueError):
            continue
        out.append({
            "ticker": ticker,
            "name": row.get("name") or ticker,
            "mentions": mentions,
            "chg": round((mentions / prev - 1) * 100, 1) if prev else None,
            "upvotes": row.get("upvotes"),
        })

    if not out:
        print("종목을 하나도 못 받았습니다 — 저장하지 않습니다.", file=sys.stderr)
        return

    write_json(DATA_DIR / "community_buzz.json", {
        "updated_at": now_kst().isoformat(),
        "source": "ApeWisdom (Reddit)",
        "stocks": out,
    })
    print(f"저장 완료 ({len(out)}종목)")


if __name__ == "__main__":
    main()
