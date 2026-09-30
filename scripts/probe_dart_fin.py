"""DART 재무제표 응답 구조를 눈으로 확인하는 시험 (파일을 만들지 않는다).

한국 딥밸류의 재무 자료를 DART 전체 재무제표(fnlttSinglAcntAll)에서 받기 전에,
실제 응답이 어떤 모양인지 -- 계정과목 이름·코드, 3개월/누적 열, 단위, 연결/개별 --
를 로그로 확인한다.
"""

import io
import sys
import zipfile
import xml.etree.ElementTree as ET

import requests

from common import env

KEY = env("DART_API_KEY")
BASE = "https://opendart.fss.or.kr/api"
UA = {"User-Agent": "Mozilla/5.0 (compatible; research-dashboard/1.0)"}


def corp_codes():
    r = requests.get(f"{BASE}/corpCode.xml", params={"crtfc_key": KEY}, headers=UA, timeout=60)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    out = {}
    for it in root.findall("list"):
        sc = (it.findtext("stock_code") or "").strip()
        if sc:
            out[sc] = (it.findtext("corp_code").strip(), (it.findtext("corp_name") or "").strip())
    return out


def call(path, **params):
    params["crtfc_key"] = KEY
    r = requests.get(f"{BASE}/{path}", params=params, headers=UA, timeout=40)
    r.raise_for_status()
    return r.json()


def show_fin(corp, year, reprt, fs, only=None, limit=400):
    d = call("fnlttSinglAcntAll.json", corp_code=corp, bsns_year=year, reprt_code=reprt, fs_div=fs)
    print(f"  [{year} {reprt} {fs}] status={d.get('status')} {d.get('message')} · 행 {len(d.get('list') or [])}")
    rows = d.get("list") or []
    n = 0
    for r in rows:
        if only and r.get("sj_div") not in only:
            continue
        if r.get("sj_div") == "SCE":
            continue
        print(f"    {r.get('sj_div'):3} {str(r.get('account_id'))[:38]:38} | {str(r.get('account_nm'))[:24]:24} | "
              f"{r.get('thstrm_amount')} | add {r.get('thstrm_add_amount')} | 전기 {r.get('frmtrm_amount')} | 전전기 {r.get('bfefrmtrm_amount')}")
        n += 1
        if n >= limit:
            print("    ...")
            break
    return rows


def main():
    if not KEY:
        print("DART_API_KEY 없음")
        return
    cc = corp_codes()
    print(f"상장사 고유번호 {len(cc)}개")

    # 1) 삼성전자: 연간(연결) 전체, 반기(연결)의 손익 열
    samsung = cc["005930"][0]
    print(f"\n=== 삼성전자 {samsung} 사업보고서 2025 (연결) ===")
    show_fin(samsung, "2025", "11011", "CFS", limit=260)
    print(f"\n=== 삼성전자 반기보고서 2026 (연결) -- 손익·현금흐름만 ===")
    show_fin(samsung, "2026", "11012", "CFS", only=("IS", "CIS", "CF"), limit=80)
    print(f"\n=== 삼성전자 1분기 2026 (연결) -- 손익만 ===")
    show_fin(samsung, "2026", "11013", "CFS", only=("IS", "CIS"), limit=30)

    # 2) 주식 총수
    print("\n=== 주식총수 현황 stockTotqySttus (삼성전자 2025 사업보고서) ===")
    d = call("stockTotqySttus.json", corp_code=samsung, bsns_year="2025", reprt_code="11011")
    print(f"  status={d.get('status')} {d.get('message')}")
    for r in (d.get("list") or [])[:8]:
        print("   ", {k: r.get(k) for k in ("se", "isu_stock_totqy", "now_to_isu_stock_totqy", "istc_totqy", "tesstk_co", "distb_stock_co")})

    # 3) 중소형주: 계정과목 이름·코드 변형 살펴보기 (이름 목록만)
    print("\n=== 중소형주 3곳: 계정 이름 변형 (BS·IS·CF 계정 이름과 코드) ===")
    for sc in ("247540", "086520", "035900"):      # 에코프로비엠, 에코프로, JYP Ent.
        if sc not in cc:
            continue
        corp, name = cc[sc]
        print(f"\n--- {name} {sc} {corp} 사업보고서 2025 ---")
        rows = None
        for fs in ("CFS", "OFS"):
            d = call("fnlttSinglAcntAll.json", corp_code=corp, bsns_year="2025", reprt_code="11011", fs_div=fs)
            if d.get("status") == "000":
                rows = d.get("list") or []
                print(f"  {fs} 행 {len(rows)}")
                break
        for r in rows or []:
            if r.get("sj_div") in ("BS", "IS", "CIS", "CF"):
                print(f"    {r['sj_div']:3} {str(r.get('account_id'))[:36]:36} | {str(r.get('account_nm'))[:26]}")
    print("\n시험 끝")


if __name__ == "__main__":
    main()
