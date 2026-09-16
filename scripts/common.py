"""공용 유틸 — 설정 읽기, HTTP, 중복 제거, JSON 입출력."""

import json
import os
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import sys

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "docs" / "data"
KST = timezone(timedelta(hours=9))

UA = "Mozilla/5.0 (compatible; research-dashboard/1.0)"


def now_kst():
    return datetime.now(KST)


def load_config(name):
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, payload):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return p


def get(url, **kwargs):
    kwargs.setdefault("timeout", 15)
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    return requests.get(url, headers=headers, **kwargs)


TAG_RE = re.compile(r"<[^>]+>")
BRACKET_RE = re.compile(r"[\[\(【][^\]\)】]{0,20}[\]\)】]")
NONWORD_RE = re.compile(r"[^0-9a-z가-힣]+")


def clean_text(s):
    """HTML 태그와 엔티티를 걷어낸 평문."""
    if not s:
        return ""
    s = TAG_RE.sub("", s)
    for a, b in (("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&apos;", "'"), ("&#39;", "'"),
                 ("&nbsp;", " "), ("&middot;", "·"), ("&hellip;", "…")):
        s = s.replace(a, b)
    s = re.sub(r"\s+", " ", s)
    return unicodedata.normalize("NFKC", s).strip()


def title_key(title):
    """비교용으로 정규화한 제목 — 말머리와 기호를 제거."""
    s = BRACKET_RE.sub(" ", clean_text(title).lower())
    return NONWORD_RE.sub("", s)


def echoes_title(summary, title, threshold=0.7):
    """요약이 제목을 되풀이하고 있는지 — 구글 RSS 폴백에서 자주 발생한다."""
    a, b = title_key(summary), title_key(title)
    if not a or not b:
        return True
    if a.startswith(b[:24]) or b.startswith(a[:24]):
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold


def dedupe(items, threshold=0.82):
    """제목이 비슷한 기사를 하나로 접는다.

    가장 먼저 들어온 항목을 대표로 남기고, 접힌 개수를 dup_count 에 기록한다.
    같은 보도자료를 여러 매체가 받아쓰는 경우를 잡기 위한 것이라
    threshold 를 너무 낮추면 서로 다른 기사가 합쳐지니 주의.
    """
    kept = []
    for item in items:
        key = title_key(item.get("title", ""))
        if not key:
            continue
        match = None
        for k in kept:
            if key == k["_key"] or SequenceMatcher(None, key, k["_key"]).ratio() >= threshold:
                match = k
                break
        if match:
            match["dup_count"] = match.get("dup_count", 0) + 1
            continue
        item["_key"] = key
        item["dup_count"] = 0
        kept.append(item)
    for k in kept:
        k.pop("_key", None)
    return kept


def env(name, default=None):
    v = os.environ.get(name, "")
    return v.strip() or default


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# 구글이 모델을 자주 갈아치우므로 이름을 박아두지 않는다.
# 계정에서 실제로 쓸 수 있는 목록을 물어보고 그중에서 고른다.
_STATE = {"model": None, "listed": False}


def _list_models(api_key):
    """generateContent 를 지원하는 모델 이름들을 새 것부터 정렬해 돌려준다."""
    try:
        r = requests.get(f"{GEMINI_BASE}/models", params={"key": api_key},
                         timeout=30)
        r.raise_for_status()
        rows = r.json().get("models", [])
    except Exception as e:
        print(f"  Gemini 모델 목록 조회 실패: {e}", file=sys.stderr)
        return []

    names = []
    for m in rows:
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        name = str(m.get("name", "")).split("/")[-1]
        if not name:
            continue
        names.append(name)

    def score(n):
        low = n.lower()
        s = 0
        if "flash" in low:
            s += 100          # 무료 등급은 사실상 flash 계열
        if "lite" in low:
            s += 1            # 같은 세대면 한도가 넉넉한 lite 를 먼저
        if any(w in low for w in ("preview", "exp", "thinking", "tts",
                                  "audio", "image", "embedding")):
            s -= 60           # 실험판과 특수 목적 모델은 뒤로
        nums = re.findall(r"\d+(?:\.\d+)?", n)
        if nums:
            try:
                s += min(float(nums[0]), 20) * 5   # 최신 세대를 확실히 앞으로
            except ValueError:
                pass
        return -s

    names.sort(key=score)
    return names


def ask_gemini(prompt, api_key, timeout=60):
    """살아 있는 모델을 찾아 한 번 물어본다. 실패하면 None."""
    if not api_key:
        return None

    order = []
    if _STATE["model"]:
        order.append(_STATE["model"])
    if not _STATE["listed"]:
        _STATE["listed"] = True
        _STATE["available"] = _list_models(api_key)
    order += [m for m in _STATE.get("available", []) if m != _STATE["model"]]

    if not order:
        print("  Gemini: 쓸 수 있는 모델이 없습니다.", file=sys.stderr)
        return None

    last = None
    for model in order[:5]:
        try:
            r = requests.post(f"{GEMINI_BASE}/models/{model}:generateContent",
                              params={"key": api_key}, timeout=timeout,
                              json={"contents": [{"parts": [{"text": prompt}]}]})
            if r.status_code in (404, 400):
                last = f"{model}: {r.status_code}"
                continue
            r.raise_for_status()
            if _STATE["model"] != model:
                print(f"  Gemini 모델: {model}")
            _STATE["model"] = model
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            last = f"{model}: {e}"
            continue
    print(f"  Gemini 실패 (마지막 시도 {last})", file=sys.stderr)
    return None
