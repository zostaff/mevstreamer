"""Детекция Solana адресов (mint / wallet) и тикеров в OCR-блоках."""

from __future__ import annotations

import re

try:
    from .mint_repair import is_near_mint
    from .ocr import bbox_height, group_lines
except ImportError:  # запуск как скрипт из vision/
    from mint_repair import is_near_mint
    from ocr import bbox_height, group_lines

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(BASE58_ALPHABET)}
_B58 = "1-9A-HJ-NP-Za-km-z"

# Границы — не-base58 символы, чтобы не выдёргивать 44 символа из tx-сигнатуры (88)
ADDRESS_RE = re.compile(rf"(?<![{_B58}])[{_B58}]{{32,44}}(?![{_B58}])")
_HEAD_RUN_RE = re.compile(rf"^[{_B58}]+")
_TAIL_RUN_RE = re.compile(rf"[{_B58}]+$")
_B58_WORD_RE = re.compile(rf"^[{_B58}]+$")

# Контекст, при котором не-pump адрес — скорее всего mint, а не кошелёк
_MINT_CONTEXT_RE = re.compile(r"(?i)\b(ca|contract|mint|token\s*address)\b")

DOLLAR_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Z][A-Z0-9]{1,9})(?![A-Za-z0-9])")
LABEL_TICKER_RE = re.compile(r"(?i)\b(?:ticker|symbol)\s*[:\-]?\s*\$?([A-Za-z][A-Za-z0-9]{1,9})\b")
PRICE_SOL_RE = re.compile(r"(?i)\d[\d.,]*\s*[kmb]?\s*sol\b")
_BARE_TICKER_RE = re.compile(r"^\$?([A-Z][A-Z0-9]{1,9})$")

# Базовые валюты и UI-лейблы, которые выглядят как тикеры
TICKER_STOPWORDS = frozenset({
    "SOL", "WSOL", "USD", "USDC", "USDT", "MC", "MCAP", "FDV", "LIQ", "VOL", "ATH",
    "TVL", "BUY", "SELL", "PNL", "TXNS", "HOLDERS", "PRICE", "AUTO", "MAX", "MIN",
    "CA", "DEV", "TOP", "NEW", "LP", "KOL", "OK", "PUMP",
})


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + body


def is_valid_address(s: str) -> bool:
    """32–44 символа base58 И декодируется ровно в 32 байта (ed25519 pubkey).

    Проверка длины после декодирования отсекает большую часть OCR-мусора,
    который случайно состоит из base58 символов.
    """
    if not 32 <= len(s) <= 44 or any(c not in _B58_INDEX for c in s):
        return False
    return len(b58decode(s)) == 32


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


