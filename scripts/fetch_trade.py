"""수출입 대시보드 -- 관세청 무역통계(공공데이터포털 API 두 개).

- 품목별 수출입실적(Itemtrade): 테마(HS코드)별 전 세계 월간 수출액·중량
- 품목별 국가별 수출입실적(nitemtrade): 같은 테마를 나라별로
테마와 나라 목록은 config/trade.yaml 에서 고친다.

데이터는 매월 15일경 전월분이 확정·정정되므로 매일 다 받을 필요가 없다.
- 처음(파일 없음): 2023-01부터 전부 (시간이 10~20분 걸릴 수 있음)
- 이후 매일: 전 세계 합계만 가볍게 확인해서 새 달이 생겼거나 마지막 전체
  갱신이 7일 넘었으면 최근 13개월을 다시 받는다. 아니면 그대로 끝.
결과: docs/data/trade.json (패널용), history/trade_monthly.csv (전체 스냅샷)
인증키: GitHub Secrets 의 DATA_GO_KR_KEY (Decoding 키)
"""

import csv
import datetime
import json
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
import yaml

from common import DATA_DIR, env, now_kst

API_WORLD = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
API_CNTY = "https://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList"
START = "2023-01"
REFRESH_DAYS = 7

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "trade.yaml"
OUT = DATA_DIR / "trade.json"
HISTORY = ROOT / "history" / "trade_monthly.csv"


class ApiRefused(Exception):
    pass


# ---------------------------------------------------------------- 기간

def ym_add(ym, n):
    y, m = int(ym[:4]), int(ym[5:7])
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def chunks(start, end):
    out, s = [], start
    while s <= end:
        e = min(ym_add(s, 11), end)
        out.append((s, e))
        s = ym_add(e, 1)
    return out


# ---------------------------------------------------------------- API

def num(x):
    try:
        return float((x or "").replace(",", ""))
    except ValueError:
        return None


def parse(xml_text, hs):
    """응답 XML → {월: [수출액$, 수출중량kg]}. 여러 자리수 코드가 섞여 오면
    이중 합산을 피하려고 요청 코드 그 자체 줄, 없으면 가장 짧은 코드 줄만 더한다."""
    root = ET.fromstring(xml_text)
    code = (root.findtext(".//resultCode") or root.findtext(".//returnReasonCode") or "").strip()
    if code and code not in ("00", "0", "000"):
        msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or ""
        raise ApiRefused(f"{code} {msg}")
    rows = {}
    for it in root.iter("item"):
        ym = (it.findtext("year") or "").strip().replace(".", "-")
        if len(ym) != 7 or not ym[:4].isdigit():
            continue
        c = (it.findtext("hsCd") or "").strip()
        if c and not c.startswith(hs):
            continue
        rows.setdefault(ym, []).append((c, num(it.findtext("expDlr")), num(it.findtext("expWgt"))))
    out = {}
    for ym, lst in rows.items():
        exact = [r for r in lst if r[0] == hs or not r[0]]
        if exact:
            use = exact
        else:
            short = min(len(r[0]) for r in lst)
            use = [r for r in lst if len(r[0]) == short]
        out[ym] = [sum(r[1] or 0 for r in use), sum(r[2] or 0 for r in use)]
    return out


TRANSIENT = ("01 ", "04 ", "05 ", "23 ", "HTTP 5", "접속 실패")


def call_parse(url, params, hs, tries=5):
    """한 번 요청해서 해석까지. 중계 서버의 일시 오류는 쉬었다가 다시 시도한다."""
    for attempt in range(tries):
        try:
            try:
                r = requests.get(url, params=params, timeout=40)
            except Exception as ex:
                raise ApiRefused(f"접속 실패: {ex}")
            if r.status_code != 200:
                raise ApiRefused(f"HTTP {r.status_code} {r.text[:150]}")
            return parse(r.text, hs)
        except ApiRefused as ex:
            if attempt < tries - 1 and str(ex).startswith(TRANSIENT):
                time.sleep(5 * (attempt + 1))
                continue
            raise


def fetch(url, key, hs, start, end, cntyCd=None):
    got = {}
    for s, e in chunks(start, end):
        p = {"serviceKey": key, "strtYymm": s.replace("-", ""),
             "endYymm": e.replace("-", ""), "hsSgn": hs}
        if cntyCd:
            p["cntyCd"] = cntyCd
        got.update(call_parse(url, p, hs))
        time.sleep(0.25)
    return got


