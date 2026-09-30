"""센텔리안24 아마존 트래커(구글 시트) → 대시보드 패널 데이터.

시트는 '웹에 게시'된 주소로 읽는다(로그인·키 불필요).
주소는 config/amazon.yaml 의 sheet_url 한 줄에 둔다. 시트를 새로 만들면
그 파일을 '웹에 게시'하고 주소만 바꾸면 된다(코드는 안 고쳐도 됨).
게시 주소는 웹 페이지(pubhtml)·CSV 어느 형식이든 받아서 CSV 로 바꿔 읽는다.
시트 구조: 블록마다 제목 줄(A=블록명, B="지표", C열부터 날짜)이 있고,
그 아래 상품별로 [링크 줄 → 지표 줄들 → 빈 줄]이 반복된다.
블록: 단품 / 번들 / 소셜 / 경쟁, 맨 아래 '메모'.

만드는 것
- docs/data/amazon.json : 패널용. 시트 전체 이력을 매번 새로 만든다.
- history/centellian_amazon.csv : 긴 형식(date, block, product, metric, value)
  백업. 이미 있는 (date, block, product, metric) 조합은 다시 쓰지 않고,
  새로 생긴 칸만 뒤에 붙인다. 나중에 빈칸을 채워 넣은 것도 새 칸으로 들어간다.

시트를 못 읽으면 기존 파일을 건드리지 않고 끝낸다.
손으로 시험할 때: python scripts/fetch_amazon.py --file 받은파일.csv
"""

import csv
import io
import json
import re
import sys
from pathlib import Path

import requests

from common import CONFIG_DIR, DATA_DIR, load_config, now_kst

# 설정 파일이 없을 때만 쓰는 예비 주소(예전 시트)
FALLBACK_URL = ("https://docs.google.com/spreadsheets/d/e/"
             "2PACX-1vTolwFm5uMd-VC-hz-hOgl3ILtZOC5TvwEahdkXBJNGv4X2HxLfsyu5mBGQiXrI7PTcQhYFFBSl_YND"
             "/pub?output=csv")

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "history" / "centellian_amazon.csv"
OUT = DATA_DIR / "amazon.json"

def sheet_csv_url():
    url = ""
    if (CONFIG_DIR / "amazon.yaml").exists():
        url = str((load_config("amazon.yaml") or {}).get("sheet_url") or "").strip()
    url = url or FALLBACK_URL
    # .../pubhtml, .../pubhtml?gid=..., .../pub?output=csv 모두 CSV 주소로
    gid = re.search(r"[?&#]gid=(\d+)", url)
    base = re.split(r"/pub(?:html)?\b", url)[0]
    return f"{base}/pub?output=csv" + (f"&gid={gid.group(1)}" if gid else "")


BLOCKS = ["단품", "번들", "소셜", "경쟁"]
CORE_IDS = {"1", "2", "3", "4", "15"}          # 주력 5종 (단품 번호)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# BSR 이 아닌 지표들. 여기 없는 지표 이름은 BSR(카테고리 순위)로 본다.
PLAIN_METRICS = {
    "price": "price", "할인률": "discount", "review": "review",
    "bought in past month": "bought", "개당 환산가": "unit_price",
    "월 구매수 (세트)": "bought", "환산 수량 (개)": "units",
    "팔로워": "followers", "게시물": "posts", "상태": "status",
}


def metric_kind(name):
    base = re.sub(r"\s*\(.*?\)\s*$", "", name).strip().lower()  # "(공유)" 등 꼬리표 제거
    if base in PLAIN_METRICS:
        return PLAIN_METRICS[base]
    if base.startswith("beauty & personal care"):
        return "bsr_main"
    return "bsr_sub"


def parse_value(s):
    """'31%'→31, '$1,234.5'→1234.5, 빈칸→None, 숫자가 아니면 글자 그대로."""
    s = (s or "").strip()
    if not s:
        return None
    t = s.replace(",", "").replace("$", "").replace("%", "").strip()
    try:
        v = float(t)
        return int(v) if v.is_integer() else v
    except ValueError:
        return s


