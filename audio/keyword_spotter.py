"""Detect trading key phrases and tickers in a stream transcript.

Matching is token-based (not substring), so "im in" does not fire inside
"im interested", nor "sold" inside "soldier".

Fuzzy: Whisper garbles 1-2 characters ("apeing in", "a ping in"). Tolerance
depends on phrase length: on short phrases fuzzy mostly yields false positives
("selling" → "telling", "sold" → "told"), so single-word and short phrases
match exactly only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CONF_EXACT = 0.9
CONF_FUZZY_1 = 0.7
CONF_FUZZY_2 = 0.5
_CONF_BY_DIST = {0: CONF_EXACT, 1: CONF_FUZZY_1, 2: CONF_FUZZY_2}

# Negation within ≤5 tokens before the phrase → no signal ("i'm not buying this",
# "i would never be aping into that", "don't sell")
_NEGATIONS = {"not", "dont", "never", "wont", "cant", "isnt", "aint", "didnt", "wouldnt", "shouldnt",
              "no", "nobody", "stop"}
_NEGATION_WINDOW = 5
# "not gonna lie, aping in" is not a negation
_NOT_NEGATION_AFTER = (("gonna", "lie"), ("going", "to", "lie"), ("to", "lie"), ("lie",))


def _is_negation(norms: list[str], k: int) -> bool:
    if norms[k] not in _NEGATIONS:
        return False
    return not any(tuple(norms[k + 1:k + 1 + len(p)]) == p for p in _NOT_NEGATION_AFTER)

# Negative context in the same utterance suppresses BUY ("everyone is aping into
# this rug", "stay away, honeypot"). Single words match by token, multi-word by phrase.
_NEGATIVE_WORDS = {"rug", "rugged", "rugging", "rugpull", "scam", "scammer", "scammed", "honeypot", "avoid"}
_NEGATIVE_PHRASES = ("stay away", "dont buy", "do not buy", "not buying", "rug pull")

# Unambiguous phrases (everything else is exact-only). Fuzzy is allowed only for
# long, specific phrases where a Whisper typo cannot turn them into everyday speech
# ("going on" ≈ "going in", "getting it" ≈ "getting in", hence exact-only).
_FUZZY_OK = {
    "buying this", "aping in", "aping into", "sniping this", "buying right now",
    "just bought", "im buying", "market buy", "taking profit", "taking profits",
    "selling now", "cutting losses",
}

# Phrases without an object: the next word is not a ticker ("let's go guys", "yolo bro")
_NO_OBJECT_PHRASES = {
    "interesting", "what do you think", "this looks good", "could be big", "im out",
    "sold it", "selling it", "dumping it",
}

# Verbs usually followed by a ticker ("buying [TICKER]"), even outside phrases
_TICKER_VERBS = {
    "buying": 0.6, "buy": 0.55, "bought": 0.6, "aping": 0.6, "ape": 0.55,
    "sniping": 0.6, "snipe": 0.55, "grabbing": 0.55, "longing": 0.55,
    "selling": 0.55, "sell": 0.5, "dumping": 0.5, "holding": 0.5,
}

# Skipped when looking for a ticker after a phrase ("buying some more PEPE")
_FILLERS = {
    "this", "that", "the", "a", "an", "some", "more", "into", "in", "on", "at",
    "right", "now", "here", "uh", "um", "uhh", "umm", "like", "just", "real",
    "quick", "it", "little", "bit", "of", "worth", "out", "called", "named",
    "ticker", "coin", "token", "my", "all", "rest", "bag", "bags", "position",
}
_NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "twenty", "thirty", "fifty", "hundred", "thousand", "half", "quarter", "couple",
}
_AMOUNT_UNITS = {"sol", "solana", "eth", "k", "bucks", "dollars", "grand", "usd", "usdc"}

# Not tickers: function words, streamer fillers and trading slang.
STOPWORDS = {
    # spec
    "this", "it", "some", "that", "the",
    # function words
    "a", "an", "and", "or", "but", "so", "if", "then", "than", "as", "by", "from",
    "in", "on", "into", "at", "of", "to", "for", "with", "up", "down", "out", "off",
    "over", "back", "about", "here", "there", "now", "right", "again", "too", "also",
    "very", "just", "really", "only", "even", "still", "because", "cause", "cuz",
    "not", "dont", "cant", "wont", "no", "yes", "yeah", "yep", "nah", "ok", "okay",
    # pronouns / question words
    "i", "im", "me", "my", "mine", "you", "your", "youre", "we", "us", "our", "they",
    "them", "their", "he", "she", "his", "her", "its", "these", "those", "what",
    "which", "who", "how", "why", "when", "where", "everyone", "everybody",
    # verbs
    "is", "was", "be", "are", "am", "were", "been", "being", "do", "did", "does",
    "have", "has", "had", "get", "getting", "got", "go", "going", "gonna", "wanna",
    "gotta", "lets", "let", "can", "could", "would", "should", "will", "might",
    "think", "thinking", "know", "see", "check", "look", "looks", "looking",
    "watch", "watching", "keep", "make", "take", "taking", "put", "want",
    # quantities
    "all", "everything", "something", "anything", "nothing", "thing", "stuff",
    "more", "much", "lot", "lots", "bit", "little", "few", "many", "any", "every",
    "another", "other", "next", "same", "one", "half",
    # adjectives / interjections
    "big", "good", "bad", "nice", "huge", "great", "new", "real", "quick", "fast",
    "uh", "um", "uhh", "umm", "oh", "like", "well", "yo", "hey", "wow", "damn",
    "holy", "shit", "fuck", "fucking", "lol", "lmao", "haha", "bro", "dude", "man",
    "guys", "chat", "boys", "baby", "maybe", "probably", "actually", "literally",
    "definitely", "honestly", "way", "time", "today", "tonight", "please",
    # trading slang (not tickers)
    "buy", "buying", "bought", "sell", "selling", "sold", "ape", "aping", "send",
    "sending", "sent", "entry", "market", "profit", "profits", "loss", "losses",
    "pump", "pumping", "dump", "dumping", "moon", "mooning", "dip", "chart",
    "coin", "coins", "token", "tokens", "money", "position", "bag", "bags",
    "snipe", "sniping", "launch", "dollar", "dollars", "bucks", "buck", "cents",
    "worth", "ticker", "called", "named", "size", "full", "yolo", "exit",
}

# "X looks good", "X is pumping" — ticker precedes the trigger
_POSTFIX_TRIGGERS = [
    ("looks", "good"), ("looks", "bullish"), ("looking", "good"), ("is", "pumping"),
    ("is", "sending"), ("is", "mooning"), ("is", "ripping"), ("could", "be", "big"),
    ("to", "the", "moon"),
]

_TOKEN_RE = re.compile(r"\$\s?[A-Za-z][A-Za-z0-9]*|[A-Za-z0-9']+")
_TICKER_RE = re.compile(r"^[a-z][a-z0-9]{1,14}$")  # 2-15 chars, starts with a letter


@dataclass(frozen=True)
class Token:
    norm: str      # lowercase, no punctuation or apostrophes: "i'm" → "im"
    raw: str       # as Whisper wrote it (for case checks)
    dollar: bool   # had a "$" prefix


@dataclass(frozen=True)
class _Match:
    type: str
    phrase: str
    start: int     # token index (inclusive)
    end: int       # token index (exclusive)
    dist: int
    matched: str


def normalize(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'")
    return " ".join(t.norm for t in tokenize(text))


def tokenize(text: str) -> list[Token]:
    text = text.replace("’", "'").replace("‘", "'")
    tokens = []
    for m in _TOKEN_RE.finditer(text):
        raw = m.group(0)
        dollar = raw.startswith("$")
        word = raw.lstrip("$").strip().replace("'", "")
        if word:
            tokens.append(Token(norm=word.lower(), raw=word, dollar=dollar))
    return tokens


def edit_distance(a: str, b: str, limit: int) -> int:
    """Levenshtein with early exit: returns limit+1 if the distance exceeds limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def _max_edits(phrase: str) -> int:
    if phrase not in _FUZZY_OK:
        return 0                       # short/ambiguous: exact only
    if " " not in phrase:
        return 0                       # single-word: exact only
    n = len(phrase.replace(" ", ""))
    if n <= 5:
        return 0                       # "im in", "im out", "sold"
    if n <= 9:
        return 1                       # "aping in", "ape this"
    return 2                           # "buying this", "taking profit"


