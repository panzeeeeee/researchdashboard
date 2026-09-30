"""한국 딥밸류용 재무 자료 -- DART 전체 재무제표를 미국 딥밸류(dv_metrics)가 기대하는 fd 딕셔너리로.

미국판은 야후의 재무제표(income_stmt·balance_sheet·cash_flow·분기)를 `fd` 딕셔너리로 만들어
dv_metrics 의 판정 함수에 넘긴다. 여기서는 같은 이름·같은 뜻의 `fd` 를 DART 에서 만든다.
판정 로직은 건드리지 않는다.

받는 것 (종목 하나당, 결과는 캐시에 저장 -- 공시는 한 번 나오면 바뀌지 않는다)
  - 사업보고서(연간) 전체 재무제표   : 당기·전기·전전기 3개년 BS·IS·CF  (연결 우선, 없으면 개별)
  - 3년 전 사업보고서 주요계정        : 매출·영업이익 5~6년 이력 (가벼운 요청)
  - 분기 보고서(최근 3개 분기)       : 3개월 값·누적·전년 같은 분기 값
  - 주식총수 현황                    : 보통주 발행주식·자기주식 (당기·전기·3년 전·최근)

DART 응답 구조 (probe_out/ 시험으로 확인)
  - 분기 IS: thstrm_amount = 그 분기 3개월, thstrm_add_amount = 누적,
             frmtrm_q_amount = 전년 같은 분기 3개월, frmtrm_add_amount = 전년 누적
  - 분기 BS: thstrm_amount = 분기말, frmtrm_amount = 직전 사업연도말
  - 연간   : thstrm / frmtrm / bfefrmtrm = 당기 / 전기 / 전전기
  - 금액은 원 단위. 계정은 표준 코드(ifrs-full_..., dart_...)로 찾고, 코드가 없는 계정은 이름으로 찾는다.
  - 감가상각비는 회사에 따라 현금흐름표에 있기도 없기도 하다 -- 없으면 EBITDA 는 비워 둔다(추측하지 않는다).
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime

import numpy as np
import requests

from common import ROOT, env, now_kst

BASE = "https://opendart.fss.or.kr/api"
UA = {"User-Agent": "Mozilla/5.0 (compatible; research-dashboard/1.0)"}
CACHE_DIR = ROOT / "screener" / "dv_kr_cache"
CORP_FILE = ROOT / "screener" / "dart_corp_codes.json"
NEG_TTL_DAYS = 20          # '아직 공시 없음' 결과를 이만큼 기억한다
NAN = float("nan")

STATS = {"calls": 0, "cache_hits": 0, "fail": 0}
_lock = threading.Lock()
_corp = None


# ---------------------------------------------------------------- 기본 도구

def corp_map():
    global _corp
    if _corp is None:
        _corp = json.loads(CORP_FILE.read_text(encoding="utf-8"))
    return _corp


def _bump(k):
    with _lock:
        STATS[k] += 1


def dart_json(path, **params):
    """DART 요청. 요청 제한(020)이나 일시 오류는 쉬었다 다시. 끝내 못 받으면 status ERR."""
    key = env("DART_API_KEY")
    params["crtfc_key"] = key
    for i in range(4):
        try:
            r = requests.get(f"{BASE}/{path}", params=params, headers=UA, timeout=90)
            r.raise_for_status()
            d = r.json()
        except Exception:
            time.sleep(2 * (i + 1))
            continue
        _bump("calls")
        if d.get("status") == "020":          # 요청 제한 초과
            time.sleep(6 * (i + 1))
            continue
        return d
    _bump("fail")
    return {"status": "ERR", "list": []}


def amt(s):
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if t in ("", "-"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _check(d):
    """000(정상)·013(공시 없음) 이 아니면 일시 오류 -- 예외로 알려 '없음'으로 기억하지 않게 한다."""
    if d.get("status") not in ("000", "013"):
        raise RuntimeError(f"DART {d.get('status')} {d.get('message', '')}")


def _cache_path(key):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.json"


def cached(key, fn, neg_ttl_days=NEG_TTL_DAYS):
    """결과를 파일로 기억한다. fn() 이 None 이면 '없음'을 neg_ttl_days 동안 기억."""
    p = _cache_path(key)
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if "_none" not in d:
                _bump("cache_hits")
                return d
            if time.time() - d["_none"] < neg_ttl_days * 86400:
                _bump("cache_hits")
                return None
        except Exception:
            pass
    v = fn()
    p.write_text(json.dumps(v if v is not None else {"_none": time.time()}, ensure_ascii=False), encoding="utf-8")
    return v


# ---------------------------------------------------------------- 계정 찾기

def _id_tail(r):
    a = r.get("account_id") or ""
    return a.split("_", 1)[1] if "_" in a else a


def find_one(rows, divs, ids=(), name_re=None):
    for dv in divs:
        for want in ids:
            for r in rows:
                if r.get("sj_div") == dv and r.get("account_id") == want:
                    return r
        if name_re is not None:
            for r in rows:
                if r.get("sj_div") == dv and name_re.search(r.get("account_nm") or ""):
                    return r
    return None


def find_all(rows, div, pred):
    return [r for r in rows if r.get("sj_div") == div and pred(r)]


REV_RE = re.compile(r"^(매출액|매출|영업수익|수익\(매출액\)|총수익)$")
IS_SPECS = {
    "revenue": (["ifrs-full_Revenue"], REV_RE),
    "cogs": (["ifrs-full_CostOfSales"], re.compile(r"^매출원가$")),
    "gross_profit": (["ifrs-full_GrossProfit"], re.compile(r"^매출총이익")),
    "op_income": (["dart_OperatingIncomeLoss"], re.compile(r"^영업(이익|손익|손실)")),
    "net_income": (["ifrs-full_ProfitLoss"], re.compile(r"(당기|분기|반기)순(이익|손익|손실)")),
    "finance_costs": (["ifrs-full_FinanceCosts"], re.compile(r"^금융(비용|원가)")),
    "interest_exp": (["ifrs-full_InterestExpense", "dart_InterestExpense"], re.compile(r"^이자비용")),
}
DEBT_NAME_RE = re.compile(r"(차입금|사채|리스부채|리스 부채|전환사채|신주인수권부사채|교환사채|유동성장기부채)")
DEBT_EXCL_RE = re.compile(r"(할인|이자|충당|미지급|발행비|상환|예수|보증|미수)")
DEBT_ID_KEYS = ("Borrowings", "BondsIssued", "LeaseLiabilities", "DebenturesAndBonds", "ConvertibleBonds",
                "LoansReceived", "DebtInstruments")
NONCUR_NAME_RE = re.compile(r"^(장기|비유동|사채$|전환사채$|신주인수권부사채$|교환사채$)")
DA_ID_PREFIX = ("AdjustmentsForDepreciation", "AdjustmentsForAmortisation", "DepreciationAndAmortisation",
                "DepreciationExpense", "AmortisationExpense", "DepreciationRightofuse")
DA_NAME_RE = re.compile(r"^(감가상각비|무형자산상각비|사용권자산상각비|감가상각비및무형자산상각비|상각비)$")


def _cols_a(r):
    return [amt(r.get("thstrm_amount")), amt(r.get("frmtrm_amount")), amt(r.get("bfefrmtrm_amount"))]


def _sum3(rows):
    """행 여러 개의 열별 합. 전부 비어 있으면 None."""
    if not rows:
        return None
    out = []
    for i in range(3):
        vs = [_cols_a(r)[i] for r in rows]
        vs = [v for v in vs if v is not None]
        out.append(sum(vs) if vs else None)
    return out if any(v is not None for v in out) else None


def is_debt_row(r):
    nm = r.get("account_nm") or ""
    if DEBT_EXCL_RE.search(nm):
        return False
    tail = _id_tail(r)
    if any(k in tail for k in DEBT_ID_KEYS) and not any(x in tail for x in ("Provisions", "Payables", "Receivable")):
        return True
    return bool(DEBT_NAME_RE.search(nm))


def is_noncurrent_debt(r):
    tail = _id_tail(r)
    if tail.startswith(("Current", "Shortterm")):
        return False
    if tail.startswith(("Noncurrent", "Longterm")):
        return True
    nm = r.get("account_nm") or ""
    if nm.startswith(("단기", "유동")):
        return False
    return bool(NONCUR_NAME_RE.search(nm))


def parse_annual(rows):
    """사업보고서 전체 재무제표 -> 필드마다 [당기, 전기, 전전기]."""
    out = {}
    for f, (ids, nre) in IS_SPECS.items():
        r = find_one(rows, ("IS", "CIS"), ids, nre)
        out[f] = _cols_a(r) if r else None
    bs1 = {
        "total_assets": ["ifrs-full_Assets"], "cur_assets": ["ifrs-full_CurrentAssets"],
        "cur_liab": ["ifrs-full_CurrentLiabilities"], "equity_total": ["ifrs-full_Equity"],
        "equity_owner": ["ifrs-full_EquityAttributableToOwnersOfParent"],
        "inventory": ["ifrs-full_Inventories"],
    }
    for f, ids in bs1.items():
        r = find_one(rows, ("BS",), ids)
        out[f] = _cols_a(r) if r else None
    # 현금: 현금및현금성자산 + 단기금융상품
    cash = [find_one(rows, ("BS",), ["ifrs-full_CashAndCashEquivalents"]),
            find_one(rows, ("BS",), ["ifrs-full_ShorttermDepositsNotClassifiedAsCashEquivalents"])]
    out["cash"] = _sum3([r for r in cash if r])
    # 영업권·무형자산
    r = find_one(rows, ("BS",), ["ifrs-full_IntangibleAssetsAndGoodwill"])
    if r:
        out["goodwill_intang"] = _cols_a(r)
    else:
        gs = [find_one(rows, ("BS",), ["ifrs-full_Goodwill"]),
              find_one(rows, ("BS",), ["ifrs-full_IntangibleAssetsOtherThanGoodwill"])]
        out["goodwill_intang"] = _sum3([g for g in gs if g])
    # 차입금·사채·리스부채 (부채 행만)
    debt = find_all(rows, "BS", is_debt_row)
    out["total_debt"] = _sum3(debt)
    out["lt_debt"] = _sum3([r for r in debt if is_noncurrent_debt(r)])
    # 현금흐름
    r = find_one(rows, ("CF",), ["ifrs-full_CashFlowsFromUsedInOperatingActivities"], re.compile(r"^영업활동\s*현금흐름"))
    out["cfo"] = _cols_a(r) if r else None
    capex = find_all(rows, "CF", lambda x: (x.get("account_id") or "").startswith(
        ("ifrs-full_PurchaseOfPropertyPlantAndEquipment", "ifrs-full_PurchaseOfIntangibleAssets")))
    out["capex"] = _sum3(capex)
    tre = find_all(rows, "CF", lambda x: x.get("account_id") == "dart_AcquisitionOfTreasuryShares"
                   or re.search(r"자기주식.*(취득|매입)", x.get("account_nm") or ""))
    out["treasury_buy"] = _sum3(tre)
    da = find_all(rows, "CF", lambda x: _id_tail(x).startswith(DA_ID_PREFIX)
                  or DA_NAME_RE.search((x.get("account_nm") or "").replace(" ", "")))
    out["da"] = _sum3([r for r in da if not re.search(r"손상|충당|대손", r.get("account_nm") or "")])
    if out["interest_exp"] is None:                 # 손익계산서에 이자비용이 없으면 현금흐름표 조정 항목
        r = find_one(rows, ("CF",), ["dart_AdjustmentsForInterestExpenses"], re.compile(r"^이자비용$"))
        out["interest_exp_cf"] = _cols_a(r) if r else None
    # 현금흐름표의 '이자의 지급'(실제로 낸 이자). 금융비용은 환율·파생상품 손실이 섞여 과대하므로 이걸 먼저 쓴다
    ip = find_all(rows, "CF", lambda x: (x.get("account_id") or "").startswith("ifrs-full_InterestPaid")
                  or re.match(r"^이자(의)?\s*지급$", (x.get("account_nm") or "").strip()))
    out["interest_paid"] = _sum3(ip)
    return out


def parse_quarter(rows):
    """분기 보고서 -> IS 필드마다 {q, cum, pq, pcum}, BS 재고자산 {now, fye}."""
    out = {}
    for f in ("revenue", "cogs", "gross_profit", "op_income", "net_income"):
        ids, nre = IS_SPECS[f]
        r = find_one(rows, ("IS", "CIS"), ids, nre)
        out[f] = ({"q": amt(r.get("thstrm_amount")), "cum": amt(r.get("thstrm_add_amount")),
                   "pq": amt(r.get("frmtrm_q_amount")), "pcum": amt(r.get("frmtrm_add_amount"))} if r else None)
    r = find_one(rows, ("BS",), ["ifrs-full_Inventories"])
    out["inventory"] = {"now": amt(r.get("thstrm_amount")), "fye": amt(r.get("frmtrm_amount"))} if r else None
    return out


# ---------------------------------------------------------------- DART 호출 (캐시)

def get_full(corp, year, reprt, fs_pref=None):
    """전체 재무제표. 연결(CFS) 먼저, 없으면 개별(OFS). 반환 (parsed, fs) 또는 (None, None)."""
    kind = "A" if reprt == "11011" else "Q"
    fss = [fs_pref] if fs_pref else ["CFS", "OFS"]
    for fs in fss:
        def fetch(fs=fs):
            d = dart_json("fnlttSinglAcntAll.json", corp_code=corp, bsns_year=str(year), reprt_code=reprt, fs_div=fs)
            _check(d)
            if d.get("status") != "000" or not d.get("list"):
                return None
            p = parse_annual(d["list"]) if kind == "A" else parse_quarter(d["list"])
            p["_fs"] = fs
            return p
        p = cached(f"F_{corp}_{year}_{reprt}_{fs}", fetch)
        if p:
            return p, fs
    return None, None


def get_main(corp, year):
    """주요계정(가벼움): 매출·영업이익 3개년 [당기, 전기, 전전기]. 연결 우선."""
    def fetch():
        d = dart_json("fnlttSinglAcnt.json", corp_code=corp, bsns_year=str(year), reprt_code="11011")
        _check(d)
        rows = d.get("list") or []
        if d.get("status") != "000" or not rows:
            return None
        res = {}
        for fs in ("CFS", "OFS"):
            sub = [r for r in rows if r.get("fs_div") == fs and r.get("sj_div") == "IS"]
            if not sub:
                continue
            rev = next((r for r in sub if REV_RE.search(r.get("account_nm") or "")), None)
            op = next((r for r in sub if re.search(r"^영업(이익|손익|손실)", r.get("account_nm") or "")), None)
            res = {"fs": fs, "revenue": _cols_a(rev) if rev else None, "op_income": _cols_a(op) if op else None}
            break
        return res or None
    return cached(f"M_{corp}_{year}", fetch)


def get_shares(corp, year, reprt="11011"):
    """보통주 발행주식총수·자기주식수."""
    def fetch():
        d = dart_json("stockTotqySttus.json", corp_code=corp, bsns_year=str(year), reprt_code=reprt)
        _check(d)
        rows = d.get("list") or []
        if d.get("status") != "000" or not rows:
            return None
        pick = next((r for r in rows if str(r.get("se", "")).startswith("보통")), None) \
            or next((r for r in rows if str(r.get("se", "")).startswith("합계")), None)
        if not pick:
            return None
        return {"issued": amt(pick.get("istc_totqy")), "treasury": amt(pick.get("tesstk_co"))}
    return cached(f"S_{corp}_{year}_{reprt}", fetch)


# ---------------------------------------------------------------- fd 조립

def _v(cols, i):
    if cols is None or i >= len(cols) or cols[i] is None:
        return NAN
    return float(cols[i])


def _elem(a, b, fn):
    if a is None or b is None:
        return None
    return [(fn(x, y) if x is not None and y is not None else None) for x, y in zip(a, b)]


def _gp(A):
    """매출총이익: 있으면 그대로, 없으면 매출 - 매출원가."""
    if A.get("gross_profit"):
        return A["gross_profit"]
    return _elem(A.get("revenue"), A.get("cogs"), lambda r, c: r - c)


def _quarter_table(corp, fs, Y, A):
    """분기 값 표 T[(연도, 분기)] = {revenue, gp, op, ni, inv}. 최신 분기부터 3개 분기를 만들 수 있게 필요한 보고서만 받는다."""
    Yq = Y + 1
    reps, cum, T = {}, {}, {}

    def fld(p, f):
        return p.get(f) if p else None

    def load(year, q):
        code = {1: "11013", 2: "11012", 3: "11014"}[q]
        p, _ = get_full(corp, year, code, fs)
        return p

    # 최신 분기 찾기 (Yq 의 3, 2, 1 분기)
    latest = 0
    for q in (3, 2, 1):
        p = load(Yq, q)
        if p:
            reps[(Yq, q)] = p
            latest = q
            break
    if latest:                                   # 그 전 분기들도 (최대 3개 분기가 필요)
        for q in range(latest - 1, 0, -1):
            p = load(Yq, q)
            if p:
                reps[(Yq, q)] = p
        if latest <= 2:                          # 전년 3분기(4분기 계산에 필요)
            p = load(Y, 3)
            if p:
                reps[(Y, 3)] = p
    else:                                        # Yq 분기 공시가 아직 없음 -> 최신은 Y 의 4분기
        for q in (3, 2):
            p = load(Y, q)
            if p:
                reps[(Y, q)] = p

    def put(key, f, val):
        if val is not None and T.get(key, {}).get(f) is None:      # 비어 있던 자리만 채운다
            T.setdefault(key, {})[f] = val

    for (yr, q), p in reps.items():
        for f in ("revenue", "cogs", "gross_profit", "op_income", "net_income"):
            c = fld(p, f)
            if c:
                put((yr, q), f, c["q"])
                put((yr - 1, q), f, c["pq"])
                cum[(yr, q, f)] = c["cum"]
                cum[(yr - 1, q, f)] = c["pcum"]
        inv = p.get("inventory")
        if inv:
            put((yr, q), "inv", inv["now"])
    # 4분기 = 연간 - 3분기 누적
    for yr, col in ((Y, 0), (Y - 1, 1)):
        for f, af in (("revenue", "revenue"), ("cogs", "cogs"), ("gross_profit", "gross_profit"),
                      ("op_income", "op_income"), ("net_income", "net_income")):
            a = (A.get(af) or [None] * 3)[col]
            c3 = cum.get((yr, 3, f))
            if a is not None and c3 is not None:
                put((yr, 4), f, a - c3)
    inv0 = (A.get("inventory") or [None])[0]
    if inv0 is not None:
        put((Y, 4), "inv", inv0)

    # 최신 분기부터 거꾸로 순서
    if latest:
        cur = (Yq, latest)
    else:
        cur = (Y, 4)
    seq = []
    y_, q_ = cur
    for _ in range(8):
        seq.append((y_, q_))
        q_ -= 1
        if q_ == 0:
            q_, y_ = 4, y_ - 1
    return T, seq, cur


def build_fd(sc, name, sector, industry, price):
    """종목 하나의 fd. 실패하면 {"ok": False, "error": ...}. 일시 오류는 캐시에 남기지 않는다."""
    try:
        return _build_fd(sc, name, sector, industry, price)
    except Exception as e:                       # noqa: BLE001
        return {"ticker": sc, "ok": False, "error": f"api_error: {str(e)[:80]}"}


def _build_fd(sc, name, sector, industry, price):
    cm = corp_map().get(sc)
    if not cm:
        return {"ok": False, "error": "no_corp_code"}
    corp = cm[0]
    fd = {"ticker": sc, "ok": False, "name": name, "sector": sector, "industry": industry,
          "fetched": now_kst().isoformat()}
    today = now_kst()
    Y = None
    A = fs = None
    for yr in (today.year - 1, today.year - 2):
        A, fs = get_full(corp, yr, "11011")
        if A:
            Y = yr
            break
    if not A:
        fd["error"] = "annual_missing"
        return fd
    fd["fs_div"], fd["fy"] = fs, Y

    # ---- 손익·이력
    main = get_main(corp, Y - 3)
    rev3 = A.get("revenue") or [None] * 3
    op3 = A.get("op_income") or [None] * 3
    rev_hist = [v for v in rev3 if v is not None]
    op_hist = list(op3)
    if main and main.get("fs") == fs:
        for c in (main.get("revenue") or []):
            if c is not None:
                rev_hist.append(c)
        op_hist += list(main.get("op_income") or [None] * 3)
    fd["rev_hist"] = [float(x) for x in rev_hist[:6]]
    fd["rev_ttm"] = _v(rev3, 0)
    fd["revenue_p1"] = _v(rev3, 1)
    revs = [v for v in rev3] + list((main or {}).get("revenue") or [None] * 3) if (main and main.get("fs") == fs) else list(rev3)
    margins = []
    for i in range(min(5, len(revs), len(op_hist))):
        if revs[i] and revs[i] > 0 and op_hist[i] is not None:
            margins.append(op_hist[i] / revs[i])
    fd["ebit_margin_hist"] = [float(m) for m in margins]
    fd["ebit_margin_med5"] = float(np.median(margins)) if margins else NAN

    gp = _gp(A)
    da = A.get("da")
    ebitda = _elem(A.get("op_income"), da, lambda o, d: o + d)     # 감가상각비를 알 때만
    # 이자비용: 손익계산서 -> 현금흐름표 조정 항목 -> 이자 지급액 -> (마지막) 금융비용
    src = [("interest", A.get("interest_exp")), ("interest_cf", A.get("interest_exp_cf")),
           ("interest_paid", A.get("interest_paid")), ("finance_costs", A.get("finance_costs"))]
    intr, fd["interest_exp_src"] = next(((c, n) for n, c in src if c), (None, "none"))
    # 주의: 미국판은 fd 에 'ebit' 를 넣지 않아 이자보상배율이 정상화 EBIT(5년 중앙 마진 x 매출)로 계산된다.
    #       같은 기준으로 비교하려고 여기서도 'ebit' 는 넣지 않는다(영업이익은 op_income 으로만 남긴다).
    for k, cols in (("gross_profit", gp), ("ebitda", ebitda), ("net_income", A.get("net_income")),
                    ("interest_exp", intr), ("op_income", A.get("op_income"))):
        fd[k] = _v(cols, 0)
        fd[k + "_p1"] = _v(cols, 1)
    eq = A.get("equity_owner") or A.get("equity_total")
    for k, cols in (("total_assets", A.get("total_assets")), ("cur_assets", A.get("cur_assets")),
                    ("cur_liab", A.get("cur_liab")), ("total_debt", A.get("total_debt")),
                    ("lt_debt", A.get("lt_debt")), ("cash", A.get("cash")), ("equity", eq),
                    ("goodwill_intang", A.get("goodwill_intang")), ("inventory", A.get("inventory"))):
        fd[k] = _v(cols, 0)
        fd[k + "_p1"] = _v(cols, 1)
    fd["total_assets_p2"] = _v(A.get("total_assets"), 2)
    # 총차입이 없으면 0 이 아니라 '없음'(무차입일 수 있다) -- 재무제표에 차입 계정이 하나도 없으면 0 으로 본다
    if fd["total_debt"] != fd["total_debt"]:
        fd["total_debt"] = 0.0 if A.get("total_assets") else NAN
    if fd["lt_debt"] != fd["lt_debt"]:
        fd["lt_debt"] = 0.0 if A.get("total_assets") else NAN
    fd["lt_debt_p1"] = fd["lt_debt_p1"] if fd["lt_debt_p1"] == fd["lt_debt_p1"] else (0.0 if A.get("total_assets") else NAN)

    # ---- 현금흐름
    fd["cfo"] = _v(A.get("cfo"), 0)
    capex = A.get("capex")
    fd["capex"] = -_v(capex, 0) if capex else NAN
    fcf = _elem(A.get("cfo"), capex, lambda c, x: c - abs(x)) if capex else None
    fd["fcf"] = _v(fcf, 0)
    f3 = [x for x in (fcf or []) if x is not None]
    fd["fcf_avg3"] = float(np.mean(f3)) if f3 else NAN
    tre = A.get("treasury_buy")
    fd["buyback"] = -abs(_v(tre, 0)) if tre and _v(tre, 0) == _v(tre, 0) and _v(tre, 0) != 0 else 0.0

    # ---- 주식 수 (보통주 발행주식총수)
    s0, s1, s3 = get_shares(corp, Y), get_shares(corp, Y - 1), get_shares(corp, Y - 3)
    fd["shares"] = float(s0["issued"]) if s0 and s0.get("issued") else NAN
    fd["shares_p1"] = float(s1["issued"]) if s1 and s1.get("issued") else NAN
    fd["shares_p3"] = float(s3["issued"]) if s3 and s3.get("issued") else NAN
    cur_sh = None
    for code in ("11014", "11012", "11013"):        # 최신 분기 기준 주식 수가 있으면 그걸로 시총 계산
        s = get_shares(corp, Y + 1, code)
        if s and s.get("issued"):
            cur_sh = float(s["issued"])
            break
    cur_sh = cur_sh or fd["shares"]
    fd["shares_out"] = cur_sh
    fd["market_cap"] = float(price) * cur_sh if price and cur_sh == cur_sh else NAN
    eq0 = fd["equity"]
    fd["pb"] = fd["market_cap"] / eq0 if eq0 == eq0 and eq0 and eq0 > 0 and fd["market_cap"] == fd["market_cap"] else NAN

    # ---- 분기
    T, seq, cur = _quarter_table(corp, fs, Y, A)

    def val(key, f):
        v = T.get(key, {}).get(f)
        return NAN if v is None else float(v)

    def gp_q(key):
        g = T.get(key, {}).get("gross_profit")
        if g is None:
            r, c = T.get(key, {}).get("revenue"), T.get(key, {}).get("cogs")
            g = (r - c) if r is not None and c is not None else None
        return g

    q_rev = [val(k, "revenue") for k in seq[:6]]
    fd["q_rev"] = q_rev
    fd["q_ni"] = [val(seq[0], "net_income"), val(seq[1], "net_income"), val(seq[2], "net_income"), NAN,
                  val(seq[4], "net_income")]
    gm, om = [], []
    for k in seq[:3]:
        r, g, o = T.get(k, {}).get("revenue"), gp_q(k), T.get(k, {}).get("op_income")
        if r and r > 0 and g is not None:
            gm.append(float(g / r))
        else:
            break
    for k in seq[:3]:
        r, o = T.get(k, {}).get("revenue"), T.get(k, {}).get("op_income")
        if r and r > 0 and o is not None:
            om.append(float(o / r))
        else:
            break
    fd["q_gross_margin"], fd["q_op_margin"] = gm, om

    def yoy(i, j):
        a, b = val(seq[i], "revenue"), val(seq[j], "revenue")
        return float(a / b - 1) if a == a and b == b and b else NAN
    fd["rev_yoy_q0"], fd["rev_yoy_q1"] = yoy(0, 4), yoy(1, 5)
    inv = [T.get(k, {}).get("inv") for k in seq[:2]]
    fd["q_inventory"] = [float(x) for x in inv if x is not None] if all(x is not None for x in inv) else []
    fd["q_shares"], fd["q_cash"], fd["q_debt"] = [], [], []
    fd["q_latest"] = f"{cur[0]}Q{cur[1]}"

    # ---- 야후에만 있던 값 (한국은 출처가 없다): 추정치 수정, 공매도, 지분율
    for k in ("rev_up_30d", "rev_down_30d", "eps_trend_30d_chg", "eps_trend_90d_chg",
              "beta", "trailing_pe", "forward_pe", "held_insiders", "held_inst", "short_pct_float", "div_yield"):
        fd[k] = NAN
    fd["ev_info"] = NAN
    fd["ok"] = bool(fd["total_assets"] == fd["total_assets"])
    return fd
