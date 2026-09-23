"""시장 요약 + 스크리너 결과를 텔레그램으로 보낸다.

기존 push_telegram.py(새 뉴스만 보냄)와 목적이 다르다 -- 이건 매 실행마다
그날 스냅샷을 한 방에 보낸다. 중복 방지 기록(sent.json) 없이 그냥 현재 상태를
발송한다. send() 방식과 토큰·챗ID는 push_telegram.py 와 동일하게 쓴다.

market.json / credit.json / liquidity.json / market_notes.json / tables.json /
extremes.json 을 읽는다 -- 그 앞 단계들이 다 끝난 뒤(=push_telegram 근처)에
돌아야 한다.
"""

import html
import sys
import time

import requests

from common import DATA_DIR, env, now_kst, read_json

API = "https://api.telegram.org/bot{token}/sendMessage"
API_PHOTO = "https://api.telegram.org/bot{token}/sendPhoto"


def finviz_chart(ticker):
    """Finviz 일봉 차트 이미지 URL. 티커만 넣으면 됨(키 불필요, 미국 종목)."""
    return f"https://charts.finviz.com/chart.ashx?t={ticker}&ty=c&ta=1&p=d"


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
            # 응답의 retry_after 만큼 대기 후 재시도
            try:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
            except Exception:
                wait = 5
            print(f"  429 -- {wait}초 대기 후 재시도", file=sys.stderr)
            time.sleep(wait + 1)
            continue
        print(f"발송 실패: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return False
    return False


def send_photo(token, chat_id, photo_url, caption, tries=4):
    """차트 이미지(URL)를 캡션과 함께 보낸다. 429면 대기 후 재시도.
    캡션은 1024자 제한이 있어 넘으면 잘라 뒤에 텍스트로 따로 보낸다."""
    cap = caption if len(caption) <= 1024 else caption[:1000] + "…"
    for attempt in range(tries):
        r = requests.post(
            API_PHOTO.format(token=token), timeout=30,
            json={"chat_id": chat_id, "photo": photo_url, "caption": cap,
                  "parse_mode": "HTML"},
        )
        if r.ok:
            # 캡션이 잘렸으면 나머지를 텍스트로 이어 보냄
            if len(caption) > 1024:
                send(token, chat_id, caption[1000:])
            return True
        if r.status_code == 429:
            try:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
            except Exception:
                wait = 5
            print(f"  사진 429 -- {wait}초 대기 후 재시도", file=sys.stderr)
            time.sleep(wait + 1)
            continue
        # 사진 실패 시 텍스트라도 보낸다
        print(f"사진 실패: {r.status_code} {r.text[:150]} -- 텍스트로 대체", file=sys.stderr)
        return send(token, chat_id, caption)
    return send(token, chat_id, caption)



def fmt(v, suffix=""):
    return f"{v}{suffix}" if v is not None else "—"


def build_market_msg():
    m = read_json(DATA_DIR / "market.json") or {}
    cr = read_json(DATA_DIR / "credit.json") or {}
    liq = read_json(DATA_DIR / "liquidity.json") or {}
    notes = read_json(DATA_DIR / "market_notes.json") or {}

    lines = [f"<b>📊 시장 요약</b>  <i>{now_kst().strftime('%m/%d %H:%M')}</i>", ""]

    # 지수
    for r in (m.get("indexes") or []):
        lines.append(f"· {html.escape(r.get('name',''))}: {signed(r.get('chg'), '%')}")

    # VIX
    vix = next((r for r in (m.get("risk") or []) if r.get("label") == "VIX"), None)
    if vix:
        lines.append(f"· VIX: {fmt(vix.get('value'))}")

    # 크레딧 / 순유동성
    hy = (cr.get("high_yield") or {}).get("value")
    if hy is not None:
        lines.append(f"· 하이일드 스프레드: {fmt(hy, '%p')}")
    nl = (liq.get("net_liquidity") or {}).get("value")
    if nl is not None:
        lines.append(f"· 순유동성: {fmt(nl)}십억$")

    # 오늘 마켓 총평
    situation = (notes.get("notes") or {}).get("situation")
    if situation:
        lines += ["", f"<b>오늘 총평</b>", html.escape(situation)]
        lines.append("<i>자동 생성 · 참고용</i>")

    return "\n".join(lines)


def esc(x):
    return html.escape(str(x)) if x is not None else ""


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
    mc = c.get("marketCap")
    tv = c.get("tradingValue")
    parts = [f"시총 {usd_short(mc)}{krw_short(c.get('marketCapKrw'))}",
             f"거래대금 {usd_short(tv)}{krw_short(c.get('tradingValueKrw'))}"]
    if c.get("capRank"):
        parts.append(f"시총순위 {c['capRank']}위")
    out.append("  <i>" + " · ".join(parts) + "</i>")
    for b in (c.get("overview") or [])[:2]:
        out.append(f"  - {esc(b)}")
    return out


def signed(v, suffix=""):
    if v is None:
        return "—"
    return f"{'+' if v > 0 else ''}{v}{suffix}"


def breakout_lines(cards):
    """긴 조정 후 신고가 -- 종목마다 이름·티커·등락률·섹터·점수·게이트."""
    out = []
    for c in cards:
        m = c.get("meta") or {}
        out.append(
            f'· <b>{esc(c.get("name"))}</b> ({esc(c.get("ticker"))}) '
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
    """딥밸류 -- 이름·티커·등락률·섹터·점수·F스코어·왜 싼가."""
    out = []
    for c in cards:
        m = c.get("meta") or {}
        trig = " 🔥트리거" if m.get("triggered") else ""
        out.append(
            f'· <b>{esc(c.get("name"))}</b> ({esc(c.get("ticker"))}) '
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
    """신고가·신저가 -- 이름·티커·가격·등락률·섹터 + 시총·순위·기업개요."""
    out = []
    for c in cards:
        out.append(
            f'· <b>{esc(c.get("name"))}</b> ({esc(c.get("ticker"))}) '
            f'${esc(c.get("price"))} {signed(c.get("chg"), "%")}')
        if c.get("sector"):
            out.append(f"  <i>{esc(c['sector'])}</i>")
        out += info_lines(c)
    return out


def build_screener_messages():
    """스크리너 결과를 '메시지 목록'으로 만든다.
    맨 앞은 섹션별 건수 요약 1개, 그다음 종목마다 개별 메시지 1개씩.
    main 에서 종목당 하나씩 간격을 두고 보낸다."""
    bo = (read_json(DATA_DIR / "breakout_cards.json") or {}).get("cards") or []
    dv = (read_json(DATA_DIR / "deepvalue_cards.json") or {}).get("cards") or []
    ex = read_json(DATA_DIR / "extremes.json") or {}
    highs = ex.get("high") or []
    lows = ex.get("low") or []

    # (ticker or None, caption) 목록. ticker 가 있으면 차트 사진으로 보낸다.
    messages = []
    # 1) 요약 헤더 (사진 없음)
    messages.append((None,
        f"<b>🔎 스크리너 결과</b>  <i>{now_kst().strftime('%m/%d')}</i>\n"
        f"· 긴 조정 후 신고가 {len(bo)}건\n"
        f"· 딥밸류 {len(dv)}건\n"
        f"· 52주 신고가 {len(highs)}건 · 신저가 {len(lows)}건"))

    # 2) 종목마다 (차트 사진 + 상세 캡션)
    for c in bo:
        messages.append((c.get("ticker"), "🟢 <b>[신고가]</b>\n" + "\n".join(breakout_lines([c]))))
    for c in dv:
        messages.append((c.get("ticker"), "🔵 <b>[딥밸류]</b>\n" + "\n".join(deepvalue_lines([c]))))
    for c in highs:
        messages.append((c.get("ticker"), "🔺 <b>[52주 신고가]</b>\n" + "\n".join(extreme_lines([c]))))
    for c in lows:
        messages.append((c.get("ticker"), "🔻 <b>[52주 신저가]</b>\n" + "\n".join(extreme_lines([c]))))
    return messages


def main():
    # 요약은 전용 봇+전용 채널로 보낸다. 전용 값이 있으면 그걸,
    # 없으면 기존 봇/채널로 폴백한다.
    token = env("TELEGRAM_DIGEST_BOT_TOKEN") or env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_DIGEST_CHAT_ID") or env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("텔레그램 설정이 없어 발송을 건너뜁니다.")
        return

    ok = 0
    if send(token, chat_id, build_market_msg()):
        ok += 1

    # 스크리너는 종목당 하나씩. 차트 사진 + 상세 캡션. 텔레 제한 피해 3초 간격.
    messages = build_screener_messages()
    for i, (ticker, caption) in enumerate(messages):
        if ticker:
            done = send_photo(token, chat_id, finviz_chart(ticker), caption)
        else:
            done = send(token, chat_id, caption)
        if done:
            ok += 1
        if i < len(messages) - 1:
            time.sleep(3)
    print(f"발송 {ok}건")


if __name__ == "__main__":
    main()
