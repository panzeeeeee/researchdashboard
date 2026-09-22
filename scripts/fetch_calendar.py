"""주요 일정 캘린더 -- 두 갈래로 나눈다.

  1. 거시경제 지표 및 회의 일정 (macro) -- FOMC 회의, CPI, 고용지표(비농업
     고용지수). 전부 연준(federalreserve.gov)·노동통계청(bls.gov)이 미리
     공식 발표하는 고정 일정이라 하드코딩한다 (2026-09-22 확인 기준).
     연말에 다음 해 일정이 나오면 갱신해야 한다.
  2. 포트폴리오 기업 일정 (earnings) -- 보유 종목별 다음 실적 발표일.
     종목마다 yfinance 에서 받는다 -- 버전마다 .calendar 반환 형태가 달라서
     방어적으로 처리한다.
"""

import sys
import time

import yfinance as yf

from common import DATA_DIR, load_config, now_kst, write_json

# 연준 공식 발표 2026년 FOMC 일정 (federalreserve.gov, 2026-09-22 확인)
FOMC_2026 = [
    ("2026-01-27", "2026-01-28"),
    ("2026-03-17", "2026-03-18"),
    ("2026-04-28", "2026-04-29"),
    ("2026-06-16", "2026-06-17"),
    ("2026-07-28", "2026-07-29"),
    ("2026-09-15", "2026-09-16"),
    ("2026-10-27", "2026-10-28"),
    ("2026-12-08", "2026-12-09"),
]

# 노동통계청(BLS) 공식 발표 일정 (bls.gov/schedule, 2026-09-22 확인)
# 매달 비농업고용지수(Employment Situation)가 먼저, CPI가 그 뒤에 나온다.
CPI_2026 = ["2026-10-14", "2026-11-10", "2026-12-10"]           # 각각 9,10,11월분
EMPLOYMENT_2026 = ["2026-10-02", "2026-11-06", "2026-12-04"]    # 각각 9,10,11월분


def upcoming_macro(today):
    out = []
    for start, end in FOMC_2026:
        if end >= today:
            out.append({"date": start, "label": f"FOMC 회의 ({start[5:7]}/{start[8:10]}~{end[8:10]})"})
    for d in EMPLOYMENT_2026:
        if d >= today:
            out.append({"date": d, "label": "고용지표 (비농업고용지수)"})
    for d in CPI_2026:
        if d >= today:
            out.append({"date": d, "label": "소비자물가지수(CPI)"})
    out.sort(key=lambda r: r["date"])
    return out


def earnings_dates():
    cfg = load_config("portfolio.yaml")
    tickers = [h["ticker"] for h in (cfg.get("holdings") or []) if h.get("ticker")]

    out = []
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            cal = tk.calendar
            d = None
            if isinstance(cal, dict):
                d = cal.get("Earnings Date")
            elif cal is not None and hasattr(cal, "empty") and not cal.empty:
                if "Earnings Date" in getattr(cal, "index", []):
                    d = cal.loc["Earnings Date"]
                elif "Earnings Date" in getattr(cal, "columns", []):
                    d = cal["Earnings Date"].iloc[0]
            if isinstance(d, (list, tuple)) and d:
                d = d[0]
            if d:
                out.append({"ticker": t, "date": str(d)[:10]})
        except Exception as e:
            print(f"  {t} 실적일 조회 실패: {e}", file=sys.stderr)
        time.sleep(0.15)

    out.sort(key=lambda r: r["date"])
    return out


def main():
    today = now_kst().date().isoformat()
    macro = upcoming_macro(today)
    earnings = earnings_dates()

    write_json(DATA_DIR / "calendar.json", {
        "updated_at": now_kst().isoformat(),
        "macro": macro,
        "earnings": earnings,
    })
    print(f"저장 완료 (거시일정 {len(macro)}건, 실적 {len(earnings)}건)")


if __name__ == "__main__":
    main()
