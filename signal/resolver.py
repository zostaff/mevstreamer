"""Ticker → mint address.

Source chain:
  1. Jupiter Tokens API v2 (free, no key):
       GET https://lite-api.jup.ag/tokens/v2/search?query=PEPE
     Jupiter shut down the old https://token.jup.ag/strict; it no longer responds.
  2. DexScreener search (free): catches fresh pump.fun tokens not
     yet in Jupiter.
  3. Helius DAS (if a key is set): DAS has no symbol search, so it is
     used to verify the DexScreener candidate: getAsset by mint,
     the on-chain symbol must match the ticker.

Choosing among same-name tokens (copycats are the norm for memecoins):
  verified → highest liquidity. This is a heuristic: a ticker-only resolve
  can in principle point at a different token than the streamer means,
  so by default the orchestrator does NOT send ticker-only BUYs (ALLOW_TICKER_ONLY).
  Only pump.fun mints (address ending in "pump") are accepted; the executor
  can only trade pump.fun curves anyway. Positive cache TTL is 60 s
  (the winning copycat changes within an hour).
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

log = logging.getLogger("signal.resolver")

JUP_SEARCH_URL = "https://lite-api.jup.ag/tokens/v2/search"
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/"


def _sym(symbol) -> str:
    """Some tokens store the symbol as "$WIF"; compare without the "$"."""
    return str(symbol or "").strip().lstrip("$").upper()


def _is_pump_mint(mint) -> bool:
    """pump.fun mints (vanity suffix "pump") are the only thing the executor trades."""
    return isinstance(mint, str) and mint.endswith("pump")


def pick_by_mcap(pairs: list[dict], term: str, mcap_usd: float, tol: float = 0.25) -> tuple[str | None, str]:
    """DexScreener pairs → the single token with symbol/name `term` and market cap
    within ±tol of `mcap_usd`. A token's market cap is taken from its most liquid pair."""
    term = _sym(term)
    tokens: dict[str, tuple[float, float]] = {}  # mint → (liq, mcap) of the best pair
    for p in pairs:
        base = p.get("baseToken") or {}
        if p.get("chainId") != "solana":
            continue
        if _sym(base.get("symbol")) != term and _sym(base.get("name")) != term:
            continue
        mint = base.get("address")
        mcap = float(p.get("marketCap") or p.get("fdv") or 0)
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if not mint or mcap <= 0:
            continue
        if mint not in tokens or liq > tokens[mint][0]:
            tokens[mint] = (liq, mcap)
    if not tokens:
        return None, "no-token-with-this-symbol"
    near = [(abs(mc - mcap_usd) / mcap_usd, m) for m, (_, mc) in tokens.items() if abs(mc - mcap_usd) <= tol * mcap_usd]
    if not near:
        return None, f"no-mcap-match ({len(tokens)} tokens named ${term})"
    if len(near) > 1:
        return None, f"ambiguous ({len(near)} tokens within ±{tol:.0%})"
    return near[0][1], f"unique of {len(tokens)} ${term} tokens, mcap off {near[0][0]:.0%}"


