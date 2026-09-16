"""신고가 스크리너가 받아둔 종목 목록을 딥밸류가 쓸 수 있는 형태로 넘긴다.

두 스크리너 모두 iShares·위키피디아에서 지수 구성종목을 가져오는데,
신고가 쪽 파서가 형식 변화에 더 잘 견딘다. 실제로 2026-09-16 실행에서
딥밸류만 러셀2000 목록을 못 읽고 멈췄다.

그래서 이미 성공한 목록을 재활용한다. 남의 사이트를 두 번 긁지 않아도 되고,
한쪽이 깨져도 다른 쪽이 살아 있으면 전체가 돈다.

입력  screener/universe_cache/{R2K,SPX,NDX}_YYYYMMDD.csv   (열: 티커, 종목명)
출력  screener/universe_cache/dv_{r2k,spx,ndx}.csv          (첫 열이 티커)
"""

import csv
import re
import sys
from pathlib import Path

from common import ROOT

UNIV_DIR = ROOT / "screener" / "universe_cache"
DATE_RE = re.compile(r"_(\d{8})\.csv$")


def newest_for(code):
    """해당 지수의 가장 최근 목록 파일."""
    files = [p for p in UNIV_DIR.glob(f"{code}_*.csv") if DATE_RE.search(p.name)]
    if not files:
        return None
    return max(files, key=lambda p: DATE_RE.search(p.name).group(1))


def tickers_from(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2:
        return []

    header = [c.strip() for c in rows[0]]
    idx = 0
    for want in ("티커", "Ticker", "ticker", "symbol", "Symbol"):
        if want in header:
            idx = header.index(want)
            break

    # 현금·증거금 자리표시자는 종목이 아니다
    SKIP = {"XTSLA", "MARGIN-CASH", "USD", "CASH", "-"}

    out, seen = [], set()
    for r in rows[1:]:
        if len(r) <= idx:
            continue
        t = (r[idx] or "").strip().upper()
        if not t or t in seen or t in SKIP:
            continue
        if not re.fullmatch(r"[A-Z0-9.\-]{1,10}", t):
            continue
        seen.add(t)
        out.append(t)
    return out


def main():
    if not UNIV_DIR.exists():
        print("universe_cache 폴더가 없습니다. 신고가 스크리너를 먼저 돌리세요.",
              file=sys.stderr)
        return

    made = 0
    for code in ("R2K", "SPX", "NDX"):
        src = newest_for(code)
        if not src:
            print(f"  {code}: 목록 파일 없음 — 건너뜁니다", file=sys.stderr)
            continue

        tickers = tickers_from(src)
        if not tickers:
            print(f"  {code}: 티커를 읽지 못했습니다 ({src.name})", file=sys.stderr)
            continue

        dst = UNIV_DIR / f"dv_{code.lower()}.csv"
        with open(dst, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ticker"])
            w.writerows([t] for t in tickers)
        made += 1
        print(f"  {code}: {len(tickers)}종목 -> {dst.name} (원본 {src.name})")

    print(f"완료 ({made}개 지수)")


if __name__ == "__main__":
    main()
