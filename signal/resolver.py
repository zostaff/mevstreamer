"""Тикер → mint address.

Цепочка источников:
  1. Jupiter Tokens API v2 (бесплатно, без ключа):
       GET https://lite-api.jup.ag/tokens/v2/search?query=PEPE
     Старый https://token.jup.ag/strict Jupiter отключил — он больше не отвечает.
  2. DexScreener search (бесплатно) — ловит свежие pump.fun токены, которых
     ещё нет в Jupiter.
  3. Helius DAS (если есть ключ): у DAS нет поиска по символу, поэтому он
     используется для проверки кандидата из DexScreener — getAsset по mint,
     символ on-chain должен совпасть с тикером.

Выбор среди одноимённых токенов (копикаты — норма для мемкоинов):
  verified → наибольшая ликвидность. Это эвристика: ticker-only резолв
  в принципе может указать не на тот токен, который имеет в виду стример.
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
    """У части токенов символ записан как "$WIF" — сравниваем без "$"."""
    return str(symbol or "").strip().lstrip("$").upper()


class TokenResolver:
    def __init__(
        self,
        helius_api_key: str | None = None,
        timeout_sec: float = 2.5,
        cache_ttl_sec: float = 3600.0,
        negative_ttl_sec: float = 60.0,
        min_liquidity_usd: float = 0.0,
    ):
        self.helius_api_key = helius_api_key or None
        self.timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self.cache_ttl_sec = cache_ttl_sec
        # Негативный кеш короткий: свежий токен может появиться через минуту
        self.negative_ttl_sec = negative_ttl_sec
        self.min_liquidity_usd = min_liquidity_usd
        self._cache: dict[str, tuple[str | None, float, str]] = {}  # TICKER → (mint, ts, source)
        self._inflight: dict[str, asyncio.Future] = {}
        self._session: aiohttp.ClientSession | None = None
        self.last_source: str | None = None

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
        ticker = ticker.strip().lstrip("$").upper()
        if not ticker:
            return None
        hit = self.cached(ticker)
        if hit is not None:
            self.last_source = f"cache:{hit[1]}"
            return hit[0]
        # Параллельные запросы одного тикера — один HTTP-поход
        task = self._inflight.get(ticker)
        if task is None:
            task = asyncio.create_task(self._resolve_and_cache(ticker))
            self._inflight[ticker] = task
            task.add_done_callback(lambda _t, k=ticker: self._inflight.pop(k, None))
        return await asyncio.shield(task)

    async def _resolve_and_cache(self, ticker: str) -> str | None:
        mint, source = await self._resolve(ticker)
        self._cache[ticker] = (mint, time.time(), source)
        self.last_source = source
        return mint

    async def _resolve(self, ticker: str) -> tuple[str | None, str]:
        mint = await self._safe(self._jupiter, ticker)
        if mint:
            return mint, "jupiter"
        mint = await self._safe(self._dexscreener, ticker)
        if mint:
            if self.helius_api_key:
                symbol = await self._safe(self._helius_symbol, mint)
                if symbol is not None and _sym(symbol) != ticker:
                    log.warning("helius: %s symbol=%r ≠ $%s — отклонено", mint, symbol, ticker)
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
        matches = [
            t for t in tokens or []
            if _sym(t.get("symbol")) == ticker
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
        best: tuple[float, str] | None = None
        for pair in (data or {}).get("pairs") or []:
            base = pair.get("baseToken") or {}
            if pair.get("chainId") != "solana" or _sym(base.get("symbol")) != ticker:
                continue
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            if liq >= self.min_liquidity_usd and (best is None or liq > best[0]):
                best = (liq, base.get("address"))
        return best[1] if best else None

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
                mint = await r.ticker_to_mint(t)
                print(f"${t.upper():10} → {mint}  [{r.last_source}, {(time.perf_counter() - t0) * 1000:.0f}ms]")
        finally:
            await r.close()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main(sys.argv[1:] or ["BONK"]))
