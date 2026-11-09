"""Repair of OCR-corrupted mint addresses.

OCR routinely confuses similar glyphs: `1→I`, `2→Z`, `5→S`, `o→0`… A corrupted
address fails the base58/32-byte check, and the strongest signal (a mint on
screen) is lost. Example from a real EasyOCR run:

    9iPGa52e8Fhxs496SK1HEvtqPMb64J71TkKnz2Vnpump   (on screen)
    9iPGa52e8Fhxs496SKIHEvtqPMb64J71TkKnzZVnpump   (OCR)

Approach:
1. near-address: a 40–46 char token ending in `pump` (pump.fun vanity),
   invalid as-is;
2. candidates: mandatory substitution of non-base58 chars (`I`, `l`, `O`, `0`, `|`)
   + up to MAX_SWAPS swaps of look-alike chars; filter "decodes to 32 bytes"
   (weak: ~30% of random 44-char strings pass — hence step 3);
3. mainnet verification: one `getMultipleAccounts` — does a mint exist
   owned by SPL Token / Token-2022. The result is cached by raw string,
   so an address repeated across frames is checked once.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import ssl
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("vision.mint_repair")

BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_IDX = {c: i for i, c in enumerate(BASE58)}

# Non-base58 chars → what they usually were on screen.
FORCED = {
    "I": "1i", "l": "1i", "|": "1", "O": "oDQ", "0": "oDQ",
}
# Base58 chars that OCR confuses with each other (symmetric).
_PAIRS = [("Z", "2"), ("z", "2"), ("S", "5"), ("s", "5"), ("B", "8"), ("G", "6"),
          ("g", "9"), ("q", "9"), ("b", "6"), ("T", "7"), ("A", "4"), ("u", "v"),
          ("U", "V"), ("n", "m")]
SWAPS: dict[str, str] = {}
for a, b in _PAIRS:
    if b is None:
        continue
    SWAPS[a] = SWAPS.get(a, "") + b
    SWAPS[b] = SWAPS.get(b, "") + a

MAX_SWAPS = 2
MAX_CANDIDATES = 1500
TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}


def decodes_to_32(s: str) -> bool:
    if not 32 <= len(s) <= 44 or any(c not in _IDX for c in s):
        return False
    n = 0
    for c in s:
        n = n * 58 + _IDX[c]
    zeros = len(s) - len(s.lstrip("1"))
    return zeros + (n.bit_length() + 7) // 8 == 32


def is_near_mint(token: str) -> bool:
    """Looks like an OCR-corrupted pump.fun mint (not an arbitrary word)."""
    t = token.strip(".,:;()[]{}<>\"'`")
    if not 40 <= len(t) <= 46 or not t.lower().endswith("pump"):
        return False
    bad = sum(c not in _IDX for c in t)
    return bad <= 4 and not decodes_to_32(t)


def candidate_levels(token: str, max_swaps: int = MAX_SWAPS, limit: int = MAX_CANDIDATES) -> list[list[str]]:
    """Repair candidates by level: [k=0 look-alike swaps, k=1, k=2 …]."""
    t = token.strip(".,:;()[]{}<>\"'`")
    body, suffix = t[:-4], "pump"  # vanity-address suffix is fixed
    forced = [i for i, c in enumerate(body) if c not in _IDX]
    if any(body[i] not in FORCED for i in forced):
        return []
    swappable = [i for i, c in enumerate(body) if c in SWAPS and i not in forced]
    seen: set[str] = set()
    levels: list[list[str]] = [[] for _ in range(max_swaps + 1)]
    total = 0
    for k in range(max_swaps + 1):
        for fixed in itertools.product(*(FORCED[body[i]] for i in forced)):
            base = list(body)
            for i, ch in zip(forced, fixed):
                base[i] = ch
            for pos in itertools.combinations(swappable, k):
                for repl in itertools.product(*(SWAPS[body[i]] for i in pos)):
                    s = base.copy()
                    for i, ch in zip(pos, repl):
                        s[i] = ch
                    cand = "".join(s) + suffix
                    if cand not in seen and decodes_to_32(cand):
                        seen.add(cand)
                        levels[k].append(cand)
                        total += 1
                        if total >= limit:
                            return levels
    return levels


def candidates(token: str, max_swaps: int = MAX_SWAPS, limit: int = MAX_CANDIDATES) -> list[str]:
    return [c for lvl in candidate_levels(token, max_swaps, limit) for c in lvl]


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # python.org builds don't see the system root CAs
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class MintVerifier:
    """Cached on-chain candidate verification. Thread-safe (vision runs in to_thread)."""

    BATCH = 100  # getMultipleAccounts limit
    NEG_TTL = 30.0  # "nothing found" — recheck later (the token may have just been created)

    def __init__(self, rpc_url: str | None = None, timeout: float = 2.0):
        url = rpc_url or os.getenv("RPC_URL") or ""
        if not url or "YOUR_KEY" in url:  # placeholder from .env.example
            url = "https://api.mainnet-beta.solana.com"
        self.rpc_url = url
        self._down_until = 0.0  # after an RPC error, don't hit it on every frame
        self.timeout = timeout
        self._ctx = _ssl_context()
        self._cache: dict[str, tuple[str | None, float]] = {}
        self._lock = threading.Lock()

    def _existing_mints(self, keys: list[str]) -> list[str]:
        """Parallel getMultipleAccounts, 100 keys each."""
        chunks = [keys[i:i + self.BATCH] for i in range(0, len(keys), self.BATCH)]
        if len(chunks) <= 1:
            return self._batch(chunks[0]) if chunks else []
        with ThreadPoolExecutor(max_workers=min(8, len(chunks))) as pool:
            return [k for part in pool.map(self._batch, chunks) for k in part]

    def _batch(self, chunk: list[str]) -> list[str]:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                           "params": [chunk, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}]}).encode()
        req = urllib.request.Request(self.rpc_url, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
            vals = json.load(r)["result"]["value"]
        return [k for k, v in zip(chunk, vals) if v and v.get("owner") in TOKEN_PROGRAMS]

    def resolve(self, raw: str) -> str | None:
        """Repaired mint for a raw OCR string, or None."""
        now = time.monotonic()
        if now < self._down_until:
            return None
        with self._lock:
            hit = self._cache.get(raw)
        if hit and (hit[0] is not None or now - hit[1] < self.NEG_TTL):
            return hit[0]
        # By level: fewest swaps first; the first unambiguous match wins.
        mint, n, t0 = None, 0, time.perf_counter()
        # Small levels are merged into one batch: one fewer RPC round-trip.
        stages: list[list[str]] = []
        for level in candidate_levels(raw):
            if stages and len(stages[-1]) + len(level) <= self.BATCH:
                stages[-1] += level
            else:
                stages.append(list(level))
        for level in stages:
            if not level:
                continue
            n += len(level)
            try:
                found = self._existing_mints(level)
            except Exception as e:  # a network error is no reason to crash vision
                self._down_until = time.monotonic() + 5.0
                log.warning("mint repair: RPC failed (%s) — pausing 5s", e)
                return None
            if len(found) == 1:
                mint = found[0]
                log.info("mint repair: %s → %s (%d candidates, %.0fms)", raw, mint, n, (time.perf_counter() - t0) * 1000)
                break
            if len(found) > 1:  # ambiguous → don't guess
                log.warning("mint repair: %s ambiguous (%d on-chain matches) — skipped", raw, len(found))
                break
        with self._lock:
            self._cache[raw] = (mint, now)
            if len(self._cache) > 2000:
                self._cache.clear()
        return mint
