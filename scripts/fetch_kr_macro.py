"""한국 경제 지표 -- 한국은행 ECOS. 물가·통화·성장·대외·심리·고용 20여 종.

미국판(fetch_macro.py)과 같은 구성: 지표마다 최신 값·직전 값·10년 이력, 그리고 Gemini 해설 3단락.
통계표·항목 코드는 ECOS 조회 시험(probe_out)으로 확인한 것이다.

변환(transform)
  level    : 원값 그대로 (scale 을 곱할 수 있다)
  yoy      : 전년 같은 기간 대비 % (지수·금액 계열을 우리가 익숙한 증감률로)
  yoy_diff : 전년 같은 기간 대비 차이 (취업자수 증감처럼)
같은 통계표에 항목 축이 둘 이상이면(원계열/계절조정 등) pref2 낱말이 들어간 쪽을 고른다. 고른 결과는 로그에 남긴다.

결과: docs/data/kr_macro.json   (한 지표가 실패해도 나머지는 그대로 만든다)
"""

import sys
import time
import urllib.parse

from common import DATA_DIR, ask_gemini, env, get, now_kst, write_json

BASE = "https://ecos.bok.or.kr/api"

# (key, 이름, 묶음, 통계표, 주기, 항목 코드들, 변환, 단위, 배율, 항목2 선호 낱말)
INDICATORS = [
    ("base_rate", "한국은행 기준금리", "통화·금리", "722Y001", "M", ["0101000"], "level", "%", 1, None),
    ("m2_yoy", "M2(광의통화) 증가율", "통화·금리", "161Y006", "M", ["BBHA00"], "yoy", "% (전년比)", 1, None),
    ("cpi_yoy", "소비자물가 CPI", "물가", "901Y009", "M", ["0"], "yoy", "% (전년比)", 1, None),
    ("core_cpi1", "근원 CPI (농산물·석유류 제외)", "물가", "901Y010", "M", ["QB"], "yoy", "% (전년比)", 1, None),
    ("core_cpi2", "근원 CPI (식료품·에너지 제외)", "물가", "901Y010", "M", ["DB"], "yoy", "% (전년比)", 1, None),
    ("ppi_yoy", "생산자물가 PPI", "물가", "404Y014", "M", ["*AA"], "yoy", "% (전년比)", 1, None),
    ("gdp_qoq", "실질 GDP 성장률 (전기비)", "성장·산업", "200Y102", "Q", ["10111"], "level", "% (전기비, 계절조정)", 1, None),
    ("gdp_yoy", "실질 GDP 성장률 (전년동기비)", "성장·산업", "200Y102", "Q", ["10211"], "level", "% (전년동기비)", 1, None),
    ("ip_yoy", "전산업생산", "성장·산업", "901Y033", "M", ["A00"], "yoy", "% (전년比)", 1, "원계열"),
    ("lei", "선행지수 순환변동치", "성장·산업", "901Y067", "M", ["I16E"], "level", "지수 (100 위=확장)", 1, None),
    ("cei", "동행지수 순환변동치", "성장·산업", "901Y067", "M", ["I16D"], "level", "지수 (100 위=확장)", 1, None),
    ("export_yoy", "수출 (통관)", "대외", "901Y118", "M", ["T002"], "yoy", "% (전년比)", 1, None),
    ("import_yoy", "수입 (통관)", "대외", "901Y118", "M", ["T004"], "yoy", "% (전년比)", 1, None),
    ("ca", "경상수지", "대외", "301Y013", "M", ["000000"], "level", "억 달러", 0.01, None),
    ("ccsi", "소비자심리지수", "심리", "511Y002", "M", ["FME", "99988"], "level", "지수 (100 위=낙관)", 1, None),
    ("bsi_all", "기업심리 업황실적BSI (전산업)", "심리", "512Y013", "M", ["99988", "AA"], "level", "지수 (100 위=긍정)", 1, None),
    ("bsi_mfg", "기업심리 업황실적BSI (제조업)", "심리", "512Y013", "M", ["C0000", "AA"], "level", "지수 (100 위=긍정)", 1, None),
    ("esi", "경제심리지수", "심리", "513Y001", "M", ["E1000"], "level", "지수", 1, None),
    ("unemp", "실업률", "고용", "901Y027", "M", ["I61BC"], "level", "%", 1, "원계열"),
    ("emp_rate", "고용률", "고용", "901Y027", "M", ["I61E"], "level", "%", 1, "원계열"),
    ("emp_diff", "취업자수 증감", "고용", "901Y027", "M", ["I61BA"], "yoy_diff", "천명 (전년比)", 1, "원계열"),
]
GROUP_ORDER = ["통화·금리", "물가", "성장·산업", "대외", "심리", "고용"]
YEARS = 11              # yoy 계산에 1년이 더 필요하니 넉넉히
KEEP_M, KEEP_Q = 120, 44


def _period(cycle):
    t = now_kst().date()
    y0 = t.year - YEARS
    if cycle == "Q":
        return f"{y0}Q1", f"{t.year}Q4"
    return f"{y0}01", f"{t.year}12"


