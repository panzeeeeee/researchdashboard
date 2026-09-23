"""크레딧 스프레드 -- 회사채가 국채 대비 얼마나 더 얹어주는지 (신용 위험의 온도계).

전부 FRED 공개 CSV(ICE BofA OAS 시리즈). 유동성 스크립트의 FRED 읽기 방식을
그대로 재사용한다 -- 첫 줄(헤더)은 위치로 건너뛴다(BOM 대응).

스프레드가 벌어지면(widening) 위험 회피, 좁혀지면(tightening) 위험 선호.
하이일드(정크본드) 쪽이 더 민감하다. 500bp(5%p) 넘으면 역사적으로 침체 신호.

해설은 Gemini로 초보자용 4단락(용어/지금 상황/왜 중요한지/과거엔 이랬다).
투자 조언은 하지 않는다 -- PROJECT.md 원칙.
"""

import csv
import io
import sys
import urllib.request

from common import DATA_DIR, ask_gemini, env, now_kst, write_json

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"

# ICE BofA Option-Adjusted Spread (OAS), 단위 %
MAIN = [
    ("BAMLH0A0HYM2", "하이일드 (HY)"),
    ("BAMLC0A0CM", "투자등급 (IG)"),
]
BY_RATING = [
    ("BAMLC0A4CBBB", "BBB"),
    ("BAMLH0A1HYBB", "BB"),
    ("BAMLH0A2HYB", "B"),
    ("BAMLH0A3HYC", "CCC 이하"),
]


def fred_rows(series_id, n=1300):
    """최근 n개 (날짜, 값), 최신순. 헤더는 위치로 건너뛴다(BOM 대응)."""
    try:
        with urllib.request.urlopen(FRED_URL.format(series_id), timeout=20) as r:
            text = r.read().decode("utf-8-sig")
        reader = list(csv.reader(io.StringIO(text)))
        rows = [(d, float(v)) for d, v in reader[1:] if v not in ("", ".")]
        rows.sort(key=lambda x: x[0])
        return rows[-n:][::-1]
    except Exception as e:
        print(f"  FRED {series_id} 실패: {e}", file=sys.stderr)
        return []


def spread_entry(series_id, label, with_history=False):
    rows = fred_rows(series_id, n=1300 if with_history else 30)
    if not rows:
        return {"label": label, "value": None, "wow": None, "date": None,
                **({"history": []} if with_history else {})}
    d0, v0 = rows[0]
    # 1주 변화: 6일 이상 전 관측치 중 가장 가까운 값
    from datetime import date, timedelta
    target = date.fromisoformat(d0) - timedelta(days=6)
    prev = next((v for d, v in rows if date.fromisoformat(d) <= target), None)
    entry = {
        "label": label,
        "value": round(v0, 2),
        "wow": round(v0 - prev, 2) if prev is not None else None,
        "date": d0,
    }
    if with_history:
        # 오래된 것부터 -> 화면 차트용
        entry["history"] = [{"date": d, "value": round(v, 2)} for d, v in rows[::-1]]
    return entry


def build_prompt(hy, ig):
    return (
        "아래는 오늘 자 미국 회사채 신용 스프레드다. 금융 초보자가 이해할 해설을 써라.\n\n"
        f"하이일드(정크본드) 스프레드: {hy.get('value')}%p (1주 변화 {hy.get('wow')})\n"
        f"투자등급 스프레드: {ig.get('value')}%p (1주 변화 {ig.get('wow')})\n\n"
        "참고: 스프레드는 회사채가 국채보다 얼마나 더 높은 금리를 주는지다. "
        "벌어지면 위험 회피, 좁혀지면 위험 선호. 하이일드 5%p 이상은 역사적으로 "
        "침체 신호로 여겨진다.\n\n"
        "다음 4개 항목을 순서대로, 다른 말 없이 이 형식으로만 출력해라:\n"
        "[용어]\n크레딧 스프레드가 뭔지 쉬운 말로 한두 문장.\n"
        "[지금 상황]\n오늘 수치가 뭘 뜻하는지 2문장 이내.\n"
        "[왜 중요한지]\n1~2문장.\n"
        "[과거엔 이랬다]\n비슷한 국면에 일반적으로 어땠는지 2~3문장. 구체적 날짜나 "
        "정확한 수치를 지어내지 말고 일반적 패턴으로만.\n"
        "투자 조언은 하지 마라. 마크다운이나 별표는 쓰지 마라."
    )


def parse_sections(text):
    keys = {"[용어]": "terms", "[지금 상황]": "situation",
            "[왜 중요한지]": "why", "[과거엔 이랬다]": "history"}
    out = {"terms": "", "situation": "", "why": "", "history": ""}
    cur = None
    for line in (text or "").splitlines():
        line = line.strip()
        hit = next((v for k, v in keys.items() if line.startswith(k)), None)
        if hit:
            cur = hit
            continue
        if cur and line:
            out[cur] = (out[cur] + " " + line).strip()
    return out


def main():
    hy = spread_entry("BAMLH0A0HYM2", "하이일드 (HY)", with_history=True)
    ig = spread_entry("BAMLC0A0CM", "투자등급 (IG)", with_history=True)
    ratings = [spread_entry(sid, label) for sid, label in BY_RATING]

    api_key = env("GEMINI_API_KEY")
    notes = {"terms": "", "situation": "", "why": "", "history": ""}
    if api_key and hy.get("value") is not None:
        text = ask_gemini(build_prompt(hy, ig), api_key)
        if text:
            notes = parse_sections(text)
    elif not api_key:
        print("GEMINI_API_KEY 없음 -- 해설은 생략합니다.", file=sys.stderr)

    write_json(DATA_DIR / "credit.json", {
        "updated_at": now_kst().isoformat(),
        "high_yield": hy,
        "investment_grade": ig,
        "by_rating": ratings,
        "notes": notes,
    })
    print("저장 완료")


if __name__ == "__main__":
    main()