def product_id(name):
    m = re.match(r"^([A-Z]?\d+)\s", name)
    return m.group(1) if m else name


def parse_sheet(text):
    rows = list(csv.reader(io.StringIO(text)))
    blocks, memo = {}, []
    block = None
    dates = {}          # 열 번호 → 날짜
    prod = None

    for row in rows:
        row = row + [""] * (3 - len(row))
        a, b = row[0].strip(), row[1].strip()

        if a == "메모":
            block = "메모"
            continue
        if block == "메모":
            if b and row[2].strip():
                memo.append([b, row[2].strip()])
            continue

        if b == "지표" and a in BLOCKS:            # 블록 제목 줄
            block = a
            dates = {i: c.strip() for i, c in enumerate(row)
                     if i >= 2 and DATE_RE.match(c.strip())}
            blocks.setdefault(block, [])
            prod = None
            continue
        if block is None:
            continue

        if not a and not b:                        # 빈 줄 = 상품 끝
            prod = None
            continue

        if a and b == "링크":                       # 상품 첫 줄
            link = next((c.strip() for c in row[2:] if c.strip().startswith("http")), "")
            prod = {"id": product_id(a), "name": a, "link": link,
                    "core": block == "단품" and product_id(a) in CORE_IDS,
                    "metrics": []}
            blocks[block].append(prod)
            continue

        if prod is not None and b:                 # 지표 줄
            series = []
            for i, d in dates.items():
                v = parse_value(row[i] if i < len(row) else "")
                if v is not None:
                    series.append([d, v])
            prod["metrics"].append({"name": b, "kind": metric_kind(b),
                                    "series": series})

    return blocks, memo


def to_long(blocks):
    out = []
    for block, prods in blocks.items():
        for p in prods:
            for m in p["metrics"]:
                for d, v in m["series"]:
                    out.append([d, block, p["name"], m["name"], v])
    return out


def append_history(long_rows):
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if HISTORY.exists():
        with HISTORY.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                seen.add((r["date"], r["block"], r["product"], r["metric"]))
    new = [r for r in long_rows if tuple(r[:4]) not in seen]
    new.sort(key=lambda r: (r[0], BLOCKS.index(r[1]) if r[1] in BLOCKS else 9))
    write_header = not HISTORY.exists()
    with HISTORY.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["date", "block", "product", "metric", "value"])
        w.writerows(new)
    return len(new)


def main():
    if "--file" in sys.argv:
        text = Path(sys.argv[sys.argv.index("--file") + 1]).read_text(encoding="utf-8")
    else:
        try:
            url = sheet_csv_url()
            print(f"시트 주소: {url[:70]}…")
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            r.encoding = "utf-8"
            text = r.text
        except Exception as e:
            print(f"시트를 못 읽었습니다 -- 기존 데이터 유지: {e}", file=sys.stderr)
            return

    blocks, memo = parse_sheet(text)
    n_prod = sum(len(v) for v in blocks.values())
    if n_prod == 0:
        print("시트에서 상품을 하나도 못 찾았습니다 -- 기존 데이터 유지", file=sys.stderr)
        return

    all_dates = sorted({d for prods in blocks.values() for p in prods
                        for m in p["metrics"] for d, _ in m["series"]})

    data = {
        "updated": now_kst().strftime("%Y-%m-%d %H:%M"),
        "last_date": all_dates[-1] if all_dates else None,
        "dates": all_dates,
        "blocks": [{"name": k, "products": blocks[k]} for k in BLOCKS if k in blocks],
        "memo": memo,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    added = append_history(to_long(blocks))
    print(f"상품 {n_prod}개 · 날짜 {len(all_dates)}개 (최근 {data['last_date']}) · 이력 새 칸 {added}개")


if __name__ == "__main__":
    main()