# ---------------------------------------------------------------- 계산

def series_map(series):
    return {ym: (usd, kg) for ym, usd, kg in series}


def ssum(m, months):
    vals = [m[x][0] for x in months if x in m]
    return sum(vals) if len(vals) == len(months) else None


def pct(a, b):
    if a is None or not b:
        return None
    return round((a / b - 1) * 100, 1)


def theme_stats(t, country_names):
    w = series_map(t["world"])
    if not w:
        return None
    last = max(w)
    ly = ym_add(last, -12)
    last3 = [ym_add(last, -i) for i in range(3)]
    ly3 = [ym_add(x, -12) for x in last3]
    usd, kg = w[last]
    unit = usd / kg if kg else None
    unit_ly = (w[ly][0] / w[ly][1]) if ly in w and w[ly][1] else None

    # 나라별: 최근 3개월 비중 vs 1년 전 같은 3개월 비중
    tot3, totly3 = ssum(w, last3), ssum(w, ly3)
    tops = []
    for cc, ser in t["by_country"].items():
        m = series_map(ser)
        a, b = ssum(m, last3), ssum(m, ly3)
        if a is None:
            continue
        share = a / tot3 * 100 if tot3 else None
        share_ly = (b / totly3 * 100) if (b is not None and totly3) else None
        tops.append({"code": cc, "name": country_names.get(cc, cc),
                     "usd3m": round(a), "share": round(share, 1) if share is not None else None,
                     "share_chg": round(share - share_ly, 1) if (share is not None and share_ly is not None) else None,
                     "yoy3m": pct(a, b)})
    tops.sort(key=lambda x: -x["usd3m"])
    return {
        "latest": last, "usd": round(usd), "kg": round(kg),
        "yoy": pct(usd, w[ly][0]) if ly in w else None,
        "yoy3m": pct(tot3, totly3),
        "unit": round(unit, 2) if unit else None,
        "unit_yoy": pct(unit, unit_ly),
        "countries": tops,
    }


def make_signals(themes):
    """숫자 규칙으로만 뽑는다(자동 요약 아님). 크게 움직인 것부터."""
    sig = []
    for t in themes:
        s = t.get("stats")
        if not s:
            continue
        if s["yoy3m"] is not None and abs(s["yoy3m"]) >= 20:
            sig.append({"key": t["key"], "kind": "금액", "score": abs(s["yoy3m"]),
                        "text": f"{t['name']} 최근 3개월 수출 전년 대비 {s['yoy3m']:+.1f}%"})
        if s["unit_yoy"] is not None and abs(s["unit_yoy"]) >= 15:
            sig.append({"key": t["key"], "kind": "단가", "score": abs(s["unit_yoy"]) * 0.8,
                        "text": f"{t['name']} {s['latest'][5:]}월 단가($/kg) 전년 대비 {s['unit_yoy']:+.1f}%"})
        for c in s["countries"][:10]:
            if c["share_chg"] is not None and abs(c["share_chg"]) >= 5:
                sig.append({"key": t["key"], "kind": "국가", "score": abs(c["share_chg"]) * 2,
                            "text": f"{t['name']} {c['name']} 비중 {c['share']}% "
                                    f"(1년 전보다 {c['share_chg']:+.1f}%p)"})
    sig.sort(key=lambda x: -x["score"])
    return sig[:8]


# ---------------------------------------------------------------- 실행