class TokenResolver:
    def __init__(
        self,
        helius_api_key: str | None = None,
        timeout_sec: float = 2.5,
        cache_ttl_sec: float = 60.0,
        negative_ttl_sec: float = 60.0,
        min_liquidity_usd: float = 0.0,
    ):
        self.helius_api_key = helius_api_key or None
        self.timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self.cache_ttl_sec = cache_ttl_sec
        # Negative cache is short: a fresh token may appear a minute later
        self.negative_ttl_sec = negative_ttl_sec
        self.min_liquidity_usd = min_liquidity_usd
        self._cache: dict[str, tuple[str | None, float, str]] = {}  # TICKER → (mint, ts, source)
        self._inflight: dict[str, asyncio.Future] = {}
        self._session: aiohttp.ClientSession | None = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout, headers={"User-Agent": "mev-streamer-signal/1.0"}
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def cached(self, ticker: str) -> tuple[str | None, str] | None:
        entry = self._cache.get(ticker.upper().lstrip("$"))
        if not entry:
            return None
        mint, ts, source = entry
        ttl = self.cache_ttl_sec if mint else self.negative_ttl_sec
        return (mint, source) if time.time() - ts < ttl else None

    async def ticker_to_mint(self, ticker: str) -> str | None:
        return (await self.resolve_ticker(ticker))[0]

    async def resolve_ticker(self, ticker: str) -> tuple[str | None, str]:
        """→ (mint | None, source). source is returned per call (not a shared attribute)
        so concurrent resolves don't overwrite each other's source."""
        ticker = ticker.strip().lstrip("$").upper()
        if not ticker:
            return None, "empty"
        hit = self.cached(ticker)
        if hit is not None:
            return hit[0], f"cache:{hit[1]}"
        # Concurrent lookups of one ticker share a single HTTP request
        task = self._inflight.get(ticker)
        if task is None:
            task = asyncio.create_task(self._resolve_and_cache(ticker))
            self._inflight[ticker] = task
            task.add_done_callback(lambda _t, k=ticker: self._inflight.pop(k, None))
        return await asyncio.shield(task)

    async def _resolve_and_cache(self, ticker: str) -> tuple[str | None, str]:
        mint, source = await self._resolve(ticker)
        self._cache[ticker] = (mint, time.time(), source)
        return mint, source

    async def _resolve(self, ticker: str) -> tuple[str | None, str]:
        mint = await self._safe(self._jupiter, ticker)
        if mint:
            return mint, "jupiter"
        mint = await self._safe(self._dexscreener, ticker)
        if mint:
            if self.helius_api_key:
                symbol = await self._safe(self._helius_symbol, mint)
                if symbol is not None and _sym(symbol) != ticker:
                    log.warning("helius: %s symbol=%r ≠ $%s — rejected", mint, symbol, ticker)
                    return None, "helius-mismatch"
                return mint, "dexscreener+helius" if symbol else "dexscreener"
            return mint, "dexscreener"
        return None, "not-found"

    async def _safe(self, fn, arg):
        try:
            return await fn(arg)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as e:
            log.warning("%s(%s) failed: %s", fn.__name__.lstrip("_"), arg, e or type(e).__name__)
            return None

    async def _jupiter(self, ticker: str) -> str | None:
        session = await self._http()
        async with session.get(JUP_SEARCH_URL, params={"query": ticker}) as r:
            r.raise_for_status()
            tokens = await r.json(content_type=None)
        if not isinstance(tokens, list):  # API errors come back as an object {"error": ...}
            raise ValueError(f"unexpected Jupiter reply: {str(tokens)[:120]}")
        matches = [
            t for t in tokens
            if isinstance(t, dict) and _is_pump_mint(t.get("id"))
            and _sym(t.get("symbol")) == ticker
            and float(t.get("liquidity") or 0) >= self.min_liquidity_usd
        ]
        if not matches:
            return None
        best = max(matches, key=lambda t: (bool(t.get("isVerified")), float(t.get("liquidity") or 0)))
        return best.get("id")

    async def _dexscreener(self, ticker: str) -> str | None:
        session = await self._http()
        async with session.get(DEXSCREENER_SEARCH_URL, params={"q": ticker}) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
        if not isinstance(data, dict):
            raise ValueError(f"unexpected DexScreener reply: {str(data)[:120]}")
        best: tuple[float, str] | None = None
        for pair in data.get("pairs") or []:
            if not isinstance(pair, dict):
                continue
            base = pair.get("baseToken") or {}
            if pair.get("chainId") != "solana" or _sym(base.get("symbol")) != ticker:
                continue
            if not _is_pump_mint(base.get("address")):
                continue
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            if liq >= self.min_liquidity_usd and (best is None or liq > best[0]):
                best = (liq, base.get("address"))
        return best[1] if best else None

    # ------------------------------------------------ ticker + market cap

    async def match_by_mcap(self, term: str, mcap_usd: float, tol: float = 0.25) -> tuple[str | None, str]:
        """Identify a token without an address: among all Solana tokens with symbol (or name)
        `term` keep the one whose current market cap is within ±tol of the on-screen one.

        Unambiguous answers only: 0 matches → None, ≥2 → None ("ambiguous").
        ALL same-name tokens are compared (not just pump), otherwise a pump copycat with
        a similar market cap could "win" over the real non-pump token.
        Returns (mint, reason); the mint may be non-pump (the executor will reject it).
        """
        term = _sym(term)
        if not term or mcap_usd <= 0:
            return None, "bad-input"
        try:
            pairs = await self._dex_pairs(term)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            return None, f"dexscreener failed: {e or type(e).__name__}"
        return pick_by_mcap(pairs, term, mcap_usd, tol)

    async def _dex_pairs(self, query: str) -> list[dict]:
        session = await self._http()
        async with session.get(DEXSCREENER_SEARCH_URL, params={"q": query}) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
        if not isinstance(data, dict):
            raise ValueError(f"unexpected DexScreener reply: {str(data)[:120]}")
        return [p for p in data.get("pairs") or [] if isinstance(p, dict)]

    async def _helius_symbol(self, mint: str) -> str | None:
        session = await self._http()
        body = {"jsonrpc": "2.0", "id": "resolver", "method": "getAsset", "params": {"id": mint}}
        async with session.post(HELIUS_RPC_URL, params={"api-key": self.helius_api_key}, json=body) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
        result = data.get("result") or {}
        symbol = (result.get("token_info") or {}).get("symbol") or (
            (result.get("content") or {}).get("metadata") or {}
        ).get("symbol")
        return symbol or None


if __name__ == "__main__":  # python signal/resolver.py BONK WIF
    import sys

    async def _main(tickers: list[str]) -> None:
        r = TokenResolver()
        try:
            for t in tickers:
                t0 = time.perf_counter()
                mint, source = await r.resolve_ticker(t)
                print(f"${t.upper():10} → {mint}  [{source}, {(time.perf_counter() - t0) * 1000:.0f}ms]")
        finally:
            await r.close()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main(sys.argv[1:] or ["BONK"]))
