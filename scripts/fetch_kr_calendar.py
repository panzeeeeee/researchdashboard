"""한국 주요 일정 -- 미국판(fetch_calendar.py)처럼 공식으로 미리 정해진 일정만 넣는다(추측한 날짜는 넣지 않는다).

  - 한국은행 통화정책방향 결정회의   : 한국은행 홈페이지 '통화정책방향 결정회의 일정'에서 확인한 날짜(2026-09-30 확인).
                                       연초에 다음 해 일정이 나오면 갱신해야 한다.
  - 옵션 만기일                     : 매월 두 번째 목요일 (3·6·9·12월은 선물·옵션 동시만기 = 네 마녀의 날). 휴장일이면 앞 영업일.
  - 휴장일                          : 남은 2026년 공휴일·연말(개천절 대체공휴일 10/5, 한글날 10/9, 성탄절 12/25, 연말 12/31).
  - 미국 FOMC·CPI·고용지표          : 코스피에 영향이 커서 fetch_calendar.py 가 만든 calendar.json 을 그대로 가져온다.

결과: docs/data/kr_calendar.json
"""

from datetime import date, timedelta

from common import DATA_DIR, now_kst, read_json, write_json

# 한국은행 통화정책방향 결정회의 (출처: bok.or.kr 통화정책방향 결정회의 일정, 2026-09-30 확인)
BOK_MEETINGS = ["2026-10-22", "2026-11-26"]
# 남은 2026년 휴장일 (한국거래소)
HOLIDAYS = {"2026-10-05": "개천절 대체공휴일 휴장", "2026-10-09": "한글날 휴장",
            "2026-12-25": "성탄절 휴장", "2026-12-31": "연말 휴장"}
MONTHS_AHEAD = 6


def second_thursday(y, m):
    d = date(y, m, 1)
    d += timedelta(days=(3 - d.weekday()) % 7)      # 첫 목요일
    return d + timedelta(days=7)


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return d.year + y, m + 1


def main():
    today = now_kst().date()
    events = []
    for s in BOK_MEETINGS:
        if s >= today.isoformat():
            events.append({"date": s, "kind": "한국", "label": "한국은행 금통위 (기준금리 결정)",
                           "note": "통화정책방향 결정회의 · 기준금리 발표"})
    for k in range(MONTHS_AHEAD + 1):
        y, m = add_months(today.replace(day=1), k)
        d = second_thursday(y, m)
        while d.isoformat() in HOLIDAYS or d.weekday() >= 5:     # 휴장일이면 앞 영업일
            d -= timedelta(days=1)
        if d >= today:
            quad = m in (3, 6, 9, 12)
            events.append({"date": d.isoformat(), "kind": "시장",
                           "label": "선물·옵션 동시만기 (네 마녀의 날)" if quad else "옵션 만기일",
                           "note": "만기일 전후로 장 막판 변동성이 커질 수 있음" if quad else "매월 두 번째 목요일"})
    for s, name in HOLIDAYS.items():
        if s >= today.isoformat():
            events.append({"date": s, "kind": "휴장", "label": name, "note": "한국거래소 휴장"})
    cal = read_json(DATA_DIR / "calendar.json") or {}
    for e in cal.get("macro", []):
        if e.get("date", "") >= today.isoformat():
            events.append({"date": e["date"], "kind": "미국", "label": e["label"], "note": "미국 지표·회의 (코스피에 영향)"})
    events.sort(key=lambda e: (e["date"], e["kind"]))
    write_json(DATA_DIR / "kr_calendar.json", {
        "updated_at": now_kst().isoformat(),
        "source": "한국은행 통화정책방향 결정회의 일정(bok.or.kr, 2026-09-30 확인) · 옵션 만기일은 매월 둘째 목요일 규칙으로 계산 · 미국 일정은 연준·노동통계청 공식 발표",
        "events": events})
    print(f"저장 완료 (일정 {len(events)}건: " + ", ".join(f"{k} {sum(1 for e in events if e['kind'] == k)}" for k in ('한국', '시장', '휴장', '미국')) + ")")


if __name__ == "__main__":
    main()
