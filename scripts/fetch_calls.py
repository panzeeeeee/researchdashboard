"""오늘 뽑힌 종목의 컨퍼런스 콜 흐름을 정리한다.

Equibles 무료 API(하루 100회)에서 분기별 브리프와 톤·주제를 받아,
3년치(최대 12분기)를 시간순으로 놓고 '말이 어떻게 바뀌었는지'를 본다.

저작권 처리
  받아온 원문과 브리프 문장은 저장하지 않는다.
  그것을 근거로 한국어 요약을 새로 써서 그것만 남기고, 원문은 링크로 연결한다.

호출 절약
  이미 요약해 둔 분기는 다시 만들지 않는다. 새 분기가 생겼을 때만 갱신한다.
"""

import sys
import time

import requests

from common import DATA_DIR, ask_gemini, env, now_kst, read_json, write_json

BASE = "https://api.equibles.com/v1"
MAX_TICKERS = 8        # 하루 호출 한도(100)를 넉넉히 남긴다
MAX_QUARTERS = 12      # 3년치
TIMEOUT = 25


def api(path, key):
    try:
        r = requests.get(f"{BASE}{path}", timeout=TIMEOUT,
                         headers={"Authorization": f"Bearer {key}"})
        if r.status_code == 404:
            return None
        if r.status_code == 429:
            print("  하루 호출 한도에 걸렸습니다.", file=sys.stderr)
            return "LIMIT"
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  {path} 실패: {e}", file=sys.stderr)
        return None