def fetch_series(stat, cycle, items, pref2):
    """[(TIME, 값)] 오래된 것부터. 실패하면 None."""
    start, end = _period(cycle)
    path = "/".join(urllib.parse.quote(i, safe="*") for i in items)
    url = f"{BASE}/StatisticSearch/{env('ECOS_API_KEY')}/json/kr/1/1000/{stat}/{cycle}/{start}/{end}/{path}"
    try:
        r = get(url, timeout=40)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        print(f"    요청 실패: {str(e)[:100]}", file=sys.stderr)
        return None
    rows = (d.get("StatisticSearch") or {}).get("row") or []
    if not rows:
        print(f"    자료 없음: {str(d)[:140]}", file=sys.stderr)
        return None
    groups = {}
    for x in rows:
        groups.setdefault((x.get("ITEM_CODE2") or "", x.get("ITEM_CODE3") or ""), []).append(x)
    if len(groups) > 1:                       # 항목 축이 더 있으면(원계열/계절조정 등) 선호 낱말로 고른다
        def nm(k):
            g = groups[k][0]
            return f"{g.get('ITEM_NAME2', '')}{g.get('ITEM_NAME3', '')}"
        pick = next((k for k in sorted(groups) if pref2 and pref2 in nm(k)), sorted(groups)[0])
        print(f"    항목 축 {len(groups)}개 -> 선택: {nm(pick)} (후보: {[nm(k) for k in sorted(groups)][:4]})")
        rows = groups[pick]
    out = []
    for x in rows:
        try:
            out.append((str(x["TIME"]), float(str(x["DATA_VALUE"]).replace(",", ""))))
        except (KeyError, ValueError):
            continue
    out.sort()
    return out or None


def iso(t):
    """ECOS 시간 -> 차트용 날짜. 월 202608 -> 2026-08-01, 분기 2026Q2 -> 2026-04-01."""
    if "Q" in t:
        return f"{t[:4]}-{(int(t[5]) - 1) * 3 + 1:02d}-01"
    return f"{t[:4]}-{t[4:6]}-01"


def label(t):
    return f"{t[:4]}년 {int(t[5])}분기" if "Q" in t else f"{t[:4]}-{t[4:6]}"


def transform(series, kind, scale):
    d = dict(series)
    out = []
    for t, v in series:
        if kind == "level":
            out.append((t, v * scale))
            continue
        pk = f"{int(t[:4]) - 1}{t[4:]}"
        p = d.get(pk)
        if p is None:
            continue
        if kind == "yoy" and p:
            out.append((t, (v / p - 1) * 100))
        elif kind == "yoy_diff":
            out.append((t, (v - p) * scale))
    return out


def entry(spec):
    key, lab, group, stat, cycle, items, kind, unit, scale, pref2 = spec
    print(f"  {lab} ({stat} {'/'.join(items)} {cycle})")
    base = {"key": key, "label": lab, "group": group, "unit": unit, "freq": cycle,
            "value": None, "prev": None, "date": None, "history": []}
    s = fetch_series(stat, cycle, items, pref2)
    if not s:
        return base
    rows = transform(s, kind, scale)
    if len(rows) < 2:
        return base
    keep = KEEP_Q if cycle == "Q" else KEEP_M
    (t0, v0), (t1, v1) = rows[-1], rows[-2]
    base.update({"value": round(v0, 2), "prev": round(v1, 2), "date": label(t0), "prev_date": label(t1),
                 "history": [[iso(t), round(v, 2)] for t, v in rows[-keep:]]})
    print(f"    -> {base['value']} ({base['date']}) 직전 {base['prev']} · 이력 {len(base['history'])}건")
    return base


def build_prompt(items):
    by = {e["key"]: e for e in items}

    def line(k):
        e = by.get(k)
        return f"{e['label']}: {e['value']} {e['unit']} ({e['date']})" if e and e["value"] is not None else None
    keys = ["base_rate", "cpi_yoy", "core_cpi1", "unemp", "gdp_qoq", "gdp_yoy", "export_yoy", "ca", "ccsi", "bsi_all", "lei", "ip_yoy"]
    block = "\n".join(x for x in (line(k) for k in keys) if x)
    return (
        "아래는 한국 주요 경제지표의 최신 값이다. 금융 초보자가 이해할 해설을 써라.\n\n"
        f"{block}\n\n"
        "다음 3개 항목을 순서대로, 다른 말 없이 이 형식으로만 출력해라:\n"
        "[지금 상황]\n물가·성장·수출·고용·심리가 지금 어떤 상태인지 쉬운 말로 2~3문장.\n"
        "[왜 중요한지]\n이게 한국은행 기준금리 결정이나 코스피에 왜 중요한지 1~2문장.\n"
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
    if not env("ECOS_API_KEY"):
        print("ECOS_API_KEY 가 없어 건너뜁니다.")
        return
    items = []
    for spec in INDICATORS:
        items.append(entry(spec))
        time.sleep(0.2)
    ok = [e for e in items if e["value"] is not None]
    if not ok:
        print("받은 지표가 없습니다 -- 파일을 만들지 않습니다.", file=sys.stderr)
        return

    notes = {"situation": "", "why": "", "history": ""}
    key = env("GEMINI_API_KEY")
    if key:
        text = ask_gemini(build_prompt(items), key)
        if text:
            notes = parse_sections(text)
    else:
        print("GEMINI_API_KEY 없음 -- 해설은 생략합니다.", file=sys.stderr)

    write_json(DATA_DIR / "kr_macro.json", {
        "updated_at": now_kst().isoformat(), "group_order": GROUP_ORDER, "indicators": items, "notes": notes})
    print(f"저장 완료 ({len(ok)}/{len(items)}종)")
    missing = [e["label"] for e in items if e["value"] is None]
    if missing:
        print("  못 받은 지표: " + ", ".join(missing))


if __name__ == "__main__":
    main()
