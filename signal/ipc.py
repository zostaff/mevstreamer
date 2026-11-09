"""Unix domain socket client → Rust executor.

Protocol (NDJSON, one line = one message):

  → {"mint": "...", "ticker": "...", "action": "buy", "confidence": 0.85,
     "source": "both", "context": "OCR: mint on Photon + Audio: 'aping in'",
     "timestamp": 1699999999.123}\\n
  ← {"status": "ack"}\\n                      — accepted
  ← {"status": "error", "error": "..."}\\n    — rejected

{"ok": true/false} and bare "ack" / "ok" strings are also accepted.

A connection is opened per signal (UDS connect ≈ tens of µs) and there are
NO retries: after a drop post-write there is no way to tell whether the
executor ran the signal, and a repeated buy = a double buy.

Mock executor for debugging without Rust:
    python signal/ipc.py --mock
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass

log = logging.getLogger("signal.ipc")

IPC_SOCKET_PATH = "/tmp/mev-streamer.sock"


@dataclass(frozen=True)
class ExecutorResponse:
    ok: bool
    status: str
    error: str | None = None
    raw: dict | str | None = None
    latency_ms: float = 0.0


class ExecutorClient:
    def __init__(
        self,
        socket_path: str = IPC_SOCKET_PATH,
        connect_timeout: float = 1.0,
        response_timeout: float = 10.0,
        warn_interval_sec: float = 30.0,
    ):
        self.socket_path = socket_path
        self.connect_timeout = connect_timeout
        self.response_timeout = response_timeout
        self.warn_interval_sec = warn_interval_sec
        self._last_warn: dict[str, float] = {}

    def _warn(self, kind: str, msg: str, *args) -> None:
        """Rate-limited warning: don't spam every frame while the executor is down."""
        now = time.monotonic()
        if now - self._last_warn.get(kind, 0.0) >= self.warn_interval_sec:
            self._last_warn[kind] = now
            log.warning(msg, *args)

    async def send_signal(self, signal) -> ExecutorResponse | None:
        """Send a signal. None: not delivered (executor unavailable / IPC error).
        status="unknown": sent but no reply received (may have executed)."""
        payload = signal.to_dict() if hasattr(signal, "to_dict") else dict(signal)
        data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        t0 = time.perf_counter()

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path), self.connect_timeout
            )
        except (FileNotFoundError, ConnectionRefusedError):
            self._warn("down", "executor not running (%s) — signal not sent", self.socket_path)
            return None
        except (OSError, asyncio.TimeoutError) as e:
            self._warn("connect", "executor connect failed (%s): %s", self.socket_path, e or type(e).__name__)
            return None

        try:
            writer.write(data)
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), self.response_timeout)
        except asyncio.TimeoutError:
            # The signal is already written to the socket: the executor may have accepted and run it,
            # so this is NOT "failed" and must not be retried (double buy).
            log.warning("executor did not reply within %.1fs — status: unknown (executor may still execute)",
                        self.response_timeout)
            return ExecutorResponse(ok=False, status="unknown", error="reply timeout (executor may still execute)",
                                    latency_ms=(time.perf_counter() - t0) * 1000)
        except (ConnectionError, OSError) as e:
            log.warning("executor IPC error: %s", e)
            return None
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

        if not line:
            log.warning("executor closed the connection without replying")
            return None
        resp = self._parse(line.decode(errors="replace").strip())
        return ExecutorResponse(**{**resp.__dict__, "latency_ms": (time.perf_counter() - t0) * 1000})

    @staticmethod
    def _parse(text: str) -> ExecutorResponse:
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            ok = text.lower() in ("ack", "ok")
            return ExecutorResponse(ok=ok, status=text.lower() or "empty", error=None if ok else text, raw=text)
        if not isinstance(obj, dict):
            return ExecutorResponse(ok=False, status="invalid", error=f"unexpected response: {text}", raw=text)
        status = str(obj.get("status", "")).lower()
        ok = status in ("ack", "ok", "accepted") or obj.get("ok") is True
        err = None if ok else str(obj.get("error") or obj.get("message") or status or "error")
        return ExecutorResponse(ok=ok, status=status or ("ack" if ok else "error"), error=err, raw=obj)

    async def close(self) -> None:  # no persistent connection; kept for API symmetry
        return None


# --------------------------------------------------------------------------
# Mock executor
# --------------------------------------------------------------------------

async def run_mock_executor(socket_path: str = IPC_SOCKET_PATH) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                    print(f"[mock-executor] {msg.get('action')} mint={msg.get('mint')} "
                          f"ticker={msg.get('ticker')} conf={msg.get('confidence')} :: {msg.get('context')}")
                    resp = {"status": "ack"}
                except json.JSONDecodeError as e:
                    resp = {"status": "error", "error": f"bad json: {e}"}
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        finally:
            writer.close()

    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = await asyncio.start_unix_server(handle, path=socket_path)
    print(f"[mock-executor] listening on {socket_path}")
    try:
        async with server:
            await server.serve_forever()
    finally:
        if os.path.exists(socket_path):
            os.unlink(socket_path)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="IPC utils")
    p.add_argument("--mock", action="store_true", help="run a mock executor (acks everything)")
    p.add_argument("--socket", default=os.getenv("IPC_SOCKET_PATH", IPC_SOCKET_PATH))
    a = p.parse_args()
    if a.mock:
        try:
            asyncio.run(run_mock_executor(a.socket))
        except KeyboardInterrupt:
            pass
    else:
        p.print_help()
