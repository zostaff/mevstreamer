#!/usr/bin/env python3
"""Python stack entry point: stream → vision/audio → scoring → executor.

    python signal/orchestrator.py                     # mode from .env (DRY_RUN=true by default)
    python signal/orchestrator.py --monitor-only      # show signals only
    python signal/orchestrator.py --dry-run --stream twitch.tv/name --threshold 0.75

Live mode (real dispatch to the executor) is enabled only by DRY_RUN=false in
the environment/.env and without --dry-run / --monitor-only.

Only action=buy|sell with confidence >= threshold is sent to the executor;
watch signals are only logged.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import multiprocessing as mp
import os
import queue
import signal
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
# multiprocessing (spawn workers, resource_tracker) starts children via
# `python -c ...`, and with -c, cwd lands in sys.path[0]. Running from the root →
# the signal/ folder shadows stdlib `signal` in child processes.
# PYTHONSAFEPATH=1 (3.11+) drops cwd from sys.path; children inherit the env.
os.environ["PYTHONSAFEPATH"] = "1"
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
# Root goes at the END of sys.path, otherwise the root's signal/ folder shadows stdlib `signal`
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from aggregator import SignalAggregator, VisionObservation, normalize_audio, normalize_vision  # noqa: E402
from dedup import SignalDedup  # noqa: E402
from ipc import IPC_SOCKET_PATH, ExecutorClient  # noqa: E402
from resolver import TokenResolver  # noqa: E402
from scorer import Signal, SignalScorer, short_mint  # noqa: E402

from ingestion.config import CaptureConfig  # noqa: E402

log = logging.getLogger("signal")

# --------------------------------------------------------------------------
# Colors
# --------------------------------------------------------------------------

_COLOR = sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def green(t): return _c("32;1", t)
def yellow(t): return _c("33;1", t)
def red(t): return _c("31;1", t)
def cyan(t): return _c("36", t)
def dim(t): return _c("2", t)
def bold(t): return _c("1", t)


def _fmt_usd(v: float) -> str:
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"{v / div:.2f}{suf}"
    return f"{v:.0f}"


class _ColorFormatter(logging.Formatter):
    LEVEL = {logging.DEBUG: dim, logging.INFO: cyan, logging.WARNING: yellow,
             logging.ERROR: red, logging.CRITICAL: red}

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created)) + f".{int(record.msecs):03d}"
        lvl = self.LEVEL.get(record.levelno, str)(f"{record.levelname:<7}")
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return f"{dim(ts)} {lvl} {msg}"


def setup_logging(level: str) -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_ColorFormatter())
    logging.basicConfig(level=level.upper(), handlers=[h], force=True)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    # faster-whisper logs 2 INFO lines per chunk, httpx logs model downloads
    for noisy in ("faster_whisper", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError as e:
        raise ValueError(f"{name} must be a number, got {raw!r}") from e


def _helius_key_from_env() -> str | None:
    if key := os.getenv("HELIUS_API_KEY", "").strip():
        return key
    rpc = os.getenv("RPC_URL", "")
    if "helius" in rpc:
        key = (parse_qs(urlparse(rpc).query).get("api-key") or [""])[0]
        if key and key != "YOUR_KEY":
            return key
    return None


@dataclass
class OrchestratorConfig:
    capture: CaptureConfig
    signal_threshold: float = 0.7
    signal_cooldown_sec: float = 60.0
    helius_api_key: str | None = None
    dry_run: bool = True
    monitor_only: bool = False
    ipc_socket_path: str = IPC_SOCKET_PATH
    display_min_confidence: float = 0.3
    vision_window_sec: float = 3.0
    audio_window_sec: float = 10.0
    # ticker-only signal (no mint on screen) → ticker resolution easily lands
    # on a copycat/stale token. By default such BUY/SELL signals are only logged.
    allow_ticker_only: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def stream_url(self) -> str | None:
        return self.capture.stream_url

    def __getattr__(self, name: str):
        # VisionPipeline/AudioPipeline receive this config: expose
        # CaptureConfig fields (fps, width, height, ...) directly.
        capture = self.__dict__.get("capture")
        if capture is not None and hasattr(capture, name):
            return getattr(capture, name)
        raise AttributeError(name)

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> "OrchestratorConfig":
        capture = CaptureConfig.from_env()  # also loads .env from the root
        if args.stream:
            capture = replace(capture, stream_urls=[args.stream])
        threshold = args.threshold if args.threshold is not None else _env_float("SIGNAL_THRESHOLD", 0.7)
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"threshold must be in (0, 1], got {threshold}")
        return cls(
            capture=capture,
            signal_threshold=threshold,
            signal_cooldown_sec=_env_float("SIGNAL_COOLDOWN_SEC", 60.0),
            helius_api_key=_helius_key_from_env(),
            dry_run=args.dry_run or _env_bool("DRY_RUN", True),
            monitor_only=args.monitor_only,
            ipc_socket_path=os.getenv("IPC_SOCKET_PATH", IPC_SOCKET_PATH),
            allow_ticker_only=_env_bool("ALLOW_TICKER_ONLY", False),
        )


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------

@dataclass
class SessionStats:
    started: float = field(default_factory=time.time)
    frames: int = 0
    frames_skipped: int = 0
    audio_chunks: int = 0
    signals: int = 0
    buy_signals: int = 0
    tokens: set = field(default_factory=set)
    above_threshold: int = 0
    deduped: int = 0
    unresolved: int = 0
    would_execute: int = 0
    sent: int = 0
    rejected: int = 0
    send_failed: int = 0
    send_unknown: int = 0
    ticker_only_blocked: int = 0
    audio_stale_skipped: int = 0
    capture_restarts: int = 0
    pipeline_errors: int = 0
    latency: dict = field(default_factory=lambda: defaultdict(list))

    def lat(self, step: str, ms: float) -> None:
        bucket = self.latency[step]
        bucket.append(ms)
        if len(bucket) > 2000:
            del bucket[:1000]

    @staticmethod
    def _p(vals: list[float], q: float) -> float:
        s = sorted(vals)
        return s[min(len(s) - 1, int(q * len(s)))]

    def report(self) -> str:
        dur = time.time() - self.started
        lines = [
            "",
            bold("━━━━━━━━━━━━━━ SESSION STATS ━━━━━━━━━━━━━━"),
            f"  Duration:                  {dur / 60:.1f} min",
            f"  Frames / audio chunks:     {self.frames} / {self.audio_chunks}"
            + (f"  (skipped stale frames: {self.frames_skipped})" if self.frames_skipped else ""),
            f"  Signals detected:          {self.signals}",
            f"  Buy signals:               {self.buy_signals}",
            f"  Unique tokens:             {len(self.tokens)}",
            f"  Above threshold:           {self.above_threshold}  (deduped: {self.deduped}, unresolved: {self.unresolved})",
            f"  Would execute (dry-run):   {self.would_execute}",
            f"  Signals sent to executor:  {self.sent}"
            + (f"  (rejected: {self.rejected}, failed: {self.send_failed}, unknown: {self.send_unknown})"
               if self.rejected or self.send_failed or self.send_unknown else ""),
        ]
        if self.ticker_only_blocked:
            lines.append(f"  Ticker-only (not sent):    {self.ticker_only_blocked}")
        if self.audio_stale_skipped or self.capture_restarts:
            lines.append(f"  Stale audio skipped:       {self.audio_stale_skipped}   capture restarts: {self.capture_restarts}")
        if self.pipeline_errors:
            lines.append(f"  Pipeline errors:           {self.pipeline_errors}")
        if self.latency:
            lines.append("  Latency (avg / p95 ms):")
            for step in ("vision", "audio", "score", "resolve", "ipc", "e2e"):
                vals = self.latency.get(step)
                if vals:
                    lines.append(f"    {step:<8} {sum(vals) / len(vals):8.1f} / {self._p(vals, 0.95):8.1f}   (n={len(vals)})")
        lines.append(bold("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

def _load_pipelines():
    """Lazy import of the heavy pipelines (torch/easyocr/whisper).

    Under spawn (macOS) every capture worker re-executes this module as
    __mp_main__ — a module-level import would load the models into every worker.
    """
    from ingestion.capture import StreamCapture
    try:
        from vision.pipeline import VisionPipeline
        from audio.pipeline import AudioPipeline
    except ImportError as e:
        # vision/audio do `try: from .x … except ImportError: from x …`,
        # so the real cause (missing numpy/easyocr/…) is in __context__
        root = e
        while root.__context__ is not None and isinstance(root.__context__, ImportError):
            root = root.__context__
        raise SystemExit(f"failed to import vision/audio pipeline: {root} "
                         f"(pip install -r vision/requirements.txt -r audio/requirements.txt)") from e
    return StreamCapture, VisionPipeline, AudioPipeline


class Orchestrator:
    PRINT_REPEAT_SEC = 10.0      # reprint an unchanged signal at most this often
    PRINT_CONF_DELTA = 0.05      # ...or when confidence changes by this much
    STATUS_INTERVAL_SEC = 30.0
    CAPTURE_CHECK_SEC = 2.0
    CAPTURE_MAX_RESTARTS = 5         # consecutive, without a stable period
    CAPTURE_STABLE_SEC = 300.0       # uptime that resets the restart counter

    def __init__(self, config: OrchestratorConfig, pipelines=None):
        StreamCapture, VisionPipeline, AudioPipeline = pipelines or _load_pipelines()
        self.config = config
        qsize = config.capture.queue_maxsize
        self.video_queue = mp.Queue(maxsize=qsize)
        self.audio_queue = mp.Queue(maxsize=qsize)
        # StreamCapture is pickled into workers → give it a plain CaptureConfig
        self.capture = StreamCapture(config.stream_url, self.video_queue, self.audio_queue, config.capture)
        # Pipelines read their own settings (OCR_ENGINE, WHISPER_MODEL, ...) from env:
        # VisionPipeline(None) → VisionConfig.from_env(), AudioPipeline(None) → AudioConfig.from_env()
        self.vision = VisionPipeline(None)
        self.audio_pipeline = AudioPipeline(None)
        self.aggregator = SignalAggregator(config.vision_window_sec, config.audio_window_sec,
                                           audio_chunk_sec=float(config.capture.audio_chunk_sec))
        self.scorer = SignalScorer()
        self.dedup = SignalDedup(cooldown_sec=config.signal_cooldown_sec)
        self.resolver = TokenResolver(config.helius_api_key)
        self.executor = ExecutorClient(config.ipc_socket_path)
        self.dry_run = config.dry_run
        self.monitor_only = config.monitor_only
        self.threshold = config.signal_threshold

        self.stats = SessionStats()
        self._stop: asyncio.Event | None = None
        self._dispatch_tasks: set[asyncio.Task] = set()
        self._last_printed: dict[tuple, tuple[float, float]] = {}  # key → (confidence, ts)
        self._last_error_log = 0.0
        self._last_lag_warn = 0.0
        self._last_ticker_only_log: dict[str, float] = {}
        # Address-less identification: term ($TICKER or title) → (mint|None, mcap at identification, ts)
        self._mcap_ids: dict[str, tuple[str | None, float, float]] = {}
        self._mcap_inflight: set[str] = set()
        self.exit_code = 0

    # ---------------------------------------------------------------- run

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._on_signal)

        mode = "MONITOR-ONLY" if self.monitor_only else ("DRY-RUN" if self.dry_run else red("LIVE"))
        log.info("mode=%s threshold=%.2f cooldown=%ss stream=%s", mode, self.threshold,
                 int(self.dedup.cooldown_sec), self.config.stream_url)
        if not self.dry_run and not self.monitor_only:
            log.warning(red("LIVE MODE: signals >= %.2f are sent to the executor (%s)"),
                        self.threshold, self.config.ipc_socket_path)

        self.capture.start()
        tasks = [
            asyncio.create_task(self._video_loop(), name="video"),
            asyncio.create_task(self._audio_loop(), name="audio"),
            asyncio.create_task(self._housekeeping(), name="housekeeping"),
            asyncio.create_task(self._capture_watchdog(), name="capture-watchdog"),
        ]
        try:
            stop_waiter = asyncio.create_task(self._stop.wait())
            done, _ = await asyncio.wait([stop_waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t is not stop_waiter and t.exception():
                    self.exit_code = self.exit_code or 1
                    log.error("task %s crashed", t.get_name(), exc_info=t.exception())
            stop_waiter.cancel()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._dispatch_tasks:  # let in-flight dispatches finish
                await asyncio.wait(self._dispatch_tasks, timeout=5)
            log.info("stopping capture...")
            await asyncio.to_thread(self.capture.stop)
            await self.resolver.close()
            await self.executor.close()
            for p in (self.vision, self.audio_pipeline):
                close = getattr(p, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        log.exception("pipeline close failed")
            print(self.stats.report(), flush=True)

    def _on_signal(self) -> None:
        if self._stop.is_set():
            print(red("\nforce exit"), flush=True)
            os._exit(130)
        print(dim("\n^C — shutting down (press again to force quit)"), flush=True)
        self._stop.set()

    # ---------------------------------------------------------------- loops

    @staticmethod
    def _get_latest(q, timeout: float = 0.1) -> tuple[tuple | None, int]:
        """(latest frame, number of stale frames dropped). Runs in
        to_thread: unpickling a 2.7 MB frame must not block the event loop."""
        try:
            item = q.get(timeout=timeout)
        except queue.Empty:
            return None, 0
        skipped = 0
        while True:
            try:
                item = q.get_nowait()
                skipped += 1
            except queue.Empty:
                return item, skipped

    @staticmethod
    def _get_one(q, timeout: float = 0.1):
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None

    def _post_error(self, kind: str) -> None:
        """Post-processing error (normalize/aggregate/score): log with traceback
        and continue; otherwise a crashed loop takes down the whole session via FIRST_COMPLETED."""
        self.stats.pipeline_errors += 1
        log.exception("%s post-processing error — item skipped", kind)

    def _pipeline_error(self, kind: str) -> None:
        self.stats.pipeline_errors += 1
        now = time.monotonic()
        if now - self._last_error_log > 10:
            self._last_error_log = now
            log.exception("%s pipeline error (further errors logged at most once per 10s)", kind)

    async def _video_loop(self) -> None:
        while True:
            item, skipped = await asyncio.to_thread(self._get_latest, self.video_queue)
            self.stats.frames_skipped += skipped
            if item is None:
                continue
            ts, frame = item
            self.stats.frames += 1
            t0 = time.perf_counter()
            try:
                cap = self.config.capture
                raw = await asyncio.to_thread(self.vision.process_frame, frame, cap.width, cap.height, ts)
            except Exception:
                self._pipeline_error("vision")
                continue
            ms = (time.perf_counter() - t0) * 1000
            self.stats.lat("vision", ms)
            try:
                obs = normalize_vision(raw, ts)
                metrics = getattr(raw, "metrics", None) or {}
                if (obs is None or not obs.mints) and metrics.get("market_cap_usd"):
                    # token page with neither address nor $TICKER ("JIMOTHY" + MC): identify it too
                    obs = self._identify_by_mcap(obs or VisionObservation(timestamp=ts), metrics)
                    obs = None if obs.empty else obs
                if obs is not None:
                    log.debug("vision %.0fms: mints=%s tickers=%s platform=%s action=%s",
                              ms, [short_mint(m) for m in obs.mints], obs.tickers, obs.platform, obs.action)
                    self.aggregator.add_vision(obs)
                    self._evaluate(ts, {"vision": ms})
            except Exception:
                self._post_error("vision")

    async def _audio_loop(self) -> None:
        while True:
            item = await asyncio.to_thread(self._get_one, self.audio_queue)
            if item is None:
                continue
            ts, pcm = item
            self.stats.audio_chunks += 1
            # Whisper is lagging: the aggregator would drop a chunk older than the audio window
            # immediately, so skip Whisper on it and catch up to the live edge
            backlog = time.time() - ts
            if backlog > self.config.audio_window_sec:
                self.stats.audio_stale_skipped += 1
                self._warn_lag(backlog, skipped=True)
                reset = getattr(self.audio_pipeline, "reset", None)
                if callable(reset):
                    reset()  # previous chunk's context is no longer adjacent
                continue
            t0 = time.perf_counter()
            try:
                raw = await asyncio.to_thread(
                    self.audio_pipeline.process_chunk, pcm, self.config.capture.audio_sample_rate)
            except Exception:
                self._pipeline_error("audio")
                continue
            ms = (time.perf_counter() - t0) * 1000
            self.stats.lat("audio", ms)
            backlog = time.time() - ts
            if backlog > 3 * self.config.capture.audio_chunk_sec:
                self._warn_lag(backlog)
            try:
                obs = normalize_audio(raw, ts)
                if obs is not None:
                    log.debug('audio %.0fms: "%s" buy=%s tickers=%s', ms, obs.transcript[-80:], obs.buy_phrase, obs.tickers)
                    self.aggregator.add_audio(obs)
                    self._evaluate(ts, {"audio": ms})
            except Exception:
                self._post_error("audio")

    def _warn_lag(self, backlog: float, skipped: bool = False) -> None:
        now = time.monotonic()
        if now - self._last_lag_warn < 10.0:
            return
        self._last_lag_warn = now
        log.warning("audio lagging by %.1fs — Whisper can't keep up (model/device?)%s", backlog,
                    f"; skipping stale chunks ({self.stats.audio_stale_skipped} total)" if skipped else "")

    def _capture_dead(self) -> bool:
        procs = getattr(self.capture, "_processes", None)
        if procs:  # one of the two workers died: also a failure
            return any(not p.is_alive() for p in procs)
        return not self.capture.is_alive()

    async def _capture_watchdog(self) -> None:
        """Capture workers died (OOM/segfault/…) → restart with backoff;
        if they fail CAPTURE_MAX_RESTARTS times in a row → exit with code 3."""
        failures = 0
        backoff = 2.0
        last_restart = time.monotonic()
        while True:
            await asyncio.sleep(self.CAPTURE_CHECK_SEC)
            if self._stop.is_set():
                return
            if time.monotonic() - last_restart > self.CAPTURE_STABLE_SEC:
                failures, backoff = 0, 2.0
            if not self._capture_dead():
                continue
            failures += 1
            if failures > self.CAPTURE_MAX_RESTARTS:
                log.error(red("capture workers keep dying (%d restarts) — exiting"), self.CAPTURE_MAX_RESTARTS)
                self.exit_code = 3
                self._stop.set()
                return
            log.error("capture workers died — restart %d/%d in %.0fs",
                      failures, self.CAPTURE_MAX_RESTARTS, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            try:
                await asyncio.to_thread(self.capture.stop)
                self.capture.start()
                self.stats.capture_restarts += 1
            except Exception:
                log.exception("capture restart failed")
            last_restart = time.monotonic()

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(self.STATUS_INTERVAL_SEC)
            self.dedup.cleanup()
            cap = self.capture
            lat = self.stats.latency
            avg = lambda k: f"{sum(lat[k][-50:]) / len(lat[k][-50:]):.0f}ms" if lat.get(k) else "-"
            log.info(dim("status: frames=%d (dropped@capture=%d, skipped=%d) audio=%d reconnects=%d "
                         "vision≈%s audio≈%s signals=%d sent=%d alive=%s"),
                     self.stats.frames, cap.frames_dropped.value, self.stats.frames_skipped,
                     self.stats.audio_chunks, cap.reconnects.value, avg("vision"), avg("audio"),
                     self.stats.signals, self.stats.sent, cap.is_alive())

    # ---------------------------------------------------------------- identify

    MCAP_MATCH_FACTOR = 0.8   # weight of an identified mint: 0.8 × 0.50 = 0.40 (on-screen address: 0.50)
    MCAP_TOL = 0.25           # on-screen market cap vs current (stream delay, volatility)
    MCAP_ID_TTL = 60.0
    MCAP_NEG_TTL = 20.0

    def _identify_by_mcap(self, obs, metrics: dict):
        """No address, but a ticker (or page title) and market cap → identify
        the token by "name + market cap" (in the background) and inject the mint into the observation.
        Multi-token screens (several tickers) are skipped: there is no way to tell
        which ticker the market cap belongs to."""
        mcap = metrics.get("market_cap_usd")
        if not mcap:
            return obs
        if len(obs.tickers) == 1:
            term = obs.tickers[0]
        elif not obs.tickers and metrics.get("title"):
            term = str(metrics["title"]).upper()
        else:
            return obs
        now = time.monotonic()
        hit = self._mcap_ids.get(term)
        if hit:
            mint, ref, ts = hit
            if mint and now - ts < self.MCAP_ID_TTL and abs(mcap - ref) <= self.MCAP_TOL * ref:
                tickers = obs.tickers or (term,)
                return replace(obs, mints=(mint,), mint_conf=((mint, self.MCAP_MATCH_FACTOR),), tickers=tickers)
            if mint is None and now - ts < self.MCAP_NEG_TTL:
                return obs
        if term not in self._mcap_inflight:
            self._mcap_inflight.add(term)
            task = asyncio.create_task(self._resolve_mcap(term, float(mcap)))
            self._dispatch_tasks.add(task)
            task.add_done_callback(self._dispatch_tasks.discard)
        return obs

    async def _resolve_mcap(self, term: str, mcap: float) -> None:
        t0 = time.perf_counter()
        try:
            mint, why = await self.resolver.match_by_mcap(term, mcap, self.MCAP_TOL)
            ms = (time.perf_counter() - t0) * 1000
            self.stats.lat("resolve", ms)
            self._mcap_ids[term] = (mint, mcap, time.monotonic())
            if mint:
                log.info("identified $%s at MC $%s → %s (%s, %.0fms)", term, _fmt_usd(mcap), short_mint(mint), why, ms)
            else:
                log.info("cannot identify $%s at MC $%s: %s (%.0fms)", term, _fmt_usd(mcap), why, ms)
        except Exception:
            log.exception("mcap identify failed for %s", term)
        finally:
            self._mcap_inflight.discard(term)

    # ---------------------------------------------------------------- decision

    def _evaluate(self, capture_ts: float, step_ms: dict[str, float]) -> None:
        t0 = time.perf_counter()
        vis, aud = self.aggregator.snapshot()
        sig = self.scorer.score(vis, aud)
        score_ms = (time.perf_counter() - t0) * 1000
        self.stats.lat("score", score_ms)
        if sig is None:
            return
        sig = replace(sig, capture_ts=capture_ts)
        actionable = sig.confidence >= self.threshold and sig.action in ("buy", "sell")
        ticker_only = actionable and not sig.mint and not self.config.allow_ticker_only
        if ticker_only:
            actionable = False
        self._maybe_print(sig, {**step_ms, "score": score_ms}, actionable)
        if ticker_only:
            self.stats.ticker_only_blocked += 1
            key = f"{sig.action}:{sig.ticker}"
            now = time.monotonic()
            if now - self._last_ticker_only_log.get(key, -1e9) > 30:
                self._last_ticker_only_log[key] = now
                log.warning("ticker-only %s $%s conf=%.2f — no mint on screen, not sent "
                            "(ALLOW_TICKER_ONLY=false)", sig.action.upper(), sig.ticker, sig.confidence)
            return

        if self.monitor_only or not actionable:
            return
        self.stats.above_threshold += 1
        if self.dedup.is_duplicate(sig):
            self.stats.deduped += 1
            return
        task = asyncio.create_task(self._dispatch(sig))
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)

    async def _dispatch(self, sig: Signal) -> None:
        try:
            if not sig.mint and sig.ticker:
                t0 = time.perf_counter()
                mint, source = await self.resolver.resolve_ticker(sig.ticker)
                ms = (time.perf_counter() - t0) * 1000
                self.stats.lat("resolve", ms)
                if not mint:
                    self.stats.unresolved += 1
                    log.warning("$%s → mint not found (%.0fms) — signal skipped", sig.ticker, ms)
                    return
                log.info("resolved $%s → %s via %s (%.0fms)", sig.ticker, short_mint(mint), source, ms)
                sig = replace(sig, mint=mint, context=f"{sig.context} + resolved ${sig.ticker}")
                if self.dedup.is_duplicate(sig):  # this mint was already sent
                    self.stats.deduped += 1
                    return

            e2e = (time.time() - sig.capture_ts) * 1000 if sig.capture_ts else 0.0
            if self.dry_run:
                self.stats.would_execute += 1
                self.stats.lat("e2e", e2e)
                log.info(yellow("WOULD EXECUTE: ") + "%s mint=%s ticker=%s conf=%.2f src=%s e2e=%.0fms :: %s",
                         sig.action.upper(), sig.mint, sig.ticker, sig.confidence, sig.source, e2e, sig.context)
                return

            resp = await self.executor.send_signal(sig)
            e2e = (time.time() - sig.capture_ts) * 1000 if sig.capture_ts else 0.0
            if resp is None:
                self.stats.send_failed += 1
                return
            if resp.status == "unknown":
                self.stats.send_unknown += 1
                log.warning("executor reply timeout for %s %s — status unknown (executor may still execute)",
                            sig.action.upper(), short_mint(sig.mint))
                return
            self.stats.lat("ipc", resp.latency_ms)
            self.stats.lat("e2e", e2e)
            if resp.ok:
                self.stats.sent += 1
                log.info(green("SENT → executor: ") + "%s %s conf=%.2f [ipc %.1fms, e2e %.0fms]",
                         sig.action.upper(), short_mint(sig.mint), sig.confidence, resp.latency_ms, e2e)
            else:
                self.stats.rejected += 1
                log.warning("executor rejected %s: %s", short_mint(sig.mint), resp.error)
        except Exception:
            log.exception("dispatch failed for %s", sig.token_key)

    # ---------------------------------------------------------------- output

    def _maybe_print(self, sig: Signal, latencies: dict[str, float], actionable: bool) -> None:
        if sig.confidence < self.config.display_min_confidence and not actionable:
            return
        key = (sig.token_key, sig.action, sig.source)
        now = time.time()
        last = self._last_printed.get(key)
        if last and abs(sig.confidence - last[0]) < self.PRINT_CONF_DELTA and now - last[1] < self.PRINT_REPEAT_SEC:
            return
        first_time = last is None
        self._last_printed[key] = (sig.confidence, now)
        if len(self._last_printed) > 5000:
            self._last_printed.clear()

        if first_time:
            self.stats.signals += 1
            if sig.action == "buy":
                self.stats.buy_signals += 1
            if sig.token_key:
                self.stats.tokens.add(sig.token_key)
        self.print_signal(sig, latencies, actionable)

    def print_signal(self, sig: Signal, latencies: dict[str, float] | None = None, actionable: bool = False) -> None:
        conf = f"[{sig.confidence:.2f}]"
        if sig.action == "buy":
            head = green(f"🟢 BUY SIGNAL {conf}")
        elif sig.action == "sell":
            head = red(f"🔴 SELL SIGNAL {conf}")
        else:
            head = yellow(f"🟡 WATCH {conf}")
        token = f"mint={short_mint(sig.mint)}" if sig.mint else f"ticker={sig.ticker}"
        if sig.mint and sig.ticker:
            token += f" (${sig.ticker})"
        mark = bold(" ≥ threshold") if actionable else ""
        lines = [f"{head} {token} src={sig.source}{mark}"]
        lines += [f"   {d}" for d in sig.details]
        timing = dict(latencies or {})
        if sig.capture_ts:
            timing["e2e"] = (time.time() - sig.capture_ts) * 1000
        if timing:
            lines.append(dim("   ⏱ " + " · ".join(f"{k} {v:.0f}ms" if v >= 1 else f"{k} {v:.2f}ms"
                                                    for k, v in timing.items())))
        print("\n".join(lines), flush=True)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="mev-streamer: signal orchestrator")
    p.add_argument("--monitor-only", action="store_true", help="show signals only, send nothing")
    p.add_argument("--dry-run", action="store_true", help="override DRY_RUN=true: log WOULD EXECUTE")
    p.add_argument("--stream", metavar="URL", help="override STREAM_URL")
    p.add_argument("--threshold", type=float, metavar="0.7", help="override SIGNAL_THRESHOLD")
    p.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)
    try:
        config = OrchestratorConfig.from_env(args)
    except ValueError as e:
        log.error("config: %s", e)
        return 2
    if not config.stream_url:
        log.error("STREAM_URL not set (.env or --stream URL)")
        return 2
    orch = Orchestrator(config)
    try:
        asyncio.run(orch.run())
    except KeyboardInterrupt:
        pass
    return orch.exit_code


if __name__ == "__main__":
    sys.exit(main())
