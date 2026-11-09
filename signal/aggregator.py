"""Объединение результатов vision + audio пайплайнов во временном окне.

Vision работает на 3 fps, audio — чанками по 3 сек, поэтому результаты приходят
в разное время. Агрегатор держит скользящие окна наблюдений каждого канала и
по запросу отдаёт слитый снапшот (VisionObservation, AudioObservation),
который потом оценивает SignalScorer.

Входы — результаты соседних пайплайнов (dict или объект с атрибутами):

    vision.pipeline.VisionResult:
        mints / addresses   — list[str] | [{address, type: "mint"|"wallet", confidence}]
        tickers             — list[str] | [{ticker, confidence}]
        platform            — "photon" | "bullx" | "gmgn" | "pumpfun" | ... | None
        actions / action    — [{action, confidence}] | str: viewing_token, about_to_buy,
                              bought, about_to_sell, sold (searching — игнорируется)

    audio.pipeline.AudioResult:
        transcript          — текст Whisper
        signals             — [{type: buy|watch|sell, phrase, confidence, matched}]
        tickers             — list[str] | [{ticker, confidence}]
        (также поддерживаются buy_phrase / sell_phrase: str | list | bool, mints)

Элементы с confidence ниже MIN_*_CONF отбрасываются.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

# --------------------------------------------------------------------------
# Валидация / нормализация
# --------------------------------------------------------------------------

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}
_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_TICKER_RE = re.compile(r"^[A-Z0-9]{2,12}$")

# Тикеры, которые постоянно видны в UI терминалов и ничего не значат
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
# Приоритет при нескольких действиях на одном фрейме: факт > намерение > просмотр
_ACTION_PRIORITY = {"bought": 5, "sold": 5, "about_to_buy": 4, "about_to_sell": 4, "viewing_token": 1}

MIN_TICKER_CONF = 0.4
MIN_ACTION_CONF = 0.5
MIN_PHRASE_CONF = 0.5


def is_valid_mint(s: str) -> bool:
    """Base58 строка, декодирующаяся ровно в 32 байта (pubkey Solana).

    Отсекает большую часть OCR-мусора (обрезанные/склеенные адреса).
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
    """list[str] | str | [{key: ..., confidence: ...}] → list[str] с фильтром по confidence."""
    out = []
    for item in _as_list(val):
        if isinstance(item, dict):
            if float(item.get("confidence", 1.0)) < min_conf or item.get(key) is None:
                continue
            item = item[key]
        out.append(str(item))
    return out


def _uniq(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(i for i in items if i))


def _phrase(val: Any, transcript: str) -> str | None:
    """buy_phrase может быть строкой, списком фраз или bool."""
    if val is None or val is False:
        return None
    if val is True:
        return transcript.strip()[:80] or "<buy phrase>"
    items = [str(v).strip() for v in _as_list(val) if str(v).strip()]
    return items[0] if items else None


# --------------------------------------------------------------------------
# Наблюдения
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class VisionObservation:
    mints: tuple[str, ...] = ()
    tickers: tuple[str, ...] = ()
    platform: str | None = None
    action: str | None = None  # viewing_token | about_to_buy | bought | sold
    text: str = ""
    timestamp: float = 0.0

    @property
    def empty(self) -> bool:
        return not (self.mints or self.tickers or self.platform or self.action)


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
    mints = _field(raw, "mints", "mint", "mint_addresses")
    if mints is None:  # addresses: [{address, type}] — только mint, не кошельки
        mints = [a for a in _as_list(_field(raw, "addresses"))
                 if not isinstance(a, dict) or a.get("type", "mint") == "mint"]
    obs = VisionObservation(
        mints=_uniq(m.strip() for m in _items(mints, "address") if is_valid_mint(m.strip())),
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
    # KeywordSpotter: signals=[{type, phrase, confidence}], по убыванию confidence не гарантировано
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
# Агрегатор
# --------------------------------------------------------------------------

class SignalAggregator:
    """Скользящие окна наблюдений; snapshot() сливает окно в одно наблюдение.

    Порядок токенов — по свежести (последний увиденный первым): если стример
    переключился на другой токен, сигнал должен относиться к нему, а не к
    тому, что дольше висел на экране.
    """

    def __init__(self, vision_window_sec: float = 3.0, audio_window_sec: float = 10.0):
        self.vision_window_sec = vision_window_sec
        self.audio_window_sec = audio_window_sec
        self._vision: deque[VisionObservation] = deque()
        self._audio: deque[AudioObservation] = deque()

    def add_vision(self, obs: VisionObservation | None) -> None:
        if obs is not None:
            self._vision.append(obs)

    def add_audio(self, obs: AudioObservation | None) -> None:
        if obs is not None:
            self._audio.append(obs)

    def _prune(self, now: float) -> None:
        while self._vision and now - self._vision[0].timestamp > self.vision_window_sec:
            self._vision.popleft()
        while self._audio and now - self._audio[0].timestamp > self.audio_window_sec:
            self._audio.popleft()

    def snapshot(self, now: float | None = None) -> tuple[VisionObservation | None, AudioObservation | None]:
        now = time.time() if now is None else now
        self._prune(now)
        return self._merge_vision(), self._merge_audio()

    def _merge_vision(self) -> VisionObservation | None:
        if not self._vision:
            return None
        newest_first = list(reversed(self._vision))
        platform = next((o.platform for o in newest_first if o.platform), None)
        actions = [o.action for o in newest_first if o.action]
        if actions and actions[0] in SELL_ACTIONS:
            action = actions[0]
        else:
            buy_actions = [a for a in actions if a in BUY_ACTION_RANK]
            action = max(buy_actions, key=BUY_ACTION_RANK.__getitem__) if buy_actions else None
        return VisionObservation(
            mints=_uniq(m for o in newest_first for m in o.mints),
            tickers=_uniq(t for o in newest_first for t in o.tickers),
            platform=platform,
            action=action,
            text=newest_first[0].text,
            timestamp=newest_first[0].timestamp,
        )

    def _merge_audio(self) -> AudioObservation | None:
        if not self._audio:
            return None
        newest_first = list(reversed(self._audio))
        return AudioObservation(
            transcript=" ".join(o.transcript for o in self._audio if o.transcript).strip(),
            buy_phrase=next((o.buy_phrase for o in newest_first if o.buy_phrase), None),
            sell_phrase=next((o.sell_phrase for o in newest_first if o.sell_phrase), None),
            tickers=_uniq(t for o in newest_first for t in o.tickers),
            mints=_uniq(m for o in newest_first for m in o.mints),
            timestamp=newest_first[0].timestamp,
        )

    def clear(self) -> None:
        self._vision.clear()
        self._audio.clear()
