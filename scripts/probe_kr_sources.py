"""한국 스크리너 준비용 -- 종목 목록과 주가를 GitHub 서버에서 받을 수 있는지 시험한다.

손으로만 돌리는 시험용(probe_kr.yml). 결과는 로그에만 찍고 아무 파일도 만들지 않는다.
확인하는 것
  1. 네이버 금융 시가총액 목록 (종목코드·시가총액·시장구분을 한 번에 줄 수 있나)
  2. KRX KIND 상장법인 목록 (업종까지 있나) -- 해외 서버에서 막히는지
  3. DART 고유번호 목록 (상장사 종목코드 전체)
  4. 야후에서 .KS/.KQ 6년치 일봉을 몇 종목씩 얼마나 빨리 받나
"""

import io
import re
import sys
import time
import zipfile

import pandas as pd
import requests

from common import env

UA = {"User-Agent": "Mozilla/5.0 (compatible; research-dashboard/1.0)"}


def step(name):
    print(f"\n=== {name} ===")


def probe_naver():
    step("1. 네이버 금융 시가총액 목록")
    total = {}
    for sosok, label in ((0, "코스피"), (1, "코스닥")):
        url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page=1"
        try:
            t0 = time.time()
            r = requests.get(url, headers=UA, timeout=20)
            print(f"  {label} 1쪽: HTTP {r.status_code} · {len(r.content):,}바이트 · {time.time()-t0:.1f}초")
            r.raise_for_status()
            html = r.content.decode("euc-kr", errors="replace")
            codes = re.findall(r"code=(\d{6})", html)
            last = re.search(r'pgRR.*?page=(\d+)', html, re.S)
            tables = pd.read_html(io.StringIO(html))
            df = max(tables, key=len)
            print(f"    종목코드 {len(set(codes))}개 · 마지막 쪽 {last.group(1) if last else '?'} · 표 열: {list(df.columns)[:8]}")
            total[label] = int(last.group(1)) if last else None
        except Exception as e:
            print(f"  {label} 실패: {str(e)[:150]}")
    return total


def probe_kind():
    step("2. KRX KIND 상장법인 목록")
    url = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
    try:
        t0 = time.time()
        r = requests.get(url, headers=UA, timeout=25)
        print(f"  HTTP {r.status_code} · {len(r.content):,}바이트 · {time.time()-t0:.1f}초")
        r.raise_for_status()
        df = pd.read_html(io.StringIO(r.content.decode("euc-kr", errors="replace")))[0]
        print(f"  {len(df)}행 · 열: {list(df.columns)}")
        if "시장구분" in df.columns:
            print("  시장구분:", df["시장구분"].value_counts().to_dict())
    except Exception as e:
        print(f"  실패: {str(e)[:150]}")


def probe_dart():
    step("3. DART 고유번호 목록")
    key = env("DART_API_KEY")
    if not key:
        print("  DART_API_KEY 없음")
        return []
    try:
        r = requests.get("https://opendart.fss.or.kr/api/corpCode.xml",
                         params={"crtfc_key": key}, headers=UA, timeout=60)
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        xml = z.read(z.namelist()[0]).decode("utf-8", errors="replace")
        codes = [c for c in re.findall(r"<stock_code>\s*(\d{6})\s*</stock_code>", xml)]
        print(f"  전체 압축 {len(r.content):,}바이트 · 상장사 종목코드 {len(set(codes))}개")
        return sorted(set(codes))
    except Exception as e:
        print(f"  실패: {str(e)[:150]}")
        return []


def probe_yahoo(codes):
    step("4. 야후 일봉 (.KS / .KQ)")
    import yfinance as yf
    sample = ["005930.KS", "000660.KS", "035720.KS", "247540.KQ", "086520.KQ", "005930.KQ"]
    try:
        df = yf.download(sample, period="6y", interval="1d", group_by="ticker",
                         progress=False, threads=False, auto_adjust=True)
        for t in sample:
            try:
                s = df[t]["Close"].dropna()
                print(f"  {t}: {len(s)}행 · {s.index[0].date() if len(s) else '-'} ~ {s.index[-1].date() if len(s) else '-'}")
            except Exception:
                print(f"  {t}: 없음")
    except Exception as e:
        print(f"  샘플 실패: {str(e)[:150]}")

    if codes:
        n = 200
        batch = [c + ".KS" for c in codes[:n]]
        t0 = time.time()
        try:
            df = yf.download(batch, period="1y", interval="1d", group_by="ticker",
                             progress=False, threads=False, auto_adjust=True)
            got = sum(1 for t in batch if t in df.columns.get_level_values(0)
                      and not df[t]["Close"].dropna().empty)
            print(f"  {n}종목 1년치 .KS 일괄: {time.time()-t0:.0f}초 · 받은 종목 {got}개")
        except Exception as e:
            print(f"  일괄 실패: {str(e)[:150]}")


def main():
    probe_naver()
    probe_kind()
    codes = probe_dart()
    probe_yahoo(codes)
    print("\n시험 끝")


if __name__ == "__main__":
    main()
