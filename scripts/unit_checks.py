#!/usr/bin/env python3
"""Offline unit checks for the false-signal guards (no models, no network).

    .venv/bin/python scripts/unit_checks.py

Covers: buy/sell phrase spotting (fuzzy, negation, negative context), Whisper
prompt-echo filter, UI toast rules, aggregator mint guards (>=2 frames,
ambiguous mints, phrase binding / TTL, OCR-confidence weighting), wire timestamp.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Module dirs directly (NOT the repo root: signal/ would shadow stdlib `signal`)
for d in ("signal", "audio", "vision"):
    sys.path.insert(0, str(ROOT / d))

import numpy as np  # noqa: E402

from aggregator import SignalAggregator, normalize_audio, normalize_vision  # noqa: E402
from keyword_spotter import KeywordSpotter  # noqa: E402
from scorer import SignalScorer  # noqa: E402
from transcriber import DEFAULT_INITIAL_PROMPT, repeats_prompt  # noqa: E402
from ui_recognizer import UIRecognizer  # noqa: E402

failures: list[str] = []
passed = 0


def check(cond: bool, name: str) -> None:
    global passed
    if cond:
        passed += 1
    else:
        failures.append(name)
        print("FAIL:", name)


# ----------------------------------------------------------------- keyword spotter
ks = KeywordSpotter()


def kinds(text: str) -> set[str]:
    return {s["type"] for s in ks.spot(text)}


NO_BUY = [
    "what's going on chat",                       # fuzzy "going in"
    "getting it done today",                      # fuzzy "getting in"
    "lets go boys",
    "im in the discord",
    "yolo",
    "full send on this stream",
    "everyone is aping into this rug",            # negative context
    "stay away from this, people aping in are getting rugged",
    "this is a honeypot dont ape into it",
    "i would never be aping into that",           # negation 4 tokens back
    "im not going to be buying this",             # negation 3 tokens back
]
YES_BUY = [
    "aping in right now",
    "just bought a bag",
    "i'm buying this one",
    "ok guys aping into PEPE right now",
    "not gonna lie aping in",                     # "not gonna lie" is not a negation
]
NO_SELL = [
    "closing the stream soon", "where is the exit", "get out of here",
    "it sold out in a minute", "selling point of this token", "who sold",
]
YES_SELL = ["i sold it", "taking profits here", "selling now guys", "i'm out"]

for t in NO_BUY:
    check("buy" not in kinds(t), f"no BUY: {t!r} -> {ks.spot(t)}")
for t in YES_BUY:
    check("buy" in kinds(t), f"BUY: {t!r} -> {ks.spot(t)}")
for t in NO_SELL:
    check("sell" not in kinds(t), f"no SELL: {t!r} -> {ks.spot(t)}")
for t in YES_SELL:
    check("sell" in kinds(t), f"SELL: {t!r} -> {ks.spot(t)}")

# ----------------------------------------------------------------- whisper prompt
low = DEFAULT_INITIAL_PROMPT.lower()
check(not any(w in low for w in ("aping", "send", "$", "buy")), "prompt has no buy phrases/tickers")
check(repeats_prompt(DEFAULT_INITIAL_PROMPT, DEFAULT_INITIAL_PROMPT), "prompt echo dropped")
check(repeats_prompt("Solana, pump.fun, market cap.", DEFAULT_INITIAL_PROMPT), "partial prompt echo dropped")
check(not repeats_prompt("aping into this one on pump.fun right now", DEFAULT_INITIAL_PROMPT),
      "real speech mentioning pump.fun kept")

# ----------------------------------------------------------------- UI toasts
ui = UIRecognizer()
H, W = 720, 1280


def frame(green_box=None):
    f = np.full((H, W, 3), 20, dtype=np.uint8)
    if green_box:
        x1, y1, x2, y2 = green_box
        f[y1:y2, x1:x2] = (20, 180, 60)
    return f


def blk(text, bbox):
    return {"text": text, "bbox": bbox, "confidence": 0.9, "roi": "full"}


def acts(f, blocks):
    return {a["action"] for a in ui.detect_actions(f, blocks, "photon", W, H)}


toast = (900, 600, 1200, 630)
green = frame((860, 585, 1240, 645))
check("bought" not in acts(green, [blk("Transaction confirmed", toast)]), "bare 'confirmed' is not bought")
check("bought" not in acts(frame(), [blk("Buy successful", toast)]), "buy toast without green is not bought")
check("bought" in acts(green, [blk("Buy successful", toast)]), "green 'Buy successful' toast is bought")
check("sold" not in acts(frame(), [blk("Success", toast), blk("Sell", (1220, 600, 1270, 630))]),
      "nearby Sell button does not make a success toast 'sold'")
check("sold" in acts(frame(), [blk("Sold 1.2M PEPE successfully", toast)]), "explicit sell toast is sold")

# ----------------------------------------------------------------- aggregator
MINT_A = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
MINT_B = "So11111111111111111111111111111111111111112"
scorer = SignalScorer()
BUY_AUDIO = {"transcript": "aping in right now", "signals": [{"type": "buy", "phrase": "aping in", "confidence": 0.9}]}


def vis(ts, mints, **kw):
    return normalize_vision({"mints": mints, "platform": "photon", "timestamp": ts, **kw}, ts)


def run(frames, audio=None, now=None):
    agg = SignalAggregator(3.0, 10.0, audio_chunk_sec=3.0)
    for ts, mints in frames:
        agg.add_vision(vis(ts, mints))
    if audio is not None:
        agg.add_audio(normalize_audio({**BUY_AUDIO, "timestamp": audio}, audio))
    v, a = agg.snapshot(now)
    return v, a, scorer.score(v, a, now=now)


T = 1_000_000.0
v, a, sig = run([(T, [MINT_A])], audio=T, now=T + 0.1)
check(not v.mints and v.unconfirmed_mints == (MINT_A,), "mint in 1 frame is not confirmed")
check(sig is None or not sig.mint, "single-frame mint cannot drive a signal")

v, a, sig = run([(T, [MINT_A]), (T + 0.33, [MINT_A])], audio=T + 0.3, now=T + 0.4)
check(sig is not None and sig.mint == MINT_A and sig.action == "buy", f"2 frames + phrase -> BUY ({sig})")
check(sig is not None and abs(sig.timestamp - (T + 0.33)) < 1e-6, "wire timestamp = newest evidence capture ts")

v, a, sig = run([(T, [MINT_A]), (T + 0.33, [MINT_A]), (T + 0.66, [MINT_B])], audio=T + 0.3, now=T + 0.7)
check(v.ambiguous_mints and sig is not None and sig.action != "buy", "two distinct mints -> no BUY")

# OCR misread of the same mint (1 char) is not "ambiguous"
MINT_A2 = MINT_A[:10] + ("Z" if MINT_A[10] != "Z" else "2") + MINT_A[11:]
agg = SignalAggregator(3.0, 10.0)
for ts in (T, T + 0.33):
    agg.add_vision(vis(ts, [MINT_A]))
va2 = normalize_vision({"mints": [MINT_A2], "timestamp": T + 0.5}, T + 0.5)
check(va2 is not None and va2.mints == (MINT_A2,), "OCR variant is itself a valid pubkey (test precondition)")
agg.add_vision(va2)
v, _ = agg.snapshot(T + 0.6)
check(not v.ambiguous_mints, "OCR variant of the same mint is not ambiguous")

# phrase spoken (chunk ended) 6 s before the mint appeared -> not bound
v, a, sig = run([(T + 6, [MINT_A]), (T + 6.33, [MINT_A])], audio=T, now=T + 6.4)
check(a is None or not a.buy_phrase, "old buy phrase (TTL 4s) dropped")
v, a, sig = run([(T + 3.5, [MINT_A]), (T + 3.8, [MINT_A])], audio=T, now=T + 3.9)
check(a is None or not a.buy_phrase, "phrase not bound to a mint that appeared >2s after it")

# OCR confidence scales the mint weight; repaired mints at most 0.9x
v1 = normalize_vision({"addresses": [{"address": MINT_A, "type": "mint", "confidence": 0.5}], "timestamp": T}, T)
check(abs(v1.mint_factor(MINT_A) - 0.9) < 1e-9, "mint factor = 0.8 + 0.2 x OCR confidence")
v2 = normalize_vision({"addresses": [{"address": MINT_A, "type": "mint", "confidence": 1.0,
                                      "repaired_from": "x"}], "timestamp": T}, T)
check(v2.mint_factor(MINT_A) <= 0.9, "repaired mint factor <= 0.9")
agg = SignalAggregator(3.0, 10.0)
agg.add_vision(v1)
agg.add_vision(normalize_vision({"addresses": [{"address": MINT_A, "type": "mint", "confidence": 0.5}],
                                 "timestamp": T + 0.3}, T + 0.3))
sig = scorer.score(*agg.snapshot(T + 0.4), now=T + 0.4)
check(sig is not None and abs(sig.components["visual_mint_detected"] - 0.45) < 1e-9,
      "mint weight scaled by confidence (0.9 x 0.50)")

print(f"unit checks: {passed} passed, {len(failures)} failed")
sys.exit(1 if failures else 0)