def walk(obj):
    """중첩된 응답에서 dict 들을 모두 훑는다. 응답 형태가 바뀌어도 버티게."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def pick(d, *names, default=None):
    for n in names:
        for k in d:
            if k.lower().replace("_", "") == n.lower().replace("_", ""):
                v = d[k]
                if v not in (None, "", []):
                    return v
    return default


def quarters_from(payload):
    """응답에서 분기 단위 항목만 골라낸다."""
    out = {}
    for d in walk(payload):
        fy = pick(d, "fiscalYear", "fiscal_year", "year")
        fq = pick(d, "fiscalQuarter", "fiscal_quarter", "quarter")
        if fy is None or fq is None:
            continue
        try:
            fy, fq = int(fy), int(fq)
        except (TypeError, ValueError):
            continue
        if not (1 <= fq <= 4) or not (1990 < fy < 2100):
            continue
        key = (fy, fq)
        row = out.setdefault(key, {"fy": fy, "fq": fq})
        for field, names in (
            ("date", ("date", "callDate", "eventDate", "heldAt")),
            ("tone", ("tone", "toneScore", "sentiment", "sentimentScore")),
            ("url", ("url", "link", "webUrl", "permalink")),
        ):
            if field not in row:
                v = pick(d, *names)
                if v is not None:
                    row[field] = v
        themes = pick(d, "themes", "topics", "keyThemes")
        if themes and "themes" not in row:
            names = []
            for t in themes if isinstance(themes, list) else [themes]:
                if isinstance(t, str):
                    names.append(t)
                elif isinstance(t, dict):
                    n = pick(t, "name", "theme", "label", "topic")
                    if n:
                        names.append(str(n))
            if names:
                row["themes"] = names[:5]

    rows = sorted(out.values(), key=lambda r: (r["fy"], r["fq"]), reverse=True)
    return rows[:MAX_QUARTERS]


def evidence(payload, limit=9000):
    """요약 근거로 넘길 짧은 텍스트 조각들. 저장하지 않고 즉시 버린다."""
    bits = []
    for d in walk(payload):
        for k, v in d.items():
            if not isinstance(v, str) or len(v) < 40:
                continue
            if any(w in k.lower() for w in
                   ("brief", "summary", "highlight", "insight", "takeaway")):
                bits.append(v.strip())
    text = "\n".join(bits)
    return text[:limit]


def summarize(ticker, name, rows, source_text, api_key):
    """분기별 쟁점과 3년 흐름을 함께 뽑는다.

    Q&A 원문은 저작물이라 옮길 수 없다. 대신 '애널리스트가 무엇을 캐물었는가'를
    쟁점으로 정리한다. 컨콜에서 정보량이 가장 높은 부분이기도 하다.
    """
    span = ", ".join(f"FY{r['fy']}Q{r['fq']}" for r in rows[:8])
    prompt = (
        f"{name}({ticker})의 실적발표 컨퍼런스 콜 자료다.\n"
        f"다룬 분기: {span}\n\n"
        f"--- 참고 자료 ---\n{source_text}\n\n"
        "아래 JSON 형식으로만 답해라. 설명이나 코드블록 표시 없이 JSON만.\n"
        "{\n"
        '  "arc": ["3년 흐름에서 달라진 점을 3~4줄", "..."],\n'
        '  "quarters": [\n'
        '    {"q": "FY2026Q3",\n'
        '     "mgmt": ["경영진이 강조한 점 2~3개", "..."],\n'
        '     "qna": ["애널리스트가 캐물은 쟁점 2~3개", "..."],\n'
        '     "shift": "직전 분기 대비 달라진 표현이나 태도 (없으면 빈 문자열)"}\n'
        "  ]\n"
        "}\n\n"
        "규칙: 한국어로 쓰고 각 항목 50자 이내. "
        "원문 문장을 그대로 옮기지 말고 네 말로 다시 써라. "
        "자료에 없으면 빈 배열로 두고 지어내지 마라. "
        "매수·매도 의견은 쓰지 마라. 최근 분기부터 최대 8개까지."
    )
    text = ask_gemini(prompt, api_key, timeout=90)
    if not text:
        return {}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    try:
        import json
        data = json.loads(cleaned.strip())
    except Exception as e:
        print(f"  {ticker} 요약 해석 실패: {e}", file=sys.stderr)
        return {}
    return {
        "arc": [str(x) for x in (data.get("arc") or [])][:4],
        "quarters": [
            {
                "q": str(q.get("q") or ""),
                "mgmt": [str(x) for x in (q.get("mgmt") or [])][:3],
                "qna": [str(x) for x in (q.get("qna") or [])][:3],
                "shift": str(q.get("shift") or ""),
            }
            for q in (data.get("quarters") or [])[:8]
            if isinstance(q, dict)
        ],
    }


def main():
    key = env("EQUIBLES_API_KEY")
    if not key:
        print("EQUIBLES_API_KEY 가 없습니다. 건너뜁니다.", file=sys.stderr)
        return

    picks = (read_json(DATA_DIR / "picks.json") or {}).get("picks", [])
    targets = [p for p in picks if p.get("market") == "us"][:MAX_TICKERS]
    if not targets:
        print("대상 종목이 없습니다.")
        return

    prev = {s["ticker"]: s for s in
            (read_json(DATA_DIR / "calls.json") or {}).get("stocks", [])}
    gemini = env("GEMINI_API_KEY")
    out = []

    for p in targets:
        t = p["code"]
        briefs = api(f"/stocks/{t}/earnings-briefs", key)
        if briefs == "LIMIT":
            break
        insights = api(f"/stocks/{t}/call-insights", key)
        if insights == "LIMIT":
            insights = None

        rows = quarters_from(briefs) or quarters_from(insights)
        if not rows:
            print(f"  {t}: 컨콜 자료 없음")
            continue

        latest = f"{rows[0]['fy']}Q{rows[0]['fq']}"
        old = prev.get(t)
        if old and old.get("latest") == latest and old.get("summary"):
            # 새 분기가 없으면 예전 요약을 그대로 쓴다 (호출 절약)
            out.append({**old, "quarters": rows})
            print(f"  {t}: {latest} (요약 재사용)")
            continue

        source = "\n".join(x for x in (evidence(briefs), evidence(insights)) if x)
        summary = summarize(t, p.get("name") or t, rows, source, gemini) \
            if source else {}
        out.append({
            "ticker": t,
            "name": p.get("name") or t,
            "latest": latest,
            "quarters": rows,
            "summary": summary,
            "source_url": f"https://equibles.com/stocks/{t.lower()}/earnings-calls",
        })
        print(f"  {t}: {len(rows)}개 분기, {latest}")
        time.sleep(0.5)

    write_json(DATA_DIR / "calls.json", {
        "updated_at": now_kst().isoformat(),
        "stocks": out,
    })
    print(f"저장 완료 ({len(out)}종목)")


if __name__ == "__main__":
    main()
