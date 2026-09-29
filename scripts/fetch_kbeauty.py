"""K-뷰티 수출 레이더 -- 관세청 품목별 국가별 수출입실적(공공데이터포털 API).

화장품(HS 3304: 기초·메이크업·네일 등)의 국가별 월간 수출액·중량을 받는다.
API 조건: 국가코드 필수, 조회기간은 한 번에 12개월 이내, 월 단위,
매월 15일경 전월까지 확정·정정된다.

- 처음(파일 없음): 2023-01부터 전부 받는다.
- 이후: 최근 13개월만 다시 받아 덮어쓴다(정정 반영). 그 이전은 기존 값 유지.
- 결과: docs/data/kbeauty.json (패널용), history/kbeauty_exports.csv (전체 스냅샷)

인증키는 GitHub Secrets 의 DATA_GO_KR_KEY (공공데이터포털 Decoding 키).
API 가 거부하면(키 오류·해외 IP 차단 등) 로그에 이유를 남기고 기존 파일은 그대로 둔다.
"""

import csv
import datetime
import json
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from common import DATA_DIR, env, now_kst

API = "https://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList"
HS = "3304"
START = "2023-01"

# 화장품 주요 수출국 (코드, 이름). 필요하면 여기만 고치면 된다.
COUNTRIES = [
    ("US", "미국"), ("CN", "중국"), ("JP", "일본"), ("HK", "홍콩"), ("VN", "베트남"),
    ("RU", "러시아"), ("TW", "대만"), ("TH", "태국"), ("PL", "폴란드"), ("GB", "영국"),
    ("AE", "아랍에미리트"), ("ID", "인도네시아"), ("MY", "말레이시아"), ("NL", "네덜란드"),
    ("DE", "독일"), ("KZ", "카자흐스탄"), ("SG", "싱가포르"), ("PH", "필리핀"),
    ("AU", "호주"), ("CA", "캐나다"), ("FR", "프랑스"), ("TR", "튀르키예"),
    ("KG", "키르기스스탄"), ("MN", "몽골"), ("IN", "인도"),
]

ROOT = Path(__file__).resolve().parent.parent
OUT = DATA_DIR / "kbeauty.json"
HISTORY = ROOT / "history" / "kbeauty_exports.csv"


class ApiRefused(Exception):
    pass


def ym_add(ym, n):
    y, m = int(ym[:4]), int(ym[5:7])
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def chunks(start, end):
    """[start, end] 를 12개월 이하 구간들로 나눈다."""
    out, s = [], start
    while s <= end:
        e = min(ym_add(s, 11), end)
        out.append((s, e))
        s = ym_add(e, 1)
    return out


def num(x):
    try:
        return float((x or "").replace(",", ""))
    except ValueError:
        return None


def parse(xml_text):
    """응답 XML → {월: (수출액$, 수출중량kg)}. 같은 달에 여러 단위(4·6·10자리)가
    섞여 오면 이중 합산을 피하려고 3304 그 자체 줄을, 없으면 가장 짧은 코드 줄들만 더한다."""
    root = ET.fromstring(xml_text)
    code = (root.findtext(".//resultCode") or root.findtext(".//returnReasonCode") or "").strip()
    if code and code not in ("00", "0", "000"):
        msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or ""
        raise ApiRefused(f"{code} {msg}")
    rows = {}
    for it in root.iter("item"):
        ym = (it.findtext("year") or "").strip().replace(".", "-")
        if len(ym) != 7 or not ym[:4].isdigit():
            continue                                  # '총계' 같은 줄 제외
        hs = (it.findtext("hsCd") or "").strip()
        if not hs.startswith(HS):
            continue
        rows.setdefault(ym, []).append((hs, num(it.findtext("expDlr")), num(it.findtext("expWgt"))))
    out = {}
    for ym, lst in rows.items():
        exact = [r for r in lst if r[0] == HS]
        if exact:
            use = exact
        else:
            shortest = min(len(r[0]) for r in lst)
            use = [r for r in lst if len(r[0]) == shortest]
        usd = sum(r[1] or 0 for r in use)
        kg = sum(r[2] or 0 for r in use)
        out[ym] = (usd, kg)
    return out


def fetch_country(key, cc, start, end):
    got = {}
    for s, e in chunks(start, end):
        params = {"serviceKey": key, "strtYymm": s.replace("-", ""),
                  "endYymm": e.replace("-", ""), "hsSgn": HS, "cntyCd": cc}
        for attempt in range(3):
            try:
                r = requests.get(API, params=params, timeout=40)
                if r.status_code != 200:
                    raise ApiRefused(f"HTTP {r.status_code} {r.text[:150]}")
                got.update(parse(r.text))
                break
            except ApiRefused:
                raise
            except Exception as ex:
                if attempt == 2:
                    raise ApiRefused(f"접속 실패: {ex}")
                time.sleep(3)
        time.sleep(0.3)
    return got


def main():
    key = env("DATA_GO_KR_KEY")
    if not key:
        print("DATA_GO_KR_KEY 가 없어 건너뜁니다.")
        return

    old = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text(encoding="utf-8"))
            for c in prev.get("countries", []):
                old[c["code"]] = {ym: (usd, kg) for ym, usd, kg in c["series"]}
        except Exception:
            old = {}

    today = now_kst()
    end = f"{today.year:04d}-{today.month:02d}"
    start = ym_add(end, -12) if old else START

    countries, failed = [], []
    for cc, name in COUNTRIES:
        data = dict(old.get(cc, {}))
        try:
            data.update(fetch_country(key, cc, start, end))
        except ApiRefused as ex:
            print(f"  {name}({cc}) 실패: {ex}", file=sys.stderr)
            failed.append(cc)
            if len(failed) >= 3 and not any(c["series"] for c in countries):
                print("처음 몇 나라가 연달아 거부됨 -- 키 또는 접속 문제로 보고 중단합니다. 기존 파일 유지.",
                      file=sys.stderr)
                sys.exit(1)
        series = [[ym, round(v[0]), round(v[1])] for ym, v in sorted(data.items())]
        countries.append({"code": cc, "name": name, "series": series})

    months = sorted({s[0] for c in countries for s in c["series"]})
    if not months:
        print("받은 데이터가 없습니다 -- 기존 파일 유지.", file=sys.stderr)
        sys.exit(1)

    out = {"updated": today.strftime("%Y-%m-%d %H:%M"), "hs": HS,
           "hs_name": "미용·메이크업·기초화장품(HS 3304)",
           "latest_month": months[-1], "months": months, "countries": countries,
           "failed": failed}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["month", "country", "hs", "export_usd", "export_kg"])
        for c in countries:
            for ym, usd, kg in c["series"]:
                w.writerow([ym, c["name"], HS, usd, kg])

    print(f"국가 {len(countries) - len(failed)}/{len(countries)}개 · {months[0]}~{months[-1]} "
          f"· 실패 {','.join(failed) or '없음'}")


if __name__ == "__main__":
    main()
