"""주요 일정 캘린더 -- FOMC 회의 일정 + 포트폴리오 종목 실적 발표일.

FOMC 날짜는 연준이 몇 달 전에 미리 공식 발표하는 고정 일정이라 하드코딩한다
(federalreserve.gov 기준, 2026-09-22 확인). 연말에 다음 해 일정 나오면
갱신해야 한다.

실적 발표일은 종목마다 yfinance 에서 받는다 -- 버전마다 .calendar 반환
형태가 달라서 방어적으로 처리한다.

CPI·고용지표 같은 매크로 지표 캘린더는 이번엔 뺐다 -- 발표기관(BLS 등)별로
정확한 날짜를 다 확인해야 해서 범위 밖으로 뒀다. 필요하면 나중에 따로.
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


def upcoming_fomc(today):
    out = []
    for start, end in FOMC_2026:
        if end >= today:
            out.append({
                "start": start, "end": end,
                "label": f"FOMC 회의 ({start[5:7]}/{start[8:10]}~{end[8:10]})",
            })
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
    fomc = upcoming_fomc(today)
    earnings = earnings_dates()

    write_json(DATA_DIR / "calendar.json", {
        "updated_at": now_kst().isoformat(),
        "fomc": fomc,
        "earnings": earnings,
    })
    print(f"저장 완료 (FOMC {len(fomc)}건, 실적 {len(earnings)}건)")


if __name__ == "__main__":
    main()
