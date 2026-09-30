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


def naver_chart(symbol, count=1500):
    """네이버 차트 일봉 [(날짜, 종가, 거래량)]. 주소 하나에 종목 하나."""
    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={symbol}&timeframe=day&count={count}&requestType=0"
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    rows = []
    for m in re.finditer(r'<item data="([^"]+)"', r.content.decode("euc-kr", errors="replace")):
        p = m.group(1).split("|")
        if len(p) >= 6 and p[4] not in ("", "null"):
            rows.append((p[0], float(p[4]), float(p[5] or 0)))
    return rows


def probe_naver_chart():
    step("5. 네이버 차트 일봉 (fchart.stock.naver.com)")
    for sym, label in (("014950", "삼익제약(야후엔 228봉)"), ("012210", "삼미금속(야후 815봉)"),
                       ("005930", "삼성전자"), ("247540", "에코프로비엠"),
                       ("KOSPI", "코스피 지수"), ("KOSDAQ", "코스닥 지수")):
        try:
            t0 = time.time()
            rows = naver_chart(sym)
            print(f"  {sym} {label}: {len(rows)}봉 · {rows[0][0] if rows else '-'} ~ {rows[-1][0] if rows else '-'}"
                  f" · 끝 종가 {rows[-1][1] if rows else '-'} · {time.time()-t0:.1f}초")
        except Exception as e:
            print(f"  {sym} {label}: 실패 {str(e)[:120]}")

    # 야후와 종가 비교 (수정주가 방식이 같은지)
    try:
        import yfinance as yf
        y = yf.download("005930.KS", period="2y", interval="1d", progress=False, auto_adjust=True)["Close"]
        y = y.squeeze().dropna()
        n = {d: c for d, c, _ in naver_chart("005930", 600)}
        diffs = []
        for ts, val in y.items():
            k = ts.strftime("%Y%m%d")
            if k in n and val:
                diffs.append(abs(n[k] / float(val) - 1))
        if diffs:
            print(f"  삼성전자 야후 대비 종가 차이: 평균 {sum(diffs)/len(diffs)*100:.2f}% · 최대 {max(diffs)*100:.2f}% ({len(diffs)}일)")
    except Exception as e:
        print(f"  야후 비교 실패: {str(e)[:120]}")

    # 속도: 순서대로 40종목
    codes = ["005930", "000660", "035420", "005380", "051910"] * 8
    t0, ok = time.time(), 0
    for c in codes:
        try:
            ok += 1 if naver_chart(c, 60) else 0
        except Exception:
            pass
    print(f"  40번 연속 요청: {time.time()-t0:.1f}초 · 성공 {ok}/40")


def probe_sample():
    """KIND 목록에서 코스피·코스닥을 무작위로 30개씩 뽑아, 네이버 일봉 봉 수 분포를 본다.
    (앞쪽 100개만 쓰면 표본이 치우칠 수 있어서)"""
    step("6. 무작위 표본의 히스토리 길이 (네이버)")
    r = requests.get("https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13", headers=UA, timeout=30)
    df = pd.read_html(io.StringIO(r.content.decode("euc-kr", errors="replace")))[0]
    df["코드"] = df["종목코드"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    print(f"  KIND 앞 5행: {df[['회사명', '시장구분', '코드']].head(5).values.tolist()}")
    for m, label in (("유가", "코스피"), ("코스닥", "코스닥")):
        sub = df[df["시장구분"] == m]
        sample = sub.sample(30, random_state=7)
        lens = []
        for _, row in sample.iterrows():
            try:
                rows = naver_chart(row["코드"], 1500)
                lens.append((len(rows), row["회사명"], rows[0][0] if rows else "-"))
            except Exception:
                lens.append((0, row["회사명"], "-"))
            time.sleep(0.2)
        ns = sorted(n for n, _, _ in lens)
        print(f"  {label} 표본 30개: 봉 수 최소 {ns[0]} · 중앙 {ns[len(ns)//2]} · 최대 {ns[-1]} · 900봉 이상 {sum(1 for n in ns if n >= 900)}/30")
        for n, name, first in sorted(lens)[:5]:
            print(f"    가장 짧은 쪽: {name} {n}봉 첫날 {first}")


def probe_overview():
    """네이버 증권에서 '기업개요' 글을 어디서 얻을 수 있는지."""
    step("기업개요 후보 (네이버 증권)")
    for code, nm in (("114090", "GKL"), ("248170", "샘표식품"), ("052400", "코나아이")):
        for label, url in (("main", f"https://finance.naver.com/item/main.naver?code={code}"),
                           ("wisereport", f"https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={code}")):
            try:
                t0 = time.time()
                r = requests.get(url, headers=UA, timeout=25)
                enc = "euc-kr" if label == "main" else (r.encoding or "utf-8")
                html = r.content.decode(enc, errors="replace")
                print(f"\n  [{nm} {code} {label}] HTTP {r.status_code} · {len(r.content):,}바이트 · {time.time()-t0:.1f}초")
                for key in ("summary_info", "corp_group", "cmp_comment", "기업개요", "Business Summary", "cmp-table-cell"):
                    i = html.find(key)
                    if i >= 0:
                        seg = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[i:i + 1600]))
                        print(f"    '{key}' 발견 위치 {i}: {seg[:420]}")
                        break
                else:
                    print("    개요 키워드 없음")
            except Exception as e:
                print(f"  [{nm} {code} {label}] 실패: {str(e)[:120]}")


def main():
    if env("PROBE_ONLY") == "overview":
        probe_overview()
        print("\n시험 끝")
        return
    if env("PROBE_ONLY") == "ecos2":
        import probe_ecos
        probe_ecos.main2()
        return
    if env("PROBE_ONLY") == "ecos":
        import probe_ecos
        probe_ecos.main()
        return
    if env("PROBE_ONLY") == "dartidx":
        import probe_dart_fin
        probe_dart_fin.main_idx()
        return
    if env("PROBE_ONLY") == "dartfin":
        import probe_dart_fin
        probe_dart_fin.main()
        return
    if env("PROBE_ONLY") == "sample":
        probe_sample()
        print("\n시험 끝")
        return
    if env("PROBE_ONLY") == "chart":
        probe_naver_chart()
        print("\n시험 끝")
        return
    probe_naver()
    probe_kind()
    codes = probe_dart()
    probe_yahoo(codes)
    probe_naver_chart()
    print("\n시험 끝")


if __name__ == "__main__":
    main()
