"""Per-token signal deduplication within a cooldown."""

from __future__ import annotations

import time


class SignalDedup:
    """Key = action + (mint or $TICKER).

    action is part of the key so a sell right after a buy of the same token is
    not a duplicate. The window is not sliding: if the streamer "holds" a buy signal
    longer than cooldown_sec, it repeats, so the executor must be idempotent
    per mint (never open a second position on the same token).
    """

    def __init__(self, cooldown_sec: float = 60):
        self.cooldown_sec = float(cooldown_sec)
        self.seen: dict[str, float] = {}  # key → last_signal_timestamp
        self._last_cleanup = time.time()

    @staticmethod
    def key(signal) -> str | None:
        token = signal.mint or (f"${signal.ticker.upper()}" if signal.ticker else None)
        return f"{signal.action}:{token}" if token else None

    def is_duplicate(self, signal, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        key = self.key(signal)
        if key is None:
            return False
        last = self.seen.get(key)
        if last is not None and now - last < self.cooldown_sec:
            return True
        self.seen[key] = now
        if now - self._last_cleanup > self.cooldown_sec:
            self.cleanup(now)
        return False

    def cleanup(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        cutoff = now - 5 * self.cooldown_sec
        stale = [k for k, ts in self.seen.items() if ts < cutoff]
        for k in stale:
            del self.seen[k]
        self._last_cleanup = now
        return len(stale)