class KeywordSpotter:
    # No "let's go" / "i'm in" / "yolo" / "full send": on streams that is idle
    # chatter, not a buy. Short ones ("going in", "getting in") are exact-only.
    BUY_PHRASES = [
        "buying this", "aping in", "aping into", "ape this", "ape into",
        "sending it", "going in", "buying right now", "just bought",
        "i'm buying", "entry here",
        "sniping this", "market buy", "buying some",
        "getting in", "let me buy", "gonna buy", "size in",
    ]

    WATCH_PHRASES = [
        "looking at", "watching this", "interesting",
        "check this out", "this looks good", "could be big",
        "keep an eye on", "might buy", "thinking about",
        "what do you think", "should i buy",
    ]

    # Only explicit multi-word phrases about the streamer's OWN sell. "closing", "exit",
    # "get out", bare "sold"/"selling" are everyday speech ("sold out", "get out of here").
    SELL_PHRASES = [
        "sold it", "i sold", "just sold", "selling now", "selling it",
        "selling this", "taking profit", "taking profits", "take profits",
        "i'm out", "cutting losses", "dumping it", "selling my bag",
    ]

    def __init__(self):
        # (type, normalized phrase, tokens, max_edits); duplicates like "i'm in"/"im in"
        # collapse after normalization
        seen: set[tuple[str, str]] = set()
        self._phrases: list[tuple[str, str, int, int]] = []
        for kind, phrases in (
            ("buy", self.BUY_PHRASES),
            ("watch", self.WATCH_PHRASES),
            ("sell", self.SELL_PHRASES),
        ):
            for p in phrases:
                norm = normalize(p)
                if (kind, norm) in seen:
                    continue
                seen.add((kind, norm))
                self._phrases.append((kind, norm, len(norm.split()), _max_edits(norm)))

    # ------------------------------------------------------------------ phrases

    def _find_matches(self, tokens: list[Token], boundary: int = 0) -> list[_Match]:
        norms = [t.norm for t in tokens]
        joined = " " + " ".join(norms) + " "
        negative_ctx = any(n in _NEGATIVE_WORDS for n in norms) or any(
            f" {p} " in joined for p in _NEGATIVE_PHRASES)
        candidates: list[_Match] = []
        for kind, phrase, n, max_ed in self._phrases:
            # windows of n-1..n+1 tokens: Whisper may merge/split words
            sizes = (n,) if max_ed == 0 else tuple(s for s in (n - 1, n, n + 1) if s >= 1)
            for size in sizes:
                for i in range(len(norms) - size + 1):
                    if i + size <= boundary:
                        continue       # entirely inside context: already handled
                    window = " ".join(norms[i:i + size])
                    d = edit_distance(window, phrase, max_ed)
                    if d <= max_ed:
                        candidates.append(_Match(kind, phrase, i, i + size, d, window))

        # Best non-overlapping: more exact first, then longer phrase
        candidates.sort(key=lambda m: (m.dist, -len(m.phrase), m.start))
        taken: list[_Match] = []
        for m in candidates:
            if any(m.start < t.end and t.start < m.end for t in taken):
                continue
            if any(_is_negation(norms, k)
                   for k in range(max(0, m.start - _NEGATION_WINDOW), m.start)):
                continue
            if m.type == "buy" and negative_ctx:
                continue
            taken.append(m)
        taken.sort(key=lambda m: m.start)
        return taken

    def spot(self, transcript_text: str, context: str = "") -> list[dict]:
        """Text → [{type, phrase, confidence, matched}].

        context is the tail of the previous chunk: catches phrases split by a chunk
        boundary ("aping" | "into pepe"). Phrases lying entirely within context
        are not returned (they were reported with the previous chunk).
        """
        ctx = tokenize(context) if context else []
        tokens = ctx + tokenize(transcript_text)
        # Repeated phrase ("let's go! let's go!") → one signal with the best confidence
        best: dict[tuple[str, str], dict] = {}
        for m in self._find_matches(tokens, boundary=len(ctx)):
            conf = _CONF_BY_DIST[m.dist]
            key = (m.type, m.phrase)
            if key not in best or conf > best[key]["confidence"]:
                best[key] = {"type": m.type, "phrase": m.phrase, "confidence": conf, "matched": m.matched}
        return list(best.values())

    # ------------------------------------------------------------------ tickers

    @staticmethod
    def _is_ticker(tok: Token) -> bool:
        return (
            bool(_TICKER_RE.match(tok.norm))
            and tok.norm not in STOPWORDS
            and tok.norm not in _NUMBER_WORDS
        )

    @staticmethod
    def _caps_boost(tok: Token) -> float:
        # Whisper writes tickers in caps ("WIF", "PEPE"): a strong cue
        return 0.15 if len(tok.raw) >= 2 and tok.raw.isupper() else 0.0

    def _ticker_after(self, tokens: list[Token], pos: int) -> int | None:
        """First plausible ticker within ≤5 tokens after the phrase.
        Skips fillers and amounts: "buying like 2 sol of PEPE" → PEPE."""
        amount_seen = False
        for j in range(pos, min(pos + 5, len(tokens))):
            t = tokens[j].norm
            nxt = tokens[j + 1].norm if j + 1 < len(tokens) else ""
            if t.isdigit() or t in _NUMBER_WORDS or re.fullmatch(r"\d+k", t):
                amount_seen = True
                continue
            if t in _AMOUNT_UNITS and (amount_seen or nxt in ("of", "worth")):
                continue
            if t in _FILLERS:
                continue
            return j if self._is_ticker(tokens[j]) else None
        return None

    def extract_tickers(self, transcript_text: str, context: str = "") -> list[dict]:
        """Text → [{ticker, confidence, source}], sorted by confidence descending."""
        ctx = tokenize(context) if context else []
        tokens = ctx + tokenize(transcript_text)
        boundary = len(ctx)
        norms = [t.norm for t in tokens]
        found: dict[str, dict] = {}

        def add(idx: int | None, base: float, source: str) -> None:
            if idx is None or idx < boundary or idx >= len(tokens):
                return
            tok = tokens[idx]
            if not self._is_ticker(tok):
                return
            conf = min(0.95, base + self._caps_boost(tok))
            key = tok.norm.upper()
            cur = found.get(key)
            if cur is None:
                found[key] = {"ticker": key, "confidence": round(conf, 2), "sources": [source]}
            else:
                if source not in cur["sources"]:
                    cur["sources"].append(source)
                    conf = max(conf, cur["confidence"]) + 0.05  # confirmed by 2 patterns
                cur["confidence"] = round(min(0.95, max(conf, cur["confidence"])), 2)

        for i, tok in enumerate(tokens):
            # "$PEPE"
            if tok.dollar:
                add(i, 0.9, "dollar")
            # "dollar sign pepe" / "dollar pepe"
            if tok.norm == "dollar":
                if i + 2 < len(tokens) and norms[i + 1] == "sign":
                    add(i + 2, 0.85, "dollar_sign")
                elif i + 1 < len(tokens) and not norms[i + 1].isdigit():
                    add(i + 1, 0.6, "dollar_word")
            # "ticker is pepe", "it's called pepe"
            if tok.norm in ("ticker", "called", "named"):
                j = i + 1
                if j < len(tokens) and norms[j] in ("is", "was"):
                    j += 1
                add(j, 0.8, "ticker_word")
            # "pepe coin", "wif token"
            if tok.norm in ("coin", "token") and i > 0:
                add(i - 1, 0.7, "coin_word")
            # "pepe looks good", "bonk is pumping"
            for trig in _POSTFIX_TRIGGERS:
                if tuple(norms[i + 1:i + 1 + len(trig)]) == trig:
                    add(i, 0.55, "postfix")

            # "buying bonk", "bought some WIF"
            if tok.norm in _TICKER_VERBS:
                add(self._ticker_after(tokens, i + 1), _TICKER_VERBS[tok.norm], "after_verb")

        # "aping into pepe", "buying this WIF", "looking at bonk"
        base_by_type = {"buy": 0.65, "sell": 0.6, "watch": 0.55}
        for m in self._find_matches(tokens):
            if m.phrase in _NO_OBJECT_PHRASES:
                continue
            add(self._ticker_after(tokens, m.end), base_by_type[m.type], f"after_{m.type}")

        return sorted(
            ({"ticker": v["ticker"], "confidence": v["confidence"], "source": "+".join(v["sources"])}
             for v in found.values()),
            key=lambda d: -d["confidence"],
        )
