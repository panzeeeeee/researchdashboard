"""경제 지표 -- 물가·고용·성장 9종. 전부 FRED 공개 CSV.

크레딧/유동성 스크립트의 FRED 읽기 방식을 그대로 재사용한다(BOM 대응).

CPI·근원CPI·PCE는 FRED가 '지수'로만 준다. 우리한테 익숙한 '전년比 %'
물가상승률은 12개월 전 대비로 여기서 직접 계산한다(yoy=True).
비농업고용은 '증감(전월 대비)'이 의미 있어서 diff로 바꾼다(mom_diff=True).
나머지(실업률·GDP성장률 등)는 FRED 값 그대로.

해설은 Gemini로 초보자용 3단락(지금 상황/왜 중요한지/과거엔 이랬다).
ISM은 FRED 무료 배포가 라이선스로 자주 빠져 산업생산(INDPRO)으로 대체했다.
"""

import csv
import io
import sys
import urllib.request

from common import DATA_DIR, ask_gemini, env, now_kst, write_json

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"

# (시리즈ID, 라벨, 그룹, 단위, 변환)
# 변환: "yoy"=전년比%, "mom_diff"=전월대비 증감, None=원값
INDICATORS = [
    ("CPIAUCSL", "소비자물가 CPI", "물가", "% (전년比)", "yoy"),
    ("CPILFESL", "근원 CPI", "물가", "% (전년比)", "yoy"),
    ("PCEPI", "PCE 물가", "물가", "% (전년比)", "yoy"),
    ("UNRATE", "실업률", "고용", "%", None),
    ("PAYEMS", "비농업고용 증감", "고용", "천명 (전월比)", "mom_diff"),
    ("ICSA", "신규 실업수당 청구", "고용", "명 (주간)", None),
    ("A191RL1Q225SBEA", "실질 GDP 성장률", "성장", "% (연율)", None),
    ("UMCSENT", "소비자심리지수", "성장", "지수", None),
    ("INDPRO", "산업생산", "성장", "지수", None),
]


def fred_rows(series_id, n=200):
    """최근 n개 (날짜, 값), 최신순. 헤더는 위치로 건너뛴다(BOM 대응)."""
    try:
        with urllib.request.urlopen(FRED_URL.format(series_id), timeout=20) as r:
            text = r.read().decode("utf-8-sig")
        reader = list(csv.reader(io.StringIO(text)))
        rows = [(d, float(v)) for d, v in reader[1:] if v not in ("", ".")]
        rows.sort(key=lambda x: x[0])
        return rows[-n:]
    except Exception as e:
        print(f"  FRED {series_id} 실패: {e}", file=sys.stderr)
        return []


def entry(series_id, label, group, unit, transform):
    rows = fred_rows(series_id)  # 오래된 것부터
    if len(rows) < 2:
        return {"label": label, "group": group, "unit": unit,
                "value": None, "prev": None, "date": None, "history": []}

    if transform == "yoy":
        # 12개월 전 대비 상승률. 월간 계열이라 13개 이상 필요
        conv = []
        for i in range(12, len(rows)):
            d, v = rows[i]
            base = rows[i - 12][1]
            if base:
                conv.append((d, round((v / base - 1) * 100, 2)))
        rows = conv
    elif transform == "mom_diff":
        conv = [(rows[i][0], round(rows[i][1] - rows[i - 1][1], 1))
                for i in range(1, len(rows))]
        rows = conv
    else:
        rows = [(d, round(v, 2)) for d, v in rows]

    if len(rows) < 2:
        return {"label": label, "group": group, "unit": unit,
                "value": None, "prev": None, "date": None, "history": []}

    d0, v0 = rows[-1]
    prev = rows[-2][1]
    # 히스토리는 차트용, 너무 길면 자름 (월간 20년치면 240개)
    hist = [{"date": d, "value": v} for d, v in rows[-260:]]
    return {"label": label, "group": group, "unit": unit,
            "value": v0, "prev": prev, "date": d0, "history": hist}


def build_prompt(items):
    picks = {e["label"]: e for e in items}
    def line(lb):
        e = picks.get(lb)
        return f"{lb}: {e['value']}{e['unit']}" if e and e["value"] is not None else None
    keys = ["소비자물가 CPI", "근원 CPI", "실업률", "비농업고용 증감",
            "실질 GDP 성장률", "산업생산"]
    block = "\n".join(x for x in (line(k) for k in keys) if x)

    return (
        "아래는 오늘 자 미국 주요 경제지표다. 금융 초보자가 이해할 해설을 써라.\n\n"
        f"{block}\n\n"
        "다음 3개 항목을 순서대로, 다른 말 없이 이 형식으로만 출력해라:\n"
        "[지금 상황]\n물가·고용·성장이 지금 어떤 상태인지 쉬운 말로 2~3문장.\n"
        "[왜 중요한지]\n이게 연준 금리나 증시에 왜 중요한지 1~2문장.\n"
        "[과거엔 이랬다]\n비슷한 지표 조합이 나타났을 때 일반적으로 어땠는지 2~3문장. "
        "구체적 날짜나 정확한 수치를 지어내지 말고 일반적 패턴으로만.\n"
        "투자 조언은 하지 마라. 마크다운이나 별표는 쓰지 마라."
    )


def parse_sections(text):
    keys = {"[지금 상황]": "situation", "[왜 중요한지]": "why", "[과거엔 이랬다]": "history"}
    out = {"situation": "", "why": "", "history": ""}
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
    items = [entry(sid, label, group, unit, tf)
             for sid, label, group, unit, tf in INDICATORS]

    api_key = env("GEMINI_API_KEY")
    notes = {"situation": "", "why": "", "history": ""}
    if api_key and any(e["value"] is not None for e in items):
        text = ask_gemini(build_prompt(items), api_key)
        if text:
            notes = parse_sections(text)
    elif not api_key:
        print("GEMINI_API_KEY 없음 -- 해설은 생략합니다.", file=sys.stderr)

    write_json(DATA_DIR / "macro.json", {
        "updated_at": now_kst().isoformat(),
        "indicators": items,
        "notes": notes,
    })
    print(f"저장 완료 ({sum(1 for e in items if e['value'] is not None)}/{len(items)}종)")


if __name__ == "__main__":
    main()