class TokenDetector:
    def __init__(self):
        self.address_re = ADDRESS_RE
        self.alphabet = BASE58_ALPHABET

    # -- addresses ---------------------------------------------------------- #

    def find_addresses(self, ocr_results: list[dict]) -> list[dict]:
        """→ [{address, type: "mint"|"wallet", bbox, confidence, source}], mint'ы первыми."""
        found: dict[str, dict] = {}
        lines = group_lines(ocr_results)
        line_text = {id(b): " ".join(x["text"] for x in line) for line in lines for b in line}

        for b in ocr_results:
            text, ctx = b["text"], line_text.get(id(b), b["text"])
            # 1. Целый адрес в одном блоке
            for m in self.address_re.finditer(text):
                self._add(found, m.group(), b["bbox"], b["confidence"], ctx, b.get("roi"))
            # 1b. OCR вставил пробел(ы) внутрь адреса: "7xKXtg2CW87 d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
            words = text.split()
            for i in range(len(words)):
                for k in (2, 3):
                    part = words[i:i + k]
                    if len(part) < k or not all(_B58_WORD_RE.match(w) for w in part):
                        continue
                    cand = "".join(part)
                    if is_valid_address(cand):
                        self._add(found, cand, b["bbox"], b["confidence"] * 0.9, ctx, b.get("roi"))

        # 2. Адрес разбит на 2 соседних блока на одной Y-линии
        for line in lines:
            for a, c in zip(line, line[1:]):
                gap = c["bbox"][0] - a["bbox"][2]
                if gap > max(bbox_height(a), bbox_height(c)) * 1.5:
                    continue
                tail = _TAIL_RUN_RE.search(a["text"])
                head = _HEAD_RUN_RE.search(c["text"])
                if not (tail and head):
                    continue
                cand = tail.group() + head.group()
                if is_valid_address(cand):
                    conf = min(a["confidence"], c["confidence"]) * 0.85
                    ctx = line_text.get(id(a), "")
                    self._add(found, cand, _union(a["bbox"], c["bbox"]), conf, ctx, a.get("roi"))

        return sorted(found.values(), key=lambda r: (r["type"] != "mint", -r["confidence"]))

    def find_near_mints(self, ocr_results: list[dict]) -> list[dict]:
        """OCR-испорченные pump.fun mint'ы (→ mint_repair): [{raw, bbox, confidence, source}]."""
        out: dict[str, dict] = {}
        for b in ocr_results:
            words = b["text"].split()
            # и как есть, и с пробелом, вставленным OCR внутрь адреса
            for w in words + ["".join(words[i:i + 2]) for i in range(len(words) - 1)]:
                w = w.split(":")[-1]  # "CA:9iPG…pump"
                if is_near_mint(w) and w not in out:
                    out[w] = {"raw": w, "bbox": tuple(b["bbox"]), "confidence": b["confidence"], "source": b.get("roi")}
        return list(out.values())

    @staticmethod
    def _classify(address: str, context: str) -> tuple[str, float]:
        if address.endswith("pump"):
            # pump.fun vanity mint: 44 символа; короче — тоже mint, но менее уверенно
            return "mint", 1.05 if len(address) == 44 else 0.95
        if _MINT_CONTEXT_RE.search(context):
            return "mint", 0.9
        return "wallet", 1.0

    def _add(self, found, address, bbox, conf, context, roi) -> None:
        kind, mult = self._classify(address, context)
        conf = round(min(1.0, conf * mult), 4)
        prev = found.get(address)
        if prev is None or conf > prev["confidence"]:
            found[address] = {
                "address": address, "type": kind, "bbox": tuple(bbox),
                "confidence": conf, "source": roi,
            }

    # -- tickers ------------------------------------------------------------ #

    def find_tickers(self, ocr_results: list[dict]) -> list[dict]:
        """→ [{ticker, confidence, bbox}], по убыванию confidence."""
        found: dict[str, dict] = {}

        def add(ticker: str, conf: float, bbox) -> None:
            ticker = ticker.upper()
            if ticker in TICKER_STOPWORDS:
                return
            conf = round(min(1.0, conf), 4)
            if ticker not in found or conf > found[ticker]["confidence"]:
                found[ticker] = {"ticker": ticker, "confidence": conf, "bbox": tuple(bbox)}

        for b in ocr_results:
            for m in DOLLAR_TICKER_RE.finditer(b["text"]):
                add(m.group(1), b["confidence"], b["bbox"])
            for m in LABEL_TICKER_RE.finditer(b["text"]):
                add(m.group(1), b["confidence"] * 0.9, b["bbox"])

        # Слово КАПСОМ рядом с ценой в SOL (та же строка или строка над ней)
        lines = group_lines(ocr_results)
        for li, line in enumerate(lines):
            if not any(PRICE_SOL_RE.search(b["text"]) for b in line):
                continue
            neighbours = line + (lines[li - 1] if li > 0 else [])
            for b in neighbours:
                m = _BARE_TICKER_RE.match(b["text"].strip())
                if m:
                    add(m.group(1), b["confidence"] * 0.5, b["bbox"])

        return sorted(found.values(), key=lambda r: -r["confidence"])