def main():
    key = env("DATA_GO_KR_KEY")
    if not key:
        print("DATA_GO_KR_KEY 가 없어 건너뜁니다.")
        return
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    themes_cfg = cfg["themes"]
    countries = cfg.get("countries") or {}

    prev = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    old = {t["key"]: t for t in prev.get("themes", [])}

    today = now_kst()
    end = f"{today.year:04d}-{today.month:02d}"

    # 새로 받을 필요가 있나? 첫 테마 전 세계 합계로 최신 달만 확인
    need_all = set()
    try:
        probe = fetch(API_WORLD, key, themes_cfg[0]["hs"], ym_add(end, -2), end)
    except ApiRefused as ex:
        print(f"API 거부 -- 키·신청 상태·접속을 확인하세요: {ex}", file=sys.stderr)
        sys.exit(1)
    newest = max(probe) if probe else None
    last_full = prev.get("last_full")
    stale = (not last_full or
             (today.date() - datetime.date.fromisoformat(last_full)).days >= REFRESH_DAYS)
    new_month = newest and newest > (prev.get("latest_month") or "")
    missing = False
    for t in themes_cfg:
        if t["key"] not in old or old[t["key"]].get("hs") != str(t["hs"]):
            need_all.add(t["key"])            # 처음 보는 테마·코드가 바뀐 테마는 전부
        elif prev.get("retry_countries", {}).get(t["key"]):
            missing = True                    # 지난번에 실패한 나라가 있으면 다시
    if not (stale or new_month or need_all or missing):
        print(f"새 달 없음(최신 {prev.get('latest_month')}) · 마지막 전체 갱신 {last_full} -- 그대로 둡니다.")
        return

    out_themes, failed, retry = [], [], {}
    for t in themes_cfg:
        hs = str(t["hs"])
        full = t["key"] in need_all
        start = START if full else ym_add(end, -12)
        o = old.get(t["key"], {}) if not full else {}
        world = series_map(o.get("world", []))
        by_c = {cc: series_map(s) for cc, s in (o.get("by_country") or {}).items()}
        try:
            world.update({k: tuple(v) for k, v in fetch(API_WORLD, key, hs, start, end).items()})
        except ApiRefused as ex:
            print(f"  {t['name']} 전체 실패: {ex}", file=sys.stderr)
            failed.append(t["key"])
        for cc in countries:
            c_start = start if (cc in by_c and by_c[cc]) else START
            try:
                got = fetch(API_CNTY, key, hs, c_start, end, cc)
                by_c.setdefault(cc, {}).update({k: tuple(v) for k, v in got.items()})
            except ApiRefused as ex:
                print(f"  {t['name']}·{cc} 실패: {ex}", file=sys.stderr, flush=True)
                retry.setdefault(t["key"], []).append(cc)
        rec = {
            "key": t["key"], "name": t["name"], "group": t.get("group", ""), "hs": hs,
            "world": [[ym, round(v[0]), round(v[1])] for ym, v in sorted(world.items())],
            "by_country": {cc: [[ym, round(v[0]), round(v[1])] for ym, v in sorted(m.items())]
                           for cc, m in by_c.items() if m},
        }
        rec["stats"] = theme_stats(rec, countries)
        out_themes.append(rec)
        print(f"  {t['name']}: {len(rec['world'])}개월 · 나라 {len(rec['by_country'])}곳", flush=True)

    months = sorted({s[0] for t in out_themes for s in t["world"]})
    if not months:
        print("받은 데이터가 없습니다 -- 기존 파일 유지.", file=sys.stderr)
        sys.exit(1)

    data = {
        "updated": today.strftime("%Y-%m-%d %H:%M"),
        "last_full": today.date().isoformat(),
        "latest_month": months[-1], "months": months,
        "countries": countries,
        "groups": list(dict.fromkeys(t.get("group", "") for t in themes_cfg)),
        "themes": out_themes,
        "signals": make_signals(out_themes),
        "failed": failed,
        "retry_countries": retry,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # 이력: 기존 기록에 합친다. 같은 (월, 테마, HS, 나라)는 최신 값으로 바꾸고,
    # 목록에서 빠진 테마나 나라의 옛 기록은 그대로 남긴다.
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    hist = {}
    if HISTORY.exists():
        with HISTORY.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                hist[(r["month"], r["theme"], r["hs"], r["country"])] = (r["export_usd"], r["export_kg"])
    for t in out_themes:
        for ym, usd, kg in t["world"]:
            hist[(ym, t["name"], t["hs"], "전체")] = (usd, kg)
        for cc, ser in t["by_country"].items():
            for ym, usd, kg in ser:
                hist[(ym, t["name"], t["hs"], countries.get(cc, cc))] = (usd, kg)
    with HISTORY.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["month", "theme", "hs", "country", "export_usd", "export_kg"])
        for k in sorted(hist):
            w.writerow([*k, *hist[k]])

    print(f"테마 {len(out_themes)}개 · {months[0]}~{months[-1]} · 신호 {len(data['signals'])}건"
          f" · 실패 {','.join(failed) or '없음'}")


if __name__ == "__main__":
    main()
