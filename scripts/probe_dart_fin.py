"""DART 재무제표 시험 2 (한국 딥밸류 준비).

확인하는 것
  1. 종목 고유번호 목록을 파일(screener/dart_corp_codes.json)로 저장 -- 다음부터 218초를 다시 안 쓴다
  2. 분기 보고서 손익 행의 모든 열 이름 (전년 같은 분기 값이 어느 열인지)
  3. 주요계정 API(fnlttSinglAcnt, 가벼운 요청) 응답
  4. 8종목 연간 재무제표를 동시에 요청했을 때 걸리는 시간 (병렬로 빨라지는지)
"""

import io
import json
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from common import ROOT, env

KEY = env("DART_API_KEY")
BASE = "https://opendart.fss.or.kr/api"
UA = {"User-Agent": "Mozilla/5.0 (compatible; research-dashboard/1.0)"}
CORP_FILE = ROOT / "screener" / "dart_corp_codes.json"


def corp_codes():
    if CORP_FILE.exists():
        d = json.loads(CORP_FILE.read_text(encoding="utf-8"))
        print(f"고유번호 파일 사용: {len(d)}개", flush=True)
        return d
    t0 = time.time()
    r = requests.get(f"{BASE}/corpCode.xml", params={"crtfc_key": KEY}, headers=UA, timeout=600)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    out = {}
    for it in root.findall("list"):
        sc = (it.findtext("stock_code") or "").strip()
        if sc:
            out[sc] = [it.findtext("corp_code").strip(), (it.findtext("corp_name") or "").strip()]
    CORP_FILE.parent.mkdir(parents=True, exist_ok=True)
    CORP_FILE.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"고유번호 {len(out)}개 받아 저장 · {time.time() - t0:.0f}초", flush=True)
    return out


def call(path, **params):
    params["crtfc_key"] = KEY
    t0 = time.time()
    try:
        r = requests.get(f"{BASE}/{path}", params=params, headers=UA, timeout=60)
        r.raise_for_status()
        d = r.json()
        print(f"  · {path} {time.time() - t0:.1f}초 · {len(r.content):,}바이트 · status {d.get('status')}", flush=True)
        return d
    except Exception as e:
        print(f"  · {path} 실패 {time.time() - t0:.1f}초: {str(e)[:100]}", flush=True)
        return {"status": "ERR", "list": []}


def main():
    if not KEY:
        print("DART_API_KEY 없음")
        return
    cc = corp_codes()
    samsung = cc["005930"][0]

    # 2) 분기 보고서 손익·재무상태 행의 모든 열
    print("\n=== 분기 보고서(반기 2026) 행 하나의 전체 열 ===", flush=True)
    d = call("fnlttSinglAcntAll.json", corp_code=samsung, bsns_year="2026", reprt_code="11012", fs_div="CFS")
    rows = d.get("list") or []
    for want in ("ifrs-full_Revenue", "ifrs-full_Inventories", "dart_OperatingIncomeLoss"):
        for r in rows:
            if r.get("account_id") == want and r.get("sj_div") in ("IS", "BS", "CIS"):
                print("  ", json.dumps(r, ensure_ascii=False))
                break
    print("\n=== 1분기 2026 매출 행 ===", flush=True)
    d = call("fnlttSinglAcntAll.json", corp_code=samsung, bsns_year="2026", reprt_code="11013", fs_div="CFS")
    for r in d.get("list") or []:
        if r.get("account_id") == "ifrs-full_Revenue":
            print("  ", json.dumps(r, ensure_ascii=False))
            break
    print("\n=== 3분기 2025 매출 행 (누적과 3개월) ===", flush=True)
    d = call("fnlttSinglAcntAll.json", corp_code=samsung, bsns_year="2025", reprt_code="11014", fs_div="CFS")
    for r in d.get("list") or []:
        if r.get("account_id") == "ifrs-full_Revenue":
            print("  ", json.dumps(r, ensure_ascii=False))
            break

    # 3) 주요계정 API
    print("\n=== 주요계정 fnlttSinglAcnt (삼성전자 2022 사업보고서) ===", flush=True)
    d = call("fnlttSinglAcnt.json", corp_code=samsung, bsns_year="2022", reprt_code="11011")
    for r in d.get("list") or []:
        print(f"    {r.get('fs_div')} {r.get('sj_div')} {r.get('account_nm')} | {r.get('thstrm_amount')} | 전기 {r.get('frmtrm_amount')} | 전전기 {r.get('bfefrmtrm_amount')}")

    # 4) 병렬 요청 속도
    codes = ["005930", "000660", "035420", "005380", "051910", "068270", "028300", "293490"]
    print("\n=== 8종목 연간 재무제표 동시 요청 (스레드 8개) ===", flush=True)

    def one(sc):
        t0 = time.time()
        try:
            r = requests.get(f"{BASE}/fnlttSinglAcntAll.json", headers=UA, timeout=90,
                             params={"crtfc_key": KEY, "corp_code": cc[sc][0], "bsns_year": "2025",
                                     "reprt_code": "11011", "fs_div": "CFS"})
            d = r.json()
            ids = {x.get("account_id") for x in (d.get("list") or [])}
            has = [k for k in ("ifrs-full_Revenue", "ifrs-full_GrossProfit", "dart_OperatingIncomeLoss",
                               "ifrs-full_Inventories") if k in ids]
            return sc, time.time() - t0, len(r.content), d.get("status"), len(has)
        except Exception as e:
            return sc, time.time() - t0, 0, f"ERR {str(e)[:50]}", 0

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(one, codes))
    wall = time.time() - t0
    for sc, sec, size, st, n in res:
        print(f"    {sc} {cc[sc][1]:12} {sec:5.1f}초 {size:>8,}바이트 status {st} · 핵심계정 {n}/4")
    print(f"  전체 걸린 시간 {wall:.1f}초 (하나씩 하면 약 {sum(r[1] for r in res):.0f}초)")
    print("\n시험 끝", flush=True)


if __name__ == "__main__":
    main()
