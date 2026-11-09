#!/usr/bin/env python3
"""Точка входа Python-стека: стрим → vision/audio → scoring → executor.

    python signal/orchestrator.py                     # режим из .env (DRY_RUN=true по умолчанию)
    python signal/orchestrator.py --monitor-only      # только показывать сигналы
    python signal/orchestrator.py --dry-run --stream twitch.tv/name --threshold 0.75

Live-режим (реальная отправка в executor) включается только DRY_RUN=false в
окружении/.env и без --dry-run / --monitor-only.

В executor уходят только action=buy|sell с confidence >= threshold;
watch-сигналы лишь логируются.
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
# multiprocessing (spawn-воркеры, resource_tracker) стартует детей через
# `python -c ...`, а при -c в sys.path[0] попадает cwd. Запуск из корня →
# папка signal/ затеняет stdlib `signal` в дочерних процессах.
# PYTHONSAFEPATH=1 (3.11+) убирает cwd из sys.path; env наследуется детьми.
os.environ["PYTHONSAFEPATH"] = "1"
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
# Корень — в КОНЕЦ sys.path: папка signal/ в корне иначе затенит stdlib `signal`
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from aggregator import SignalAggregator, normalize_audio, normalize_vision  # noqa: E402
from dedup import SignalDedup  # noqa: E402
from ipc import IPC_SOCKET_PATH, ExecutorClient  # noqa: E402
from resolver import TokenResolver  # noqa: E402
from scorer import Signal, SignalScorer, short_mint  # noqa: E402

from ingestion.config import CaptureConfig  # noqa: E402

log = logging.getLogger("signal")

# --------------------------------------------------------------------------
# Цвета
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
    # faster-whisper пишет 2 INFO-строки на каждый чанк, httpx — загрузку моделей
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
        raise ValueError(f"{name} должен быть числом, получено {raw!r}") from e


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
    extra: dict = field(default_factory=dict)

    @property
    def stream_url(self) -> str | None:
        return self.capture.stream_url

    def __getattr__(self, name: str):
        # VisionPipeline/AudioPipeline получают этот config: даём доступ к
        # полям CaptureConfig (fps, width, height, ...) напрямую.
        capture = self.__dict__.get("capture")
        if capture is not None and hasattr(capture, name):
            return getattr(capture, name)
        raise AttributeError(name)

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> "OrchestratorConfig":
        capture = CaptureConfig.from_env()  # заодно грузит .env из корня
        if args.stream:
            capture = replace(capture, stream_urls=[args.stream])
        threshold = args.threshold if args.threshold is not None else _env_float("SIGNAL_THRESHOLD", 0.7)
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"threshold должен быть в (0, 1], получено {threshold}")
        return cls(
            capture=capture,
            signal_threshold=threshold,
            signal_cooldown_sec=_env_float("SIGNAL_COOLDOWN_SEC", 60.0),
            helius_api_key=_helius_key_from_env(),
            dry_run=args.dry_run or _env_bool("DRY_RUN", True),
            monitor_only=args.monitor_only,
            ipc_socket_path=os.getenv("IPC_SOCKET_PATH", IPC_SOCKET_PATH),
        )


# --------------------------------------------------------------------------
# Статистика
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
            + (f"  (rejected: {self.rejected}, failed: {self.send_failed})" if self.rejected or self.send_failed else ""),
        ]
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
    """Ленивый импорт тяжёлых пайплайнов (torch/easyocr/whisper).

    Под spawn (macOS) каждый capture-воркер заново исполняет этот модуль как
    __mp_main__ — импорт на уровне модуля загрузил бы модели в каждый воркер.
    """
    from ingestion.capture import StreamCapture
    try:
        from vision.pipeline import VisionPipeline
        from audio.pipeline import AudioPipeline
    except ImportError as e:
        # vision/audio делают `try: from .x … except ImportError: from x …`,
        # поэтому реальная причина (нет numpy/easyocr/…) лежит в __context__
        root = e
        while root.__context__ is not None and isinstance(root.__context__, ImportError):
            root = root.__context__
        raise SystemExit(f"не удалось импортировать vision/audio pipeline: {root} "
                         f"(pip install -r vision/requirements.txt -r audio/requirements.txt)") from e
    return StreamCapture, VisionPipeline, AudioPipeline


class Orchestrator:
    PRINT_REPEAT_SEC = 10.0      # повторить вывод неизменного сигнала не чаще
    PRINT_CONF_DELTA = 0.05      # ...или при изменении confidence на столько
    STATUS_INTERVAL_SEC = 30.0

    def __init__(self, config: OrchestratorConfig, pipelines=None):
        StreamCapture, VisionPipeline, AudioPipeline = pipelines or _load_pipelines()
        self.config = config
        qsize = config.capture.queue_maxsize
        self.video_queue = mp.Queue(maxsize=qsize)
        self.audio_queue = mp.Queue(maxsize=qsize)
        # StreamCapture пиклится в воркеры → отдаём ему «чистый» CaptureConfig
        self.capture = StreamCapture(config.stream_url, self.video_queue, self.audio_queue, config.capture)
        # Пайплайны читают свои настройки (OCR_ENGINE, WHISPER_MODEL, ...) из env сами:
        # VisionPipeline(None) → VisionConfig.from_env(), AudioPipeline(None) → AudioConfig.from_env()
        self.vision = VisionPipeline(None)
        self.audio_pipeline = AudioPipeline(None)
        self.aggregator = SignalAggregator(config.vision_window_sec, config.audio_window_sec)
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
            log.warning(red("LIVE MODE: сигналы >= %.2f уходят в executor (%s)"),
                        self.threshold, self.config.ipc_socket_path)

        self.capture.start()
        tasks = [
            asyncio.create_task(self._video_loop(), name="video"),
            asyncio.create_task(self._audio_loop(), name="audio"),
            asyncio.create_task(self._housekeeping(), name="housekeeping"),
        ]
        try:
            stop_waiter = asyncio.create_task(self._stop.wait())
            done, _ = await asyncio.wait([stop_waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t is not stop_waiter and t.exception():
                    log.error("task %s crashed", t.get_name(), exc_info=t.exception())
            stop_waiter.cancel()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._dispatch_tasks:  # дать уже отправляемым сигналам завершиться
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
        print(dim("\n^C — shutting down (ещё раз для принудительного выхода)"), flush=True)
        self._stop.set()

    # ---------------------------------------------------------------- loops

    def _get_latest(self, q) -> tuple | None:
        """Самый свежий фрейм; устаревшие выбрасываем — латентность важнее полноты."""
        item = None
        while True:
            try:
                item_new = q.get_nowait()
            except queue.Empty:
                return item
            if item is not None:
                self.stats.frames_skipped += 1
            item = item_new

    def _pipeline_error(self, kind: str) -> None:
        self.stats.pipeline_errors += 1
        now = time.monotonic()
        if now - self._last_error_log > 10:
            self._last_error_log = now
            log.exception("%s pipeline error (дальше ошибки логируются не чаще раза в 10с)", kind)

    async def _video_loop(self) -> None:
        while True:
            item = self._get_latest(self.video_queue)
            if item is None:
                await asyncio.sleep(0.01)
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
            obs = normalize_vision(raw, ts)
            if obs is not None:
                log.debug("vision %.0fms: mints=%s tickers=%s platform=%s action=%s",
                          ms, [short_mint(m) for m in obs.mints], obs.tickers, obs.platform, obs.action)
                self.aggregator.add_vision(obs)
                self._evaluate(ts, {"vision": ms})

    async def _audio_loop(self) -> None:
        while True:
            try:
                ts, pcm = self.audio_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.02)
                continue
            self.stats.audio_chunks += 1
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
                log.warning("audio отстаёт на %.1fs — Whisper не успевает (модель/устройство?)", backlog)
            obs = normalize_audio(raw, ts)
            if obs is not None:
                log.debug('audio %.0fms: "%s" buy=%s tickers=%s', ms, obs.transcript[-80:], obs.buy_phrase, obs.tickers)
                self.aggregator.add_audio(obs)
                self._evaluate(ts, {"audio": ms})

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
        self._maybe_print(sig, {**step_ms, "score": score_ms}, actionable)

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
                mint = await self.resolver.ticker_to_mint(sig.ticker)
                ms = (time.perf_counter() - t0) * 1000
                self.stats.lat("resolve", ms)
                if not mint:
                    self.stats.unresolved += 1
                    log.warning("$%s → mint не найден (%.0fms) — сигнал пропущен", sig.ticker, ms)
                    return
                log.info("resolved $%s → %s via %s (%.0fms)", sig.ticker, short_mint(mint), self.resolver.last_source, ms)
                sig = replace(sig, mint=mint, context=f"{sig.context} + resolved ${sig.ticker}")
                if self.dedup.is_duplicate(sig):  # этот mint уже отправляли
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
    p.add_argument("--monitor-only", action="store_true", help="только показывать сигналы, ничего не отправлять")
    p.add_argument("--dry-run", action="store_true", help="override DRY_RUN=true: логировать WOULD EXECUTE")
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
        log.error("STREAM_URL не задан (.env или --stream URL)")
        return 2
    orch = Orchestrator(config)
    try:
        asyncio.run(orch.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
