"""Merges vision + audio pipeline results over a time window.

Vision runs at 3 fps, audio in 3 s chunks, so results arrive
at different times. The aggregator keeps a sliding window of observations per channel
and on request returns a merged snapshot (VisionObservation, AudioObservation)
for SignalScorer to evaluate.

Inputs are results from the sibling pipelines (dict or attribute object):

    vision.pipeline.VisionResult:
        mints / addresses   — list[str] | [{address, type: "mint"|"wallet", confidence}]
        tickers             — list[str] | [{ticker, confidence}]
        platform            — "photon" | "bullx" | "gmgn" | "pumpfun" | ... | None
        actions / action    — [{action, confidence}] | str: viewing_token, about_to_buy,
                              bought, about_to_sell, sold (searching is ignored)

    audio.pipeline.AudioResult:
        transcript          — Whisper text
        signals             — [{type: buy|watch|sell, phrase, confidence, matched}]
        tickers             — list[str] | [{ticker, confidence}]
        (buy_phrase / sell_phrase: str | list | bool, mints are also accepted)

Items with confidence below MIN_*_CONF are dropped.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

# --------------------------------------------------------------------------
# Validation / normalization
# --------------------------------------------------------------------------

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}
_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_TICKER_RE = re.compile(r"^[A-Z0-9]{2,12}$")

# Tickers always visible in terminal UIs that carry no signal
TICKER_STOPLIST = frozenset({"SOL", "WSOL", "USDC", "USDT", "USD", "BUY", "SELL", "MC", "LP", "ATH"})

ACTION_ALIASES = {
    "viewing_token": "viewing_token", "viewing": "viewing_token", "token_page": "viewing_token",
    "about_to_buy": "about_to_buy", "buy_button": "about_to_buy", "hover_buy": "about_to_buy",
    "bought": "bought", "buy_toast": "bought", "buy_success": "bought",
    "sold": "sold", "sell_toast": "sold", "sell_success": "sold",
    "about_to_sell": "about_to_sell",
}
BUY_ACTION_RANK = {"viewing_token": 1, "about_to_buy": 2, "bought": 3}
SELL_ACTIONS = frozenset({"about_to_sell", "sold"})
# Priority for multiple actions on one frame: done > intent > viewing
_ACTION_PRIORITY = {"bought": 5, "sold": 5, "about_to_buy": 4, "about_to_sell": 4, "viewing_token": 1}

log = logging.getLogger("signal.aggregator")

MIN_TICKER_CONF = 0.4
REPAIRED_MINT_MAX_FACTOR = 0.9
MINT_CONF_FLOOR = 0.8
MIN_ACTION_CONF = 0.5
MIN_PHRASE_CONF = 0.5


def is_valid_mint(s: str) -> bool:
    """Base58 string that decodes to exactly 32 bytes (Solana pubkey).

    Rejects most OCR garbage (truncated/merged addresses).
    """
    if not _MINT_RE.match(s):
        return False
    n = 0
    for c in s:
        n = n * 58 + _B58_INDEX[c]
    leading_zeros = len(s) - len(s.lstrip("1"))
    return leading_zeros + (n.bit_length() + 7) // 8 == 32


def normalize_ticker(raw: str) -> str | None:
    t = raw.strip().lstrip("$").upper()
    if not _TICKER_RE.match(t) or t in TICKER_STOPLIST:
        return None
    return t


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
        else:
            val = getattr(obj, name, None)
            if val is not None:
                return val
    return default


def _as_list(val: Any) -> list:
    if val is None:
        return []
    if isinstance(val, (str, bytes)):
        return [val]
    if isinstance(val, Iterable):
        return list(val)
    return [val]


def _items(val: Any, key: str, min_conf: float = 0.0) -> list[str]:
    """list[str] | str | [{key: ..., confidence: ...}] → list[str] filtered by confidence."""
    out = []
    for item in _as_list(val):
        if isinstance(item, dict):
            if float(item.get("confidence", 1.0)) < min_conf or item.get(key) is None:
                continue
            item = item[key]
        out.append(str(item))
    return out


def _mint_items(val: Any) -> list[tuple[str, float]]:
    """mints/addresses → [(mint, weight_factor)]: factor = OCR/address confidence
    (strings without confidence → 1.0); mint_repair results are capped at 0.9."""
    out = []
    for item in _as_list(val):
        if isinstance(item, dict):
            addr = item.get("address") or item.get("mint")
            if addr is None:
                continue
            # A valid 32-byte base58 address is self-checking (a random
            # OCR error almost always breaks decoding), so OCR confidence is
            # a weak signal: factor 0.8…1.0. Apple Vision often reports exactly 0.5;
            # without the floor that halved the mint weight and the signal missed the threshold.
            conf = MINT_CONF_FLOOR + (1.0 - MINT_CONF_FLOOR) * float(item.get("confidence", 1.0))
            if item.get("repaired_from"):
                conf = min(conf, REPAIRED_MINT_MAX_FACTOR)
        else:
            addr, conf = item, 1.0
        addr = str(addr).strip()
        if is_valid_mint(addr):
            out.append((addr, max(0.0, min(1.0, conf))))
    return out


def _near_same_mint(a: str, b: str) -> bool:
    """Two mints are likely the same address corrupted by OCR (≤2 positional differences)."""
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) <= 2


def _uniq(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(i for i in items if i))


def _phrase(val: Any, transcript: str) -> str | None:
    """buy_phrase may be a string, a list of phrases or a bool."""
    if val is None or val is False:
        return None
    if val is True:
        return transcript.strip()[:80] or "<buy phrase>"
    items = [str(v).strip() for v in _as_list(val) if str(v).strip()]
    return items[0] if items else None


# --------------------------------------------------------------------------
# Observations
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class VisionObservation:
    mints: tuple[str, ...] = ()
    tickers: tuple[str, ...] = ()
    platform: str | None = None
    action: str | None = None  # viewing_token | about_to_buy | bought | sold
    text: str = ""
    timestamp: float = 0.0
    # mint → weight factor (OCR confidence; repaired ≤ 0.9). Missing entry → 1.0
    mint_conf: tuple[tuple[str, float], ...] = ()
    # Merged snapshot only: mints seen in a single frame (not yet confirmed),
    # and the "window has >1 distinct mints" flag, which blocks BUY
    unconfirmed_mints: tuple[str, ...] = ()
    ambiguous_mints: bool = False

    @property
    def empty(self) -> bool:
        return not (self.mints or self.tickers or self.platform or self.action)

    def mint_factor(self, mint: str) -> float:
        return dict(self.mint_conf).get(mint, 1.0)


@dataclass(frozen=True)
class AudioObservation:
    transcript: str = ""
    buy_phrase: str | None = None
    sell_phrase: str | None = None
    tickers: tuple[str, ...] = ()
    mints: tuple[str, ...] = ()
    timestamp: float = 0.0

    @property
    def empty(self) -> bool:
        return not (self.buy_phrase or self.sell_phrase or self.tickers or self.mints)


def normalize_vision(raw: Any, timestamp: float) -> VisionObservation | None:
    if raw is None:
        return None
    if isinstance(raw, VisionObservation):
        return raw
    actions = [ACTION_ALIASES.get(a.lower()) for a in
               _items(_field(raw, "actions", "action", "ui_action", "ui_state"), "action", MIN_ACTION_CONF)]
    actions = [a for a in actions if a]
    action = max(actions, key=_ACTION_PRIORITY.__getitem__) if actions else None
    platform = _field(raw, "platform")
    # addresses: [{address, type, confidence, repaired_from?}] take precedence
    # since they carry confidence (VisionResult.mints are bare strings). Mints only, not wallets.
    addresses = _field(raw, "addresses")
    if addresses is not None:
        mints = [a for a in _as_list(addresses)
                 if not isinstance(a, dict) or a.get("type", "mint") == "mint"]
    else:
        mints = _field(raw, "mints", "mint", "mint_addresses")
    conf: dict[str, float] = {}
    for m, c in _mint_items(mints):
        conf[m] = max(c, conf.get(m, 0.0))
    obs = VisionObservation(
        mints=tuple(conf),
        mint_conf=tuple(conf.items()),
        tickers=_uniq(normalize_ticker(t) for t in _items(_field(raw, "tickers", "ticker"), "ticker", MIN_TICKER_CONF)),
        platform=str(platform).strip() or None if platform else None,
        action=action,
        text=str(_field(raw, "text", "ocr_text", default="")),
        timestamp=float(_field(raw, "timestamp", default=timestamp)),
    )
    return None if obs.empty else obs


def normalize_audio(raw: Any, timestamp: float) -> AudioObservation | None:
    if raw is None:
        return None
    if isinstance(raw, AudioObservation):
        return raw
    transcript = str(_field(raw, "transcript", "text", default=""))
    buy = _field(raw, "buy_phrase", "buy_phrases")
    sell = _field(raw, "sell_phrase", "sell_phrases")
    # KeywordSpotter: signals=[{type, phrase, confidence}], not guaranteed to be sorted by confidence
    spotted = sorted((s for s in _as_list(_field(raw, "signals")) if isinstance(s, dict)
                      and float(s.get("confidence", 1.0)) >= MIN_PHRASE_CONF),
                     key=lambda s: -float(s.get("confidence", 1.0)))
    if buy is None:
        buy = [s.get("phrase") or s.get("matched") for s in spotted if s.get("type") == "buy"] or None
    if sell is None:
        sell = [s.get("phrase") or s.get("matched") for s in spotted if s.get("type") == "sell"] or None
    obs = AudioObservation(
        transcript=transcript,
        buy_phrase=_phrase(buy, transcript),
        sell_phrase=_phrase(sell, transcript),
        tickers=_uniq(normalize_ticker(t) for t in _items(_field(raw, "tickers", "ticker"), "ticker", MIN_TICKER_CONF)),
        mints=_uniq(m.strip() for m in _items(_field(raw, "mints", "mint"), "address")
                    if is_valid_mint(m.strip())),
        timestamp=float(_field(raw, "timestamp", default=timestamp)),
    )
    return None if obs.empty else obs


# --------------------------------------------------------------------------
# Aggregator
# --------------------------------------------------------------------------

class SignalAggregator:
    """Sliding observation windows; snapshot() merges a window into one observation.

    Tokens are ordered by recency (most recently seen first): if the streamer
    switched to another token, the signal must refer to it, not to
    whichever stayed on screen longer.

    Protection against foreign mints (chat overlay, OCR garbage):
      * a mint enters the snapshot only if seen in >= MIN_MINT_FRAMES frames of the window;
      * if the window shows >1 distinct mints (OCR variants of one don't count),
        ambiguous_mints=True and the scorer emits no BUY;
      * a buy/sell phrase lives phrase_ttl_sec and binds to a mint only if
        the mint was on screen within ±phrase_bind_sec of when the phrase was spoken
        ([ts - audio_chunk_sec, ts], ts = end of the audio chunk).
    """

    MIN_MINT_FRAMES = 2
    _AMBIG_LOG_INTERVAL = 10.0

    def __init__(self, vision_window_sec: float = 3.0, audio_window_sec: float = 10.0,
                 phrase_ttl_sec: float = 4.0, phrase_bind_sec: float = 2.0,
                 audio_chunk_sec: float = 3.0):
        self.vision_window_sec = vision_window_sec
        self.audio_window_sec = audio_window_sec
        self.phrase_ttl_sec = phrase_ttl_sec
        self.phrase_bind_sec = phrase_bind_sec
        self.audio_chunk_sec = audio_chunk_sec
        self._vision: deque[VisionObservation] = deque()
        self._audio: deque[AudioObservation] = deque()
        # (ts, mint) — mint sighting history longer than the vision window, for phrase binding
        self._mint_seen: deque[tuple[float, str]] = deque()
        self._last_ambig_log = 0.0

    def add_vision(self, obs: VisionObservation | None) -> None:
        if obs is not None:
            self._vision.append(obs)
            for m in obs.mints:
                self._mint_seen.append((obs.timestamp, m))

    def add_audio(self, obs: AudioObservation | None) -> None:
        if obs is not None:
            self._audio.append(obs)

    def _prune(self, now: float) -> None:
        while self._vision and now - self._vision[0].timestamp > self.vision_window_sec:
            self._vision.popleft()
        while self._audio and now - self._audio[0].timestamp > self.audio_window_sec:
            self._audio.popleft()
        keep = self.audio_window_sec + self.audio_chunk_sec + self.phrase_bind_sec
        while self._mint_seen and now - self._mint_seen[0][0] > keep:
            self._mint_seen.popleft()

    def snapshot(self, now: float | None = None) -> tuple[VisionObservation | None, AudioObservation | None]:
        now = time.time() if now is None else now
        self._prune(now)
        vis, aud = self._merge_vision(), self._merge_audio(now)
        if vis is not None and vis.ambiguous_mints and now - self._last_ambig_log > self._AMBIG_LOG_INTERVAL:
            self._last_ambig_log = now
            log.warning("ambiguous mints in window (%d distinct) — BUY suppressed: %s",
                        len(set(vis.mints) | set(vis.unconfirmed_mints)),
                        ", ".join(f"{m[:4]}…{m[-4:]}" for m in (*vis.mints, *vis.unconfirmed_mints)))
        if aud is not None and vis is not None and vis.mints and (aud.buy_phrase or aud.sell_phrase):
            if not self._mint_near(vis.mints[0], aud.timestamp):
                # Phrase was spoken while this mint was not on screen → don't bind
                aud = AudioObservation(transcript=aud.transcript, tickers=aud.tickers,
                                       mints=aud.mints, timestamp=aud.timestamp)
                aud = None if aud.empty else aud
        return vis, aud

    def _mint_near(self, mint: str, phrase_end_ts: float) -> bool:
        lo = phrase_end_ts - self.audio_chunk_sec - self.phrase_bind_sec
        hi = phrase_end_ts + self.phrase_bind_sec
        return any(m == mint and lo <= ts <= hi for ts, m in self._mint_seen)

    def _merge_vision(self) -> VisionObservation | None:
        if not self._vision:
            return None
        newest_first = list(reversed(self._vision))
        frames: dict[str, int] = {}
        factor: dict[str, float] = {}
        for o in newest_first:
            for m in o.mints:
                frames[m] = frames.get(m, 0) + 1
                factor[m] = max(factor.get(m, 0.0), o.mint_factor(m))
        all_mints = _uniq(m for o in newest_first for m in o.mints)
        confirmed = tuple(m for m in all_mints if frames[m] >= self.MIN_MINT_FRAMES)
        unconfirmed = tuple(m for m in all_mints if frames[m] < self.MIN_MINT_FRAMES)
        # Clusters: OCR variants of one address (≤2 differences) count as one mint
        clusters: list[str] = []
        for m in all_mints:
            if not any(_near_same_mint(m, c) for c in clusters):
                clusters.append(m)
        platform = next((o.platform for o in newest_first if o.platform), None)
        actions = [o.action for o in newest_first if o.action]
        if actions and actions[0] in SELL_ACTIONS:
            action = actions[0]
        else:
            buy_actions = [a for a in actions if a in BUY_ACTION_RANK]
            action = max(buy_actions, key=BUY_ACTION_RANK.__getitem__) if buy_actions else None
        return VisionObservation(
            mints=confirmed,
            mint_conf=tuple((m, factor[m]) for m in confirmed),
            unconfirmed_mints=unconfirmed,
            ambiguous_mints=len(clusters) > 1,
            tickers=_uniq(t for o in newest_first for t in o.tickers),
            platform=platform,
            action=action,
            text=newest_first[0].text,
            timestamp=newest_first[0].timestamp,
        )

    def _merge_audio(self, now: float) -> AudioObservation | None:
        if not self._audio:
            return None
        newest_first = list(reversed(self._audio))
        # Phrases live phrase_ttl_sec (tickers/transcript: the whole audio window)
        fresh = [o for o in newest_first if now - o.timestamp <= self.phrase_ttl_sec]
        buy = next((o for o in fresh if o.buy_phrase), None)
        sell = next((o for o in fresh if o.sell_phrase), None)
        phrase_obs = buy or sell
        return AudioObservation(
            transcript=" ".join(o.transcript for o in self._audio if o.transcript).strip(),
            buy_phrase=buy.buy_phrase if buy else None,
            sell_phrase=sell.sell_phrase if sell else None,
            tickers=_uniq(t for o in newest_first for t in o.tickers),
            mints=_uniq(m for o in newest_first for m in o.mints),
            # phrase time (if any): used for mint binding and the wire timestamp
            timestamp=phrase_obs.timestamp if phrase_obs else newest_first[0].timestamp,
        )

    def clear(self) -> None:
        self._vision.clear()
        self._audio.clear()
        self._mint_seen.clear()
