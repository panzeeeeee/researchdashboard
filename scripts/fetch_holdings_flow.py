"""포트폴리오 종목 수급 -- 기관 보유·내부자 매매·공매도 잔고 변화.

한국 시장의 "외국인·기관 순매수" 같은 일별 통계를 미국은 공개하지 않는다.
대신 실제로 구할 수 있는 세 가지로 대체한다:
  1. 기관 보유 비중 (현재 스냅샷 -- 전분기 대비 변화는 야후 쪽 데이터 자체가
     대부분 비어 있어서 뺐다)
  2. 내부자 매매 (최근 거래 몇 건)
  3. 공매도 잔고 (이번 달 대 전월)

전부 yfinance 안에 있다. 새 API/키가 필요 없다.

내부자 매매 쪽 DataFrame 컬럼 이름은 버전마다 다를 수 있어 여러 후보 이름을
시도한다 -- 그래도 못 찾으면 그 종목만 빈 목록으로 남기고 넘어간다.

config/portfolio.yaml 의 보유 종목만 대상으로 한다 (관심종목 아님 -- 관심종목은
전부 한국 개념이라 미국 전용인 이 프로젝트엔 안 맞다고 판단해 포트폴리오로 바꿨다).
"""

import sys
import time

import yfinance as yf

from common import DATA_DIR, load_config, now_kst, write_json


def pick(row, *keys):
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return None


def insider_rows(tk, limit=8):
    try:
        df = tk.insider_transactions
    except Exception as e:
        print(f"    내부자 매매 조회 실패: {e}", file=sys.stderr)
        return []
    if df is None or df.empty:
        return []

    out = []
    for _, row in df.head(limit).iterrows():
        d = row.to_dict()
        try:
            out.append({
                "insider": str(pick(d, "Insider", "Filer", "Name") or ""),
                "position": str(pick(d, "Position", "Relation") or ""),
                "text": str(pick(d, "Transaction", "Text", "Type") or ""),
                "shares": pick(d, "Shares"),
                "value": pick(d, "Value"),
                "date": str(pick(d, "Start Date", "Date") or ""),
            })
        except Exception:
            continue
    return out


def get_flow(ticker):
    try:
        tk = yf.Ticker(ticker)
        info = tk.info
    except Exception as e:
        print(f"  {ticker} 정보 실패: {e}", file=sys.stderr)
        return None

    short_now = info.get("sharesShort")
    short_prev = info.get("sharesShortPriorMonth")
    short_chg = (round((short_now / short_prev - 1) * 100, 1)
                 if short_now is not None and short_prev else None)

    inst_pct = info.get("heldPercentInstitutions")

    return {
        "ticker": ticker,
        "inst_pct": round(inst_pct * 100, 1) if inst_pct is not None else None,
        "shares_short": short_now,
        "short_pct_float": (round(info["shortPercentOfFloat"] * 100, 2)
                             if info.get("shortPercentOfFloat") is not None else None),
        "short_chg": short_chg,
        "insider_recent": insider_rows(tk),
    }


def main():
    cfg = load_config("portfolio.yaml")
    tickers = [h["ticker"] for h in (cfg.get("holdings") or []) if h.get("ticker")]
    if not tickers:
        print("portfolio.yaml 에 보유 종목이 없습니다.", file=sys.stderr)
        return

    rows = []
    for t in tickers:
        flow = get_flow(t)
        if flow:
            rows.append(flow)
            print(f"  {t}: 기관 {flow['inst_pct']}% · 공매도 {flow['short_chg']}%")
        time.sleep(0.3)

    write_json(DATA_DIR / "holdings_flow.json", {
        "updated_at": now_kst().isoformat(),
        "stocks": rows,
    })
    print(f"저장 완료 ({len(rows)}종목)")


if __name__ == "__main__":
    main()
