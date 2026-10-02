"""시장 요약 + 스크리너 결과를 텔레그램으로 보낸다.

기존 push_telegram.py(새 뉴스만 보냄)와 목적이 다르다 -- 이건 그날
스냅샷을 한 방에 보낸다. 중복 방지 기록(sent.json) 없이 현재 상태를 발송한다.

- 아침 실행(한국시간 오후 1시 이전)에만 보낸다. 저녁 실행은 대시보드
  데이터만 갱신하고 이 스크립트는 그냥 넘어간다.
  손으로 돌릴 때 시간과 상관없이 보내려면 DIGEST_FORCE=1.
- 종목마다 1년치 종가(chart 배열)로 matplotlib 차트 PNG를 직접 그려
  sendPhoto 로 첨부한다. 외부 차트 URL(Finviz 등)은 403으로 막혀서 안 쓴다.
- matplotlib 이 없거나 차트 데이터가 없으면 텍스트로만 보낸다.

market.json / credit.json / liquidity.json / market_notes.json /
breakout_cards.json / deepvalue_cards.json / extremes.json 을 읽는다
-- 그 앞 단계들이 다 끝난 뒤에 돌아야 한다.
"""

import html
import io
import sys
import time

import requests

from datetime import datetime, timedelta

from common import DATA_DIR, env, now_kst, read_json

API = "https://api.telegram.org/bot{token}/sendMessage"
API_PHOTO = "https://api.telegram.org/bot{token}/sendPhoto"

MORNING_CUTOFF_HOUR = 13   # 한국시간 이 시각 전에 돈 실행만 '아침'으로 본다


# ---------------------------------------------------------------- 발송

def _retry_after(r):
    try:
        return r.json().get("parameters", {}).get("retry_after", 5)
    except Exception:
        return 5


