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

import requests

from common import DATA_DIR, env, now_kst, read_json

API = "https://api.telegram.org/bot{token}/sendMessage"


def send(token, chat_id, text):
    r = requests.post(
        API.format(token=token), timeout=20,
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
    )
    if not r.ok:
        print(f"발송 실패: {r.status_code} {r.text[:200]}", file=sys.stderr)
    return r.ok


def fmt(v, suffix=""):
    return f"{v}{suffix}" if v is not None else "—"


def signed(v, suffix=""):
    if v is None:
        return "—"
    return f"{'+' if v > 0 else ''}{v}{suffix}"


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


def build_screener_msg():
    tb = read_json(DATA_DIR / "tables.json") or {}
    ex = read_json(DATA_DIR / "extremes.json") or {}

    breakout = tb.get("breakout") or []
    deepvalue = tb.get("deepvalue") or []
    highs = ex.get("high") or []
    lows = ex.get("low") or []

    lines = [f"<b>🔎 스크리너 결과</b>  <i>{now_kst().strftime('%m/%d')}</i>", ""]

    def top_tickers(rows, key="code", n=8):
        out = [str(r.get(key) or r.get("ticker") or "") for r in rows[:n]]
        out = [x for x in out if x]
        tail = " 외" if len(rows) > n else ""
        return (", ".join(out) + tail) if out else "없음"

    lines.append(f"<b>긴 조정 후 신고가</b> {len(breakout)}건")
    lines.append(top_tickers(breakout))
    lines.append("")
    lines.append(f"<b>딥밸류</b> {len(deepvalue)}건")
    lines.append(top_tickers(deepvalue))
    lines.append("")
    lines.append(f"<b>52주 신고가</b> {len(highs)}건 · <b>신저가</b> {len(lows)}건")
    lines.append("신고가: " + top_tickers(highs, key="ticker"))
    lines.append("신저가: " + top_tickers(lows, key="ticker"))

    return "\n".join(lines)


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
    if send(token, chat_id, build_screener_msg()):
        ok += 1
    print(f"발송 {ok}건")


if __name__ == "__main__":
    main()
