"""Метрики токена с экрана: market cap, ликвидность, холдеры.

Нужны, чтобы опознать токен без адреса: тикер у мемкоинов не уникален (десятки
копий), а пара «тикер + капитализация» почти всегда однозначна — резолвер
сверяет экранный market cap с текущим у каждого одноимённого токена.

Форматы на реальных экранах (pump.fun, Photon, BullX, GMGN, Dexscreener, оверлеи):
    "MC $42.1K Liq $18K Holders 312"        — всё в одном блоке
    "Market Cap: $1.3M"                      — подпись и значение рядом
    "$1.3M" + отдельный блок "MARKET CAP"    — подпись под/над значением
Значения — USD с суффиксами K / M / B.
"""

from __future__ import annotations

import re

_NUM = r"\$?\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?([KkMmBb])?"
_VALUE_RE = re.compile(_NUM)
_VALUE_ONLY_RE = re.compile(r"^\s*" + _NUM + r"\s*$")

# Подписи → метрика. Порядок важен: длинные раньше коротких.
_LABELS = [
    ("market_cap_usd", r"market\s*cap(?:italization)?|mkt\.?\s*cap|m\.?\s?cap|mcap|mc"),
    ("liquidity_usd", r"liquidity|liq"),
    ("holders", r"holders"),
]
# Подписи, рядом с которыми число — НЕ текущая капитализация.
_NOT_CURRENT = re.compile(r"ath|all[\s-]*time|high|peak|max|24h\s*vol|volume|vol\b|change|%", re.I)

_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9}