def send(token, chat_id, text, tries=4):
    """429(Too Many Requests)면 텔레그램이 알려주는 대기시간만큼 쉬고 재시도."""
    for attempt in range(tries):
        r = requests.post(
            API.format(token=token), timeout=20,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
        )
        if r.ok:
            return True
        if r.status_code == 429:
            wait = _retry_after(r)
            print(f"  429 -- {wait}초 대기 후 재시도", file=sys.stderr)
            time.sleep(wait + 1)
            continue
        print(f"발송 실패: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return False
    return False


def send_photo_bytes(token, chat_id, png, caption, tries=4):
    """직접 그린 차트 PNG(바이트)를 캡션과 함께 보낸다.
    캡션은 1024자 제한 -- 넘으면 사진엔 첫 줄만 달고 전체 내용은 텍스트로 이어 보낸다.
    사진 발송이 실패하면 텍스트라도 보낸다."""
    long_cap = len(caption) > 1024
    cap = caption.split("\n", 1)[0] if long_cap else caption
    for attempt in range(tries):
        r = requests.post(
            API_PHOTO.format(token=token), timeout=60,
            data={"chat_id": chat_id, "caption": cap, "parse_mode": "HTML"},
            files={"photo": ("chart.png", png, "image/png")},
        )
        if r.ok:
            if long_cap:
                send(token, chat_id, caption)
            return True
        if r.status_code == 429:
            wait = _retry_after(r)
            print(f"  사진 429 -- {wait}초 대기 후 재시도", file=sys.stderr)
            time.sleep(wait + 1)
            continue
        print(f"사진 실패: {r.status_code} {r.text[:150]} -- 텍스트로 대체", file=sys.stderr)
        return send(token, chat_id, caption)
    return send(token, chat_id, caption)


# ---------------------------------------------------------------- 차트

def _num(x):
    try:
        v = float(x)
        return v if v == v else None   # NaN 제외
    except (TypeError, ValueError):
        return None


def extract_series(raw):
    """카드의 chart 값에서 (날짜목록 or None, 종가목록)을 뽑는다.
    형식이 [숫자...], [[날짜, 종가]...], [{date, close}...] 어느 쪽이어도 받는다."""
    if isinstance(raw, dict):
        # {"dates": [...], "close": [...]} 같은 형태
        closes = None
        for k in ("close", "closes", "c", "values", "y", "prices"):
            if isinstance(raw.get(k), list):
                closes = raw[k]
                break
        dates = None
        for k in ("dates", "date", "d", "t", "x"):
            if isinstance(raw.get(k), list):
                dates = raw[k]
                break
        if closes is None:
            return None, []
        raw = [[d, c] for d, c in zip(dates, closes)] if dates else closes

    if not isinstance(raw, list):
        return None, []

    dates, closes = [], []
    for p in raw:
        d, c = None, None
        if isinstance(p, (int, float, str)) and not isinstance(p, bool):
            c = _num(p)
        elif isinstance(p, (list, tuple)) and p:
            c = _num(p[-1]) if len(p) >= 2 else _num(p[0])
            d = p[0] if len(p) >= 2 else None
        elif isinstance(p, dict):
            for k in ("close", "c", "y", "v", "value", "price"):
                if k in p:
                    c = _num(p[k])
                    break
            for k in ("date", "d", "t", "x", "time"):
                if k in p:
                    d = p[k]
                    break
        if c is not None:
            closes.append(c)
            dates.append(d)
    if not any(isinstance(d, str) and len(d) >= 7 for d in dates):
        dates = None
    return dates, closes


def _ma(vals, n):
    out, s = [], 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def draw_chart(ticker, label, raw, color):
    """1년 종가 라인 + 50/200일선 + 52주 고가/저가 표시. PNG 바이트를 돌려준다.
    그릴 수 없으면 None. (러너에 한글 폰트가 없어 차트 안 글자는 영어만 쓴다)"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        print("  matplotlib 없음 -- 텍스트로만 보냄", file=sys.stderr)
        return None

    dates, closes = extract_series(raw)
    if len(closes) < 20:
        return None

    x = list(range(len(closes)))
    hi, lo, last = max(closes), min(closes), closes[-1]
    first = closes[0]
    chg_1y = (last / first - 1) * 100 if first else 0

    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=110)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    ax.fill_between(x, closes, lo * 0.98, color=color, alpha=0.08, linewidth=0)
    ax.plot(x, closes, color=color, linewidth=1.8, label="Close")

    for n, c, name in ((50, "#f59e0b", "MA50"), (200, "#6b7280", "MA200")):
        if len(closes) >= n:
            m = _ma(closes, n)
            xs = [i for i, v in enumerate(m) if v is not None]
            ax.plot(xs, [m[i] for i in xs], color=c, linewidth=1.1,
                    linestyle="--" if n == 200 else "-", label=name)

    ax.axhline(hi, color="#16a34a", linewidth=0.8, linestyle=":")
    ax.axhline(lo, color="#dc2626", linewidth=0.8, linestyle=":")
    ax.text(0, hi, f" 52W H {hi:,.2f}", color="#16a34a", fontsize=8, va="bottom")
    ax.text(0, lo, f" 52W L {lo:,.2f}", color="#dc2626", fontsize=8, va="top")

    ax.scatter([x[-1]], [last], color=color, s=22, zorder=5)
    ax.annotate(f"{last:,.2f}", (x[-1], last), textcoords="offset points",
                xytext=(6, 0), fontsize=9, color=color, fontweight="bold", va="center")

    # x축: 날짜가 있으면 월 표시, 없으면 눈금 숨김
    if dates:
        ticks, labels, prev = [], [], None
        for i, d in enumerate(dates):
            ym = str(d)[:7] if d else None
            if ym and ym != prev:
                ticks.append(i)
                labels.append(str(d)[2:7].replace("-", "."))
                prev = ym
        # 첫 달이 며칠만 걸쳐 있으면 라벨이 겹치니 뺀다
        if len(ticks) >= 2 and ticks[1] - ticks[0] < 12:
            ticks, labels = ticks[1:], labels[1:]
        step = max(1, len(ticks) // 7)
        ax.set_xticks(ticks[::step])
        ax.set_xticklabels(labels[::step], fontsize=8)
    else:
        ax.set_xticks([])

    ax.tick_params(axis="y", labelsize=8)
    ax.set_xlim(0, len(closes) - 1 + len(closes) * 0.06)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", alpha=0.2)
    ax.legend(loc="lower right", bbox_to_anchor=(1.0, 1.0), fontsize=7,
              frameon=False, ncol=3, borderaxespad=0.2)
    ax.set_title(f"{ticker}   {label}   1Y {chg_1y:+.1f}%",
                 loc="left", fontsize=11, fontweight="bold", pad=8)

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------- 문구

def fmt(v, suffix=""):
    return f"{v}{suffix}" if v is not None else "—"


def signed(v, suffix=""):
    if v is None:
        return "—"
    return f"{'+' if v > 0 else ''}{v}{suffix}"


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def build_market_msg():
    m = read_json(DATA_DIR / "market.json") or {}
    cr = read_json(DATA_DIR / "credit.json") or {}
    liq = read_json(DATA_DIR / "liquidity.json") or {}
    notes = read_json(DATA_DIR / "market_notes.json") or {}

    lines = [f"<b>📊 시장 요약</b>  <i>{now_kst().strftime('%m/%d %H:%M')}</i>", ""]

    for r in (m.get("indexes") or []):
        lines.append(f"· {html.escape(r.get('name',''))}: {signed(r.get('chg'), '%')}")

    vix = next((r for r in (m.get("risk") or []) if r.get("label") == "VIX"), None)
    if vix:
        lines.append(f"· VIX: {fmt(vix.get('value'))}")

    hy = (cr.get("high_yield") or {}).get("value")
    if hy is not None:
        lines.append(f"· 하이일드 스프레드: {fmt(hy, '%p')}")
    nl = (liq.get("net_liquidity") or {}).get("value")
    if nl is not None:
        lines.append(f"· 순유동성: {fmt(nl)}십억$")

    situation = (notes.get("notes") or {}).get("situation")
    if situation:
        lines += ["", "<b>오늘 총평</b>", html.escape(situation)]
        lines.append("<i>자동 생성 · 참고용</i>")

    return "\n".join(lines)


def usd_short(v):
    if v is None:
        return "—"
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}mil"
    return f"${v}"


def krw_short(v):
    if v is None:
        return ""
    if v >= 1e12:
        return f" ({v/1e12:.1f}조원)"
    if v >= 1e8:
        return f" ({v/1e8:.0f}억원)"
    return ""


def info_lines(c):
    """시총(원화)·거래대금(원화)·시총순위·기업개요 -- 카드에 있는 걸 텔레에도."""
    out = []
    parts = [f"시총 {usd_short(c.get('marketCap'))}{krw_short(c.get('marketCapKrw'))}",
             f"거래대금 {usd_short(c.get('tradingValue'))}{krw_short(c.get('tradingValueKrw'))}"]
    if c.get("capRank"):
        parts.append(f"시총순위 {c['capRank']}위")
    out.append("  <i>" + " · ".join(parts) + "</i>")
    for b in (c.get("overview") or [])[:2]:
        out.append(f"  - {esc(b)}")
    return out


def breakout_lines(cards):
    out = []
    for c in cards:
        m = c.get("meta") or {}
        out.append(f'· <b>{esc(c.get("name"))}</b> ({esc(c.get("ticker"))}) '
                   f'{signed(c.get("chg"), "%")}')
        detail = []
        if c.get("sector"):
            detail.append(esc(c["sector"]))
        if m.get("score") is not None:
            detail.append(f"점수 {m['score']}")
        if m.get("gates") is not None:
            detail.append(f"게이트 {m['gates']}")
        if m.get("drawdown") is not None:
            detail.append(f"낙폭 {m['drawdown']}%")
        if detail:
            out.append(f"  <i>{' · '.join(detail)}</i>")
        out += info_lines(c)
    return out


def deepvalue_lines(cards):
    out = []
    for c in cards:
        m = c.get("meta") or {}
        trig = " 🔥트리거" if m.get("triggered") else ""
        out.append(f'· <b>{esc(c.get("name"))}</b> ({esc(c.get("ticker"))}) '
                   f'{signed(c.get("chg"), "%")}{trig}')
        detail = []
        if c.get("sector"):
            detail.append(esc(c["sector"]))
        if m.get("score") is not None:
            detail.append(f"점수 {m['score']}")
        if m.get("fscore") is not None:
            detail.append(f"F {m['fscore']}")
        if m.get("drawdown") is not None:
            detail.append(f"낙폭 {m['drawdown']}%")
        if detail:
            out.append(f"  <i>{' · '.join(detail)}</i>")
        if m.get("why"):
            out.append(f"  <i>왜 싼가: {esc(m['why'])}</i>")
        out += info_lines(c)
    return out


def extreme_lines(cards):
    out = []
    for c in cards:
        out.append(f'· <b>{esc(c.get("name"))}</b> ({esc(c.get("ticker"))}) '
                   f'${esc(c.get("price"))} {signed(c.get("chg"), "%")}')
        if c.get("sector"):
            out.append(f"  <i>{esc(c['sector'])}</i>")
        out += info_lines(c)
    return out


def low_summary(highs, lows, top=3):
    """신저가를 업종별로 세서 두세 줄로 요약한다.
    같은 업종의 신고가 수를 옆에 붙여, 업종 전체가 무너지는 건지
    그 업종 안에서도 갈리는 건지 구분할 수 있게 한다."""
    if not lows:
        return ""

    def count(cards):
        out = {}
        for c in cards:
            k = (c.get("sector") or "").strip() or "기타"
            out[k] = out.get(k, 0) + 1
        return out

    lo, hi = count(lows), count(highs)
    ranked = sorted(lo.items(), key=lambda kv: -kv[1])
    total = len(lows)

    lines = ["<b>📉 신저가 업종</b>"]
    for name, n in ranked[:top]:
        share = round(n / total * 100)
        lines.append(f"· {esc(name)} {n}개 ({share}%) · 같은 업종 신고가 {hi.get(name, 0)}개")

    name, n = ranked[0]
    share = n / total
    h = hi.get(name, 0)
    if n < 3 or (len(ranked) > 1 and ranked[1][1] == n):
        note = "뚜렷하게 몰린 업종 없음 -- 개별 종목 이슈에 가까움"
    elif share >= 0.3 and h == 0:
        note = f"{name}에 몰려 있고 신고가는 없음 -- 업종 전체가 밀리는 모양"
    elif share >= 0.3:
        note = f"{name}에 몰려 있지만 신고가도 {h}개 -- 업종 안에서 종목별로 갈림"
    else:
        note = "여러 업종에 흩어져 있음 -- 특정 업종 문제라기보다 개별 종목 이슈"
    lines.append(f"<i>{esc(note)}</i>")
    return "\n".join(lines)


DASH_KR = "https://panzeeeeee.github.io/researchdashboard/kr.html"


def build_kr_message():
    """한국 시장 요약 -- 텍스트만(차트 없음). 데이터가 하나도 없으면 None."""
    mk = read_json(DATA_DIR / "kr_market.json") or {}
    bn = read_json(DATA_DIR / "banner.json") or {}
    news = read_json(DATA_DIR / "kr_news.json") or {}
    dis = read_json(DATA_DIR / "kr_disclosures.json") or {}
    bo = read_json(DATA_DIR / "kr_breakout.json") or {}
    dv = read_json(DATA_DIR / "kr_deepvalue.json") or {}
    picks = read_json(DATA_DIR / "kr_picks.json") or {}
    pol = read_json(DATA_DIR / "kr_policy.json") or {}
    cal = read_json(DATA_DIR / "kr_calendar.json") or {}
    if not mk.get("indexes"):
        return None

    def num(v, d=2):
        return f"{v:,.{d}f}"
    lines = [f"<b>🇰🇷 한국 시장 요약</b>  <i>{now_kst().strftime('%m/%d %H:%M')} · 마감 기준</i>", ""]
    for i in mk["indexes"]:
        lines.append(f"· {esc(i['label'])}: {num(i['value'])} ({signed(i['chg'], '%')})")
    rates = {r["key"]: r for r in mk.get("rates", [])}
    parts = []
    for k, nm in (("kr3y", "국고채 3년"), ("kr10y", "국고채 10년")):
        if k in rates:
            parts.append(f"{nm} {rates[k]['value']:.3f}% ({signed(rates[k]['chg'], 'bp')})")
    fx = next((x for x in bn.get("items", []) if x.get("key") == "usdkrw"), None)
    if fx:
        parts.append(f"원/달러 {num(fx['value'], 1)} ({signed(fx['chg'], '%')})")
    if parts:
        lines.append("· " + " · ".join(parts))
    secs = [s for s in mk.get("sectors", []) if s.get("d1") is not None]
    if len(secs) >= 4:
        secs.sort(key=lambda s: -s["d1"])
        lines.append("· 강한 업종 " + ", ".join(f"{esc(s['label'])} {signed(s['d1'], '%')}" for s in secs[:3])
                     + " / 약한 업종 " + ", ".join(f"{esc(s['label'])} {signed(s['d1'], '%')}" for s in secs[-3:][::-1]))

    why = ((news.get("event_notes") or {}).get("why") or "").strip()
    if why:
        lines += ["", "<b>총평</b>", esc(why[:380]), "<i>자동 생성 · 참고용</i>"]

    items = dis.get("items") or []
    tur = [x for x in items if x.get("category") == "잠정실적" and x.get("numbers")]
    if tur:
        last = max(x["date"] for x in tur)
        today = [x for x in tur if x["date"] == last]
        up = [x["corp"] for x in today if x["numbers"].get("op_tag") == "흑자전환"]
        dn = [x["corp"] for x in today if x["numbers"].get("op_tag") == "적자전환"]
        lines += ["", f"<b>📋 잠정실적</b> {last[5:].replace('-', '/')} 공시 {len(today)}건 (숫자 확인 {len(tur)}건 중)"]
        if up:
            lines.append("· 영업이익 흑자전환: " + ", ".join(esc(n) for n in up[:5]))
        if dn:
            lines.append("· 영업이익 적자전환: " + ", ".join(esc(n) for n in dn[:5]))
    risk = [x for x in items if x.get("category") == "리스크" and x["date"] >= (now_kst() - timedelta(days=1)).strftime("%Y-%m-%d")]
    if risk:
        lines.append("· ⚠ 리스크 공시: " + ", ".join(f"{esc(x['corp'])}({esc(x['title'][:14])})" for x in risk[:4]))

    ex = bo.get("extremes") or {}
    if ex or dv.get("funnel"):
        f = {a[0]: a[1] for a in dv.get("funnel", [])}
        cand = f.get("생존·희석 통과 + F-Score 6+ (후보)")
        lines += ["", "<b>🔎 스크리너</b>"]
        if ex:
            lines.append(f"· 52주 신고가 {ex.get('n_high', 0)} · 신저가 {ex.get('n_low', 0)}"
                         + (f" · 긴 조정 후 신고가 근접후보 {len(bo.get('breakout') or [])}" if bo.get("breakout") is not None else ""))
        if cand is not None:
            trig = sum(1 for x in dv.get("candidates", []) if x.get("trigger_count"))
            lines.append(f"· 딥밸류 후보 {cand} (오늘 트리거 {trig})")
    pk = picks.get("picks") or []
    if pk:
        lines += ["", "<b>⭐ 오늘의 종목</b>"]
        for p in pk[:10]:
            lines.append(f"· {esc(p['name'])} ({esc(', '.join(p['tags']))}) {signed(p.get('chg'), '%')}")
    dg = pol.get("digest") or []
    if dg:
        lines += ["", "<b>🏛 정책 이슈</b>"] + [f"· {esc(x)}" for x in dg[:3]]
    soon = [e for e in cal.get("events", []) if e["date"] <= (now_kst() + timedelta(days=7)).strftime("%Y-%m-%d")
            and e["date"] >= now_kst().strftime("%Y-%m-%d") and e.get("kind") != "휴장"]
    hol = [e for e in cal.get("events", []) if e.get("kind") == "휴장" and e["date"] <= (now_kst() + timedelta(days=7)).strftime("%Y-%m-%d")
           and e["date"] >= now_kst().strftime("%Y-%m-%d")]
    if soon or hol:
        lines += ["", "<b>📅 이번 주 일정</b>"] + [f"· {esc(e['date'][5:].replace('-', '/'))} {esc(e['label'])}" for e in (soon + hol)[:6]]
    lines += ["", f'<a href="{DASH_KR}">한국 대시보드 열기</a>']
    return "\n".join(lines)[:4000]


EARNINGS_TOP = 8           # 텔레그램에 종목별로 적는 최대 건수(큰 회사 순)
EARNINGS_STALE_HOURS = 30  # earnings.json 이 이보다 오래됐으면 오래된 소식이라 안 보낸다


def build_earnings_message():
    """밤사이 나온 미국 실적을 한 메시지로. 없거나 자료가 오래됐으면 None.
    '밤사이' = earnings.json 에서 가장 최근 발표일(미국 동부 기준) 하루치."""
    d = read_json(DATA_DIR / "earnings.json") or {}
    items = d.get("items") or []
    if not items:
        return None
    try:
        age = now_kst() - datetime.fromisoformat(d["updated_at"])
        if age > timedelta(hours=EARNINGS_STALE_HOURS):
            print(f"  earnings.json 이 {age} 전 자료라 실적 블록을 건너뜁니다.")
            return None
    except Exception:
        return None

    day = max(x.get("date_et", "") for x in items)
    rows = [x for x in items if x.get("date_et") == day]
    if not rows:
        return None

    done = [x for x in rows if x.get("react_status") == "확정"]
    up = sum(1 for x in done if x["rel_pct"] > 0)
    guide = [(x.get("numbers") or {}).get("guidance") for x in rows]
    yoys = sorted(v for v in ((x.get("numbers") or {}).get("revenue_yoy") for x in rows)
                  if v is not None)
    med = yoys[(len(yoys) - 1) // 2] if yoys else None

    lines = [f"<b>🌙 밤사이 미국 실적</b> · {esc(day[5:].replace('-', '/'))} 발표 {len(rows)}건", "",
             "<b>📊 한눈에</b>"]
    if done:
        lines.append(f" 🏁 시장(SPY)보다 오른 곳 {up}/{len(done)} (반응 확정분)")
    if len(rows) > len(done):
        lines.append(f" ⏳ 반응 대기 {len(rows) - len(done)}건 (장후 발표는 다음 거래일 종가 이후)")
    lines.append(f" 🧭 가이던스  🔼 {guide.count('상향')} · 🔽 {guide.count('하향')}"
                 f" · 🆕 {guide.count('첫 제시')}")
    if med is not None:
        lines.append(f" 📈 매출 YoY 중앙값 <b>{signed(med, '%')}</b> (숫자 있는 {len(yoys)}곳)")

    # 업종별 한 줄: 발표가 3건 이상인 업종만, 많은 순 상위 3개
    by = {}
    for x in rows:
        by.setdefault(x.get("sector") or "기타", []).append(x)
    top = sorted(((k, v) for k, v in by.items() if len(v) >= 3 and k != "기타"),
                 key=lambda kv: -len(kv[1]))[:3]
    if top:
        lines += ["", "<b>🏭 업종별</b>"]
        for k, v in top:
            ys = sorted(n["revenue_yoy"] for n in (x.get("numbers") or {} for x in v)
                        if n.get("revenue_yoy") is not None)
            y = f" · 매출 YoY 중앙값 {signed(ys[(len(ys) - 1) // 2], '%')}" if ys else ""
            lines.append(f" · <b>{esc(k)}</b> {len(v)}건{y}")

    # 큰 회사부터 종목별 (SEC 티커 목록 순서 ≈ 시가총액 순), 숫자가 있는 건만
    try:
        from fetch_edgar import load_cik_map
        order = {t: i for i, t in enumerate(load_cik_map())}
    except Exception:
        order = {}      # 못 받으면 발표 순서 그대로
    with_num = sorted((x for x in rows if x.get("numbers")),
                      key=lambda x: order.get(x["ticker"], 10 ** 6))[:EARNINGS_TOP]
    if with_num:
        lines += ["", "<b>⭐ 주요 종목</b>", ""]
        gicon = {"상향": "🔼", "하향": "🔽", "첫 제시": "🆕"}
        for x in with_num:
            n = x["numbers"]
            yoy = n.get("revenue_yoy")
            dot = "⚪" if yoy is None else "🟢" if yoy > 0 else "🔴" if yoy < 0 else "⚪"
            bits = []
            if yoy is not None:
                bits.append(f"매출 <b>{signed(yoy, '%')}</b>")
            if n.get("guidance") in gicon:
                bits.append(f"{gicon[n['guidance']]} 가이던스 {n['guidance']}")
            if x.get("react_status") == "확정":
                bits.append(f"시장 대비 {signed(x['rel_pct'], '%p')}")
            lines.append(f"{dot} <code>{esc(x['ticker'])}</code> <b>{esc(x['company'][:24])}</b>")
            if bits:
                lines.append("   " + " · ".join(bits))
            if n.get("summary"):
                lines.append(f"<blockquote>{esc(n['summary'])}</blockquote>")
            lines.append("")        # 종목 사이를 한 줄 띄워 읽기 편하게
        lines.pop()
    lines += ["", "<i>🤖 보도자료를 AI가 자동 추출한 값 · 틀릴 수 있으니 대시보드 원문 링크로 확인</i>"]
    return "\n".join(lines)


# 섹션별: (차트 제목 영문, 차트 색)
SECTIONS = {
    "breakout":  ("NEW HIGH AFTER BASE", "#16a34a"),
    "deepvalue": ("DEEP VALUE",          "#2563eb"),
    "high":      ("52W HIGH",            "#059669"),
    "low":       ("52W LOW",             "#dc2626"),
}


def build_screener_messages():
    """(card or None, section or None, caption) 목록.
    맨 앞은 건수 요약 1개, 그다음 종목마다 1개씩."""
    bo = (read_json(DATA_DIR / "breakout_cards.json") or {}).get("cards") or []
    dv = (read_json(DATA_DIR / "deepvalue_cards.json") or {}).get("cards") or []
    ex = read_json(DATA_DIR / "extremes.json") or {}
    highs = ex.get("high") or []
    lows = ex.get("low") or []

    head = (f"<b>🔎 스크리너 결과</b>  <i>{now_kst().strftime('%m/%d')}</i>\n"
            f"· 긴 조정 후 신고가 {len(bo)}건\n"
            f"· 딥밸류 {len(dv)}건\n"
            f"· 52주 신고가 {len(highs)}건 · 신저가 {len(lows)}건")
    low_text = low_summary(highs, lows)
    if low_text:
        head += "\n\n" + low_text
    messages = [(None, None, head)]

    for c in bo:
        messages.append((c, "breakout", "🟢 <b>[신고가]</b>\n" + "\n".join(breakout_lines([c]))))
    for c in dv:
        messages.append((c, "deepvalue", "🔵 <b>[딥밸류]</b>\n" + "\n".join(deepvalue_lines([c]))))
    for c in highs:
        messages.append((c, "high", "🔺 <b>[52주 신고가]</b>\n" + "\n".join(extreme_lines([c]))))
    # 신저가는 종목별로 보내지 않는다 -- 너무 많아서. 위 요약에 업종별 개수만.
    return messages


# ---------------------------------------------------------------- 실행

def is_morning_run():
    if env("DIGEST_FORCE"):
        return True
    return now_kst().hour < MORNING_CUTOFF_HOUR


def main():
    if not is_morning_run():
        print(f"저녁 실행({now_kst().strftime('%H:%M')} KST) -- 요약 발송은 아침에만. 건너뜁니다.")
        return

    token = env("TELEGRAM_DIGEST_BOT_TOKEN") or env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_DIGEST_CHAT_ID") or env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("텔레그램 설정이 없어 발송을 건너뜁니다.")
        return

    ok, charts = 0, 0
    if send(token, chat_id, build_market_msg()):
        ok += 1

    try:
        earn = build_earnings_message()
    except Exception as e:      # 실적 블록이 잘못돼도 나머지 발송은 계속한다
        print(f"  실적 블록 실패: {e}", file=sys.stderr)
        earn = None
    if earn and send(token, chat_id, earn):
        ok += 1

    try:                      # 한국 시장 요약 (텍스트만). 실패해도 나머지 발송은 계속한다
        kr = build_kr_message()
    except Exception as e:      # noqa: BLE001
        print(f"  한국 요약 실패: {type(e).__name__}: {e}", file=sys.stderr)
        kr = None
    if kr and send(token, chat_id, kr):
        ok += 1

    messages = build_screener_messages()
    for i, (card, section, caption) in enumerate(messages):
        png = None
        if card is not None:
            label, color = SECTIONS[section]
            try:
                png = draw_chart(card.get("ticker") or "", label, card.get("chart"), color)
            except Exception as e:
                print(f"  {card.get('ticker')} 차트 실패: {e}", file=sys.stderr)
        if png:
            done = send_photo_bytes(token, chat_id, png, caption)
            charts += 1
        else:
            done = send(token, chat_id, caption)
        if done:
            ok += 1
        if i < len(messages) - 1:
            time.sleep(3)
    print(f"발송 {ok}건 (차트 {charts}장)")


if __name__ == "__main__":
    main()
