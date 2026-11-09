#!/usr/bin/env python3
"""End-to-end smoke test of the module wiring — no stream, GPU, or ML models.

Runs the real signal/orchestrator.Orchestrator (aggregator → scorer → dedup → IPC)
with fake capture / vision / audio, and checks that signals reach the executor
in a format it accepts.

    python scripts/smoke_test.py               # in-process mock executor (offline)
    python scripts/smoke_test.py --executor    # real Rust binary, DRY_RUN=true (needs RPC_URL / network)

Exit code 0 = OK.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "signal"))
sys.path.append(str(ROOT))

# Valid base58 pubkey (32 bytes). Not a pump.fun mint: the real executor will
# answer "bonding curve not found", which still proves the payload deserialized.
TEST_MINT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"


# --------------------------------------------------------------------------- fakes

class _Counter:
    value = 0


class FakeCapture:
    """Same interface as ingestion.capture.StreamCapture, without ffmpeg."""

    def __init__(self, url, video_queue, audio_queue, config):
        self.vq, self.aq = video_queue, audio_queue
        self.frames_dropped = _Counter()
        self.reconnects = _Counter()
        self._alive = False

    def start(self):
        self._alive = True
        now = time.time()
        for i in range(3):
            self.vq.put((now - (2 - i) * 0.1, b"\x00" * 16))
        self.aq.put((now, b"\x00" * 3200))

    def is_alive(self):
        return self._alive

    def stop(self, *a, **kw):
        self._alive = False


class FakeVision:
    """Mint on screen without a ticker — the case that used to break the executor."""

    def __init__(self, _cfg):
        pass

    def process_frame(self, frame, width, height, ts):
        return {"mints": [TEST_MINT], "platform": "photon",
                "actions": [{"action": "about_to_buy", "confidence": 0.9}], "timestamp": ts}


class FakeAudio:
    def __init__(self, _cfg):
        pass

    def process_chunk(self, pcm, sample_rate):
        return {"transcript": "ok guys aping in right now",
                "signals": [{"type": "buy", "phrase": "aping in", "confidence": 0.95}]}


# --------------------------------------------------------------------------- executors

def validate_wire(msg: dict) -> list[str]:
    """Mirror of the Rust executor's types::Signal contract."""
    errs = []
    if not isinstance(msg.get("mint"), str) or not msg["mint"]:
        errs.append("mint must be a non-empty string")
    if not isinstance(msg.get("ticker", ""), str):
        errs.append(f"ticker must be a string, got {msg.get('ticker')!r}")
    if msg.get("action") not in ("buy", "sell"):
        errs.append(f"action must be buy|sell, got {msg.get('action')!r}")
    for k in ("confidence", "timestamp"):
        if k in msg and not isinstance(msg[k], (int, float)):
            errs.append(f"{k} must be a number")
    for k in ("source", "context"):
        if k in msg and not isinstance(msg[k], str):
            errs.append(f"{k} must be a string")
    return errs


async def start_mock(path: str, received: list, errors: list):
    async def handle(reader, writer):
        while line := await reader.readline():
            msg = json.loads(line)
            received.append(msg)
            errs = validate_wire(msg)
            errors.extend(errs)
            resp = {"status": "error", "message": "; ".join(errs)} if errs else {"status": "ok", "bundle_id": "mock"}
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, path=path)


def start_real_executor(path: str) -> subprocess.Popen:
    binary = ROOT / "executor" / "target" / "release" / "mev-streamer-executor"
    if not binary.exists():
        sys.exit(f"executor binary not found: {binary} (run scripts/build.sh)")
    # Isolated from the user's .env: explicit config, cwd without .env, ephemeral wallet.
    rpc = os.getenv("SMOKE_RPC_URL", "https://api.mainnet-beta.solana.com")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
           "SOCKET_PATH": path, "DRY_RUN": "true", "RPC_URL": rpc, "WALLET_PRIVATE_KEY": "",
           "BUY_SOL": "0.01", "JITO_TIP": "0.0001"}
    proc = subprocess.Popen([str(binary)], cwd=tempfile.gettempdir(), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 20
    while not os.path.exists(path):
        if proc.poll() is not None or time.time() > deadline:
            out = proc.stdout.read() if proc.poll() is not None else ""
            proc.kill()
            sys.exit(f"executor did not start:\n{out}")
        time.sleep(0.1)
    return proc


# --------------------------------------------------------------------------- main

async def run(use_real: bool) -> int:
    from orchestrator import Orchestrator, OrchestratorConfig, parse_args, setup_logging

    sock = os.path.join(tempfile.gettempdir(), f"mevs-smoke-{os.getpid()}.sock")  # short: AF_UNIX ≤ 104 chars
    os.environ.update(STREAM_URL="https://example.invalid/fake", DRY_RUN="false",
                      SIGNAL_THRESHOLD="0.7", IPC_SOCKET_PATH=sock)
    os.environ.pop("HELIUS_API_KEY", None)
    setup_logging(os.getenv("LOG_LEVEL", "WARNING"))

    received, errors = [], []
    server = proc = None
    if use_real:
        proc = start_real_executor(sock)
    else:
        server = await start_mock(sock, received, errors)

    try:
        cfg = OrchestratorConfig.from_env(parse_args([]))
        orch = Orchestrator(cfg, pipelines=(FakeCapture, FakeVision, FakeAudio))

        async def stopper():
            for _ in range(100):
                await asyncio.sleep(0.1)
                if orch.stats.sent + orch.stats.rejected + orch.stats.send_failed and not orch._dispatch_tasks:
                    break
            orch._stop.set()

        asyncio.get_running_loop().create_task(stopper())
        await orch.run()
    finally:
        if server:
            server.close()
        if proc:
            proc.send_signal(2)  # SIGINT → executor prints stats and removes the socket
            try:
                out, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
            errors += [l for l in out.splitlines() if "bad signal JSON" in l]
            print("\n--- executor log ---\n" + out.strip())
        if os.path.exists(sock):
            os.unlink(sock)

    s = orch.stats
    ok = s.frames > 0 and s.audio_chunks > 0 and (s.sent + s.rejected) >= 1 and not errors
    print(f"\nframes={s.frames} audio={s.audio_chunks} signals={s.signals} sent={s.sent} "
          f"rejected={s.rejected} failed={s.send_failed} wire_errors={errors or 0}")
    if received:
        print("wire sample:", json.dumps(received[0], ensure_ascii=False))
    print("SMOKE TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--executor", action="store_true", help="use the real Rust executor (DRY_RUN=true)")
    a = p.parse_args()
    return asyncio.run(run(a.executor))


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    sys.exit(main())