def parse_value(num: str, suffix: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * _SUFFIX.get((suffix or "").lower(), 1.0)


def _label_re(pattern: str) -> re.Pattern:
    return re.compile(r"(?<![a-z])(?:" + pattern + r")(?![a-z])", re.I)


_LABEL_RES = [(k, _label_re(p)) for k, p in _LABELS]


def _inline(text: str) -> dict[str, float]:
    """«MC $42.1K Liq $18K» / «Market Cap: $1.3M» / «$1.3M Market Cap» в одном блоке."""
    out: dict[str, float] = {}
    for key, lre in _LABEL_RES:
        for m in lre.finditer(text):
            after = text[m.end():]
            v = re.match(r"\s*[:\-–]?\s*" + _NUM, after)
            if v and (v.group(2) or "$" in after[:v.end()] or key == "holders" or "," in v.group(1)):
                out.setdefault(key, parse_value(v.group(1), v.group(2)))
                continue
            before = text[:m.start()]
            v = re.search(_NUM + r"\s*$", before)
            if v and (v.group(2) or "$" in v.group(0)):
                out.setdefault(key, parse_value(v.group(1), v.group(2)))
    return out


def _center(b):
    x1, y1, x2, y2 = b["bbox"]
    return (x1 + x2) / 2, (y1 + y2) / 2


def _nearby(value_block: dict, label_block: dict) -> float | None:
    """Расстояние, если подпись стоит под/над значением или слева на той же строке."""
    vx1, vy1, vx2, vy2 = value_block["bbox"]
    lx1, ly1, lx2, ly2 = label_block["bbox"]
    vh = max(1, vy2 - vy1)
    # под или над: горизонтальное перекрытие, вертикальный зазор ≤ 1.5 высоты значения
    overlap = min(vx2, lx2) - max(vx1, lx1)
    if overlap > 0:
        gap = ly1 - vy2 if ly1 >= vy2 else vy1 - ly2
        if -vh * 0.3 <= gap <= vh * 1.5:
            return abs(gap)
    # слева на той же строке
    (vcx, vcy), (lcx, lcy) = _center(value_block), _center(label_block)
    if abs(vcy - lcy) <= vh * 0.6 and lx2 <= vx1 and vx1 - lx2 <= vh * 4:
        return vx1 - lx2
    return None


def extract_metrics(blocks: list[dict]) -> dict[str, float]:
    """OCR-блоки → {"market_cap_usd": …, "liquidity_usd": …, "holders": …} (что нашлось)."""
    out: dict[str, float] = {}
    for b in blocks:
        text = b.get("text", "")
        if _NOT_CURRENT.search(text) and not any(lre.search(text) for _, lre in _LABEL_RES[:1]):
            continue
        for k, v in _inline(text).items():
            out.setdefault(k, v)

    labels = []
    for b in blocks:
        t = b.get("text", "").strip()
        for key, lre in _LABEL_RES:
            m = lre.fullmatch(t.rstrip(":").strip())
            if m:
                labels.append((key, b))
                break
    for b in blocks:
        m = _VALUE_ONLY_RE.match(b.get("text", ""))
        if not m or not (m.group(2) or "$" in b["text"] or "," in m.group(1)):
            continue
        best = None
        for key, lb in labels:
            if key in out:
                continue
            d = _nearby(b, lb)
            if d is not None and (best is None or d < best[0]):
                best = (d, key)
        if best:
            out[best[1]] = parse_value(m.group(1), m.group(2))
    if "holders" in out:
        out["holders"] = float(int(out["holders"]))
    return out


_TITLE_RE = re.compile(r"^\$?[A-Za-z][A-Za-z0-9]{1,14}$")
_TITLE_STOP = {"live", "solana", "sol", "buy", "sell", "chart", "trending", "market", "cap", "holders",
               "liquidity", "volume", "new", "pump", "photon", "bullx", "gmgn", "dexscreener", "axiom"}


def title_candidate(blocks: list[dict], frame_h: int) -> str | None:
    """Самый крупный однословный текст в верхней трети кадра — обычно название/тикер
    на странице токена ("JIMOTHY"). Используется, только если `$TICKER` не найден;
    ошибку отсекает сверка капитализации в резолвере."""
    best = None
    for b in blocks:
        t = b.get("text", "").strip()
        x1, y1, x2, y2 = b["bbox"]
        if y2 > frame_h / 3 or not _TITLE_RE.match(t) or t.lstrip("$").lower() in _TITLE_STOP:
            continue
        h = y2 - y1
        if best is None or h > best[0]:
            best = (h, t.lstrip("$"))
    return best[1] if best else None


if __name__ == "__main__":
    def blk(text, x1, y1, x2, y2):
        return {"text": text, "bbox": (x1, y1, x2, y2), "confidence": 1.0}

    cases = [
        ([blk("MC $42.1K Liq $18K Holders 312", 37, 233, 389, 255)],
         {"market_cap_usd": 42_100, "liquidity_usd": 18_000, "holders": 312}),
        ([blk("Market Cap: $1.3M", 0, 0, 200, 20)], {"market_cap_usd": 1_300_000}),
        # JIMOTHY-оверлей: значение над подписью, ATH рядом не должен попасть
        ([blk("$1.3M", 1019, 35, 1110, 75), blk("MARKET CAP", 1025, 80, 1100, 92),
          blk("$47.4M", 730, 640, 830, 675), blk("ALL-TIME HIGH", 730, 682, 830, 694),
          blk("30,484", 860, 35, 960, 75), blk("HOLDERS", 880, 80, 935, 92)],
         {"market_cap_usd": 1_300_000, "holders": 30_484}),
        ([blk("ATH $47.4M", 0, 0, 100, 20)], {}),
        ([blk("Vol 24h $51.9K", 0, 0, 100, 20)], {}),
    ]
    ok = 0
    for blocks, want in cases:
        got = extract_metrics(blocks)
        good = all(abs(got.get(k, -1) - v) < 1e-6 for k, v in want.items()) and set(got) == set(want)
        ok += good
        print("OK " if good else "BAD", got, "want", want)
    print(f"metrics: {ok}/{len(cases)}")
    raise SystemExit(0 if ok == len(cases) else 1)
