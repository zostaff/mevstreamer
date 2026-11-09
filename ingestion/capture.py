"""Захват видео/аудио из live-стримов (Twitch / Kick / YouTube) для real-time анализа.

Видео и аудио захватываются ОТДЕЛЬНЫМИ ffmpeg-процессами, каждый в своём
worker-процессе. Результат кладётся в multiprocessing.Queue:

    video_queue: (timestamp: float, frame_bytes: bytes)   # rgb24, W*H*3 байт
    audio_queue: (timestamp: float, pcm_bytes: bytes)     # s16le, 16 kHz, mono

timestamp — wall-clock (time.time()) в момент, когда фрейм/чанк вычитан из ffmpeg.

Standalone-тест:
    python capture.py twitch.tv/username
    python capture.py "https://www.youtube.com/watch?v=..." --seconds 10 --save-frame /tmp/f.png
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Callable
from urllib.parse import urlparse

try:
    from .config import CaptureConfig, load_config
except ImportError:  # запуск как скрипт: python capture.py <url>
    from config import CaptureConfig, load_config

log = logging.getLogger("ingestion.capture")

YOUTUBE_HOSTS = ("youtube.com", "youtu.be")
STREAMLINK_HOSTS = ("twitch.tv", "kick.com")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class ResolveError(RuntimeError):
    pass


class _Stopped(Exception):
    """Внутренний сигнал: получен stop_event во время долгой операции."""


def normalize_url(url: str) -> str:
    url = url.strip()
    if os.path.exists(url) or "://" in url:
        return url
    return "https://" + url


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _run_cmd(cmd: list[str], timeout: float, stop_event) -> str:
    """subprocess с таймаутом, прерываемый stop_event. Возвращает stdout."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=True,  # Ctrl+C из терминала не долетает напрямую
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if stop_event is not None and stop_event.is_set():
                    raise _Stopped()
                if time.monotonic() > deadline:
                    raise ResolveError(f"{cmd[0]}: таймаут {timeout:.0f}s")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if proc.returncode != 0:
        msg = (err or out or "").strip().splitlines()
        raise ResolveError(f"{cmd[0]} exit={proc.returncode}: {msg[-1] if msg else 'no output'}")
    return out


def resolve_stream_url(
    url: str,
    kind: str = "video",
    timeout: float = 30.0,
    stop_event=None,
) -> str:
    """Получить прямой (HLS/media) URL, который можно скормить ffmpeg.

    kind="audio" позволяет выбрать более лёгкий поток (Twitch audio_only,
    YouTube bestaudio) — аудио-воркеру не нужен 1080p видеопоток.
    Локальные файлы и неизвестные http(s) URL передаются как есть, если
    streamlink их не знает.
    """
    url = normalize_url(url)
    if os.path.exists(url):
        return url

    host = _host(url)

    if _host_matches(host, YOUTUBE_HOSTS):
        if not shutil.which("yt-dlp"):
            raise ResolveError("yt-dlp не найден в PATH (brew install yt-dlp / pip install yt-dlp)")
        fmt = "bestaudio/best" if kind == "audio" else "bestvideo[height<=?1080]/best"
        out = _run_cmd(
            ["yt-dlp", "--no-warnings", "--no-playlist", "-f", fmt, "--get-url", url],
            timeout, stop_event,
        )
    else:
        if not shutil.which("streamlink"):
            if _host_matches(host, STREAMLINK_HOSTS):
                raise ResolveError("streamlink не найден в PATH (pip install streamlink)")
            return url  # прямой URL (m3u8/mp4/rtmp) — отдаём ffmpeg как есть
        quality = "audio_only,worst" if kind == "audio" else "best"
        try:
            out = _run_cmd(
                ["streamlink", "--stream-url", url, quality],
                timeout, stop_event,
            )
        except ResolveError:
            if _host_matches(host, STREAMLINK_HOSTS):
                raise
            return url  # streamlink не знает сайт — пробуем напрямую

    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not lines:
        raise ResolveError(f"пустой ответ при резолве {url}")
    return lines[0]


def frame_to_numpy(frame_bytes: bytes, width: int, height: int):
    import numpy as np
    return np.frombuffer(frame_bytes, dtype=np.uint8).reshape(height, width, 3)


def frame_to_image(frame_bytes: bytes, width: int, height: int):
    from PIL import Image
    return Image.frombytes("RGB", (width, height), frame_bytes)


def pcm_to_numpy(pcm_bytes: bytes):
    """s16le → float32 [-1, 1] (формат, который ждёт Whisper)."""
    import numpy as np
    return np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0


def drain_queue(q) -> int:
    n = 0
    while True:
        try:
            q.get_nowait()
            n += 1
        except (queue.Empty, OSError, ValueError):
            return n


def _init_worker_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s %(processName)s %(levelname)s %(message)s",
        )


# ---------------------------------------------------------------------------
# StreamCapture
# ---------------------------------------------------------------------------

class StreamCapture:
    def __init__(self, stream_url: str, video_queue, audio_queue, config: CaptureConfig | None = None):
        self.stream_url = normalize_url(stream_url)
        self.video_queue = video_queue
        self.audio_queue = audio_queue
        self.config = config or load_config()

        self.stop_event = mp.Event()
        # Счётчики доступны из родителя (мониторинг/тест)
        self.frames_captured = mp.Value("q", 0)
        self.frames_dropped = mp.Value("q", 0)
        self.audio_chunks = mp.Value("q", 0)
        self.reconnects = mp.Value("q", 0)

        self._processes: list[mp.Process] = []

    # Process-объекты не пиклятся — под spawn (macOS) self передаётся в child
    def __getstate__(self):
        state = self.__dict__.copy()
        state["_processes"] = []
        return state

    @property
    def name(self) -> str:
        if os.path.exists(self.stream_url):
            return "file:" + os.path.basename(self.stream_url)
        u = urlparse(self.stream_url)
        return (u.hostname or "?") + (u.path.rstrip("/") or "")

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self.is_alive():
            raise RuntimeError(f"StreamCapture({self.name}) уже запущен")
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg не найден в PATH")
        self.stop_event.clear()
        self._processes = [
            mp.Process(target=self.video_worker, name=f"video[{self.name}]", daemon=True),
            mp.Process(target=self.audio_worker, name=f"audio[{self.name}]", daemon=True),
        ]
        for p in self._processes:
            p.start()
        log.info("capture started: %s (pids %s)", self.stream_url, [p.pid for p in self._processes])

    def is_alive(self) -> bool:
        return any(p.is_alive() for p in self._processes)

    def stop(self, timeout: float = 5.0, drain: bool = True) -> None:
        """Graceful shutdown: stop_event → воркеры убивают ffmpeg и выходят.

        Пока ждём — дренируем очереди, иначе feeder-поток mp.Queue в воркере
        может висеть на заполненном pipe и процесс не завершится.
        """
        self.stop_event.set()
        deadline = time.monotonic() + timeout
        for p in self._processes:
            while p.is_alive() and time.monotonic() < deadline:
                if drain:
                    drain_queue(self.video_queue)
                    drain_queue(self.audio_queue)
                p.join(0.1)
        for p in self._processes:
            if p.is_alive():
                log.warning("%s не завершился за %.0fs — terminate", p.name, timeout)
                p.terminate()
                p.join(2)
            if p.is_alive():
                p.kill()
                p.join(1)
        if drain:
            drain_queue(self.video_queue)
            drain_queue(self.audio_queue)
        self._processes = []
        log.info("capture stopped: %s", self.stream_url)

    # -- workers -------------------------------------------------------------

    def video_worker(self) -> None:
        cfg = self.config
        vf = f"fps={cfg.fps},scale={cfg.resolution}"

        def build_cmd(src: str) -> list[str]:
            return self._ffmpeg_base(src) + [
                "-an", "-sn", "-dn",
                "-vf", vf,
                "-f", "rawvideo", "-pix_fmt", "rgb24",
                "pipe:1",
            ]

        def on_chunk(ts: float, data: bytes) -> None:
            self._put_drop_oldest(self.video_queue, (ts, data))
            with self.frames_captured.get_lock():
                self.frames_captured.value += 1

        self._worker_main("video", build_cmd, cfg.frame_size, on_chunk)

    def audio_worker(self) -> None:
        cfg = self.config

        def build_cmd(src: str) -> list[str]:
            return self._ffmpeg_base(src) + [
                "-vn", "-sn", "-dn",
                "-acodec", "pcm_s16le",
                "-f", "s16le",
                "-ar", str(cfg.audio_sample_rate),
                "-ac", str(cfg.audio_channels),
                "pipe:1",
            ]

        def on_chunk(ts: float, data: bytes) -> None:
            self._put_blocking(self.audio_queue, (ts, data))
            with self.audio_chunks.get_lock():
                self.audio_chunks.value += 1

        self._worker_main("audio", build_cmd, cfg.audio_chunk_size, on_chunk)

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _ffmpeg_base(src: str) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error"]
        if src.startswith(("http://", "https://")):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
            if ".m3u8" in src:
                # Стартовать с live edge, а не с начала плейлиста — иначе на
                # старте вылетает пачка старых сегментов быстрее real-time
                cmd += ["-live_start_index", "-1"]
        elif os.path.exists(src):
            cmd += ["-re"]  # локальный файл — эмулируем real-time (для тестов)
        return cmd + ["-i", src]

    def _worker_main(
        self,
        kind: str,
        build_cmd: Callable[[str], list[str]],
        chunk_size: int,
        on_chunk: Callable[[float, bytes], None],
    ) -> None:
        # Ctrl+C обрабатывает родитель через stop_event — воркер его игнорирует
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        _init_worker_logging()
        cfg = self.config
        backoff = cfg.backoff_initial

        while not self.stop_event.is_set():
            started = time.monotonic()
            got_data = False
            try:
                src = resolve_stream_url(self.stream_url, kind, cfg.resolve_timeout, self.stop_event)
                log.info("[%s] %s → ffmpeg", kind, self.name)
                got_data = self._pump_ffmpeg(kind, build_cmd(src), chunk_size, on_chunk)
            except _Stopped:
                break
            except ResolveError as e:
                log.warning("[%s] resolve failed: %s", kind, e)
            except Exception:
                log.exception("[%s] unexpected error", kind)

            if self.stop_event.is_set():
                break

            # Долго и стабильно работали → начинаем backoff заново
            if got_data and time.monotonic() - started >= cfg.backoff_reset_after:
                backoff = cfg.backoff_initial
            with self.reconnects.get_lock():
                self.reconnects.value += 1
            log.info("[%s] reconnect in %.1fs", kind, backoff)
            if self.stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, cfg.backoff_max)

        log.info("[%s] worker exit", kind)

    def _pump_ffmpeg(
        self,
        kind: str,
        cmd: list[str],
        chunk_size: int,
        on_chunk: Callable[[float, bytes], None],
    ) -> bool:
        """Запустить ffmpeg и читать stdout блоками chunk_size. True если были данные."""
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=chunk_size,
            start_new_session=True,
        )
        stderr_tail: deque[str] = deque(maxlen=10)

        def read_stderr() -> None:
            for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    stderr_tail.append(line)
                    log.debug("[%s] ffmpeg: %s", kind, line)

        def watch_stop() -> None:
            # Разблокирует read() в главном потоке при stop()
            while proc.poll() is None:
                if self.stop_event.wait(0.2):
                    _kill(proc)
                    return

        threading.Thread(target=read_stderr, daemon=True).start()
        threading.Thread(target=watch_stop, daemon=True).start()

        got_data = False
        try:
            while not self.stop_event.is_set():
                data = proc.stdout.read(chunk_size)  # блокирует до полного фрейма/чанка или EOF
                if len(data) < chunk_size:
                    break  # EOF / обрыв; неполный хвост выбрасываем
                got_data = True
                on_chunk(time.time(), data)
        finally:
            _kill(proc)
            if not self.stop_event.is_set():
                tail = " | ".join(stderr_tail) or "no stderr"
                log.warning("[%s] ffmpeg exited (code=%s): %s", kind, proc.returncode, tail)
        return got_data

    def _put_drop_oldest(self, q, item) -> None:
        """Video: при полной очереди выкидываем самый старый фрейм."""
        while True:
            try:
                q.put_nowait(item)
                return
            except queue.Full:
                try:
                    q.get_nowait()
                    with self.frames_dropped.get_lock():
                        self.frames_dropped.value += 1
                except queue.Empty:
                    pass

    def _put_blocking(self, q, item) -> None:
        """Audio: никогда не дропаем — ждём, пока downstream освободит место."""
        warned = False
        while not self.stop_event.is_set():
            try:
                q.put(item, timeout=0.5)
                return
            except queue.Full:
                if not warned:
                    log.warning("[audio] queue full — downstream не успевает, ждём")
                    warned = True
        raise _Stopped()


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# MultiStreamCapture
# ---------------------------------------------------------------------------

class MultiStreamCapture:
    """Несколько стримов параллельно.

    Каждый стрим изолирован: свои video/audio воркер-процессы, свои ffmpeg и
    свои очереди — падение одного не влияет на другие.

        multi = MultiStreamCapture(cfg.stream_urls, cfg)
        multi.start_all()
        video_q, audio_q = multi.queues["https://twitch.tv/user1"]
    """

    def __init__(self, stream_urls: list[str], config: CaptureConfig | None = None):
        self.config = config or load_config()
        urls = [normalize_url(u) for u in (stream_urls or self.config.stream_urls)]
        if not urls:
            raise ValueError("нет URL стримов (STREAM_URL пуст)")
        self.queues: dict[str, tuple[mp.Queue, mp.Queue]] = {}
        self.captures: dict[str, StreamCapture] = {}
        for url in dict.fromkeys(urls):  # dedupe, порядок сохраняется
            vq = mp.Queue(maxsize=self.config.queue_maxsize)
            aq = mp.Queue(maxsize=self.config.queue_maxsize)
            self.queues[url] = (vq, aq)
            self.captures[url] = StreamCapture(url, vq, aq, self.config)

    def start_all(self) -> None:
        for cap in self.captures.values():
            cap.start()

    def stop_all(self, timeout: float = 5.0) -> None:
        for cap in self.captures.values():
            cap.stop_event.set()  # сигналим всем сразу, потом ждём по очереди
        for cap in self.captures.values():
            cap.stop(timeout=timeout)

    def __enter__(self):
        self.start_all()
        return self

    def __exit__(self, *exc):
        self.stop_all()


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

def _standalone_test(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Захватить N секунд стрима и показать статистику")
    parser.add_argument("url", nargs="?", help="URL стрима (по умолчанию STREAM_URL из env)")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--start-timeout", type=float, default=60.0,
                        help="сколько ждать первых данных (резолв + буферизация)")
    parser.add_argument("--save-frame", metavar="PATH", help="сохранить последний фрейм (png/jpg)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(processName)s %(levelname)s %(message)s",
    )
    cfg = load_config()
    url = args.url or cfg.stream_url
    if not url:
        parser.error("укажи URL или STREAM_URL")

    vq: mp.Queue = mp.Queue(maxsize=cfg.queue_maxsize)
    aq: mp.Queue = mp.Queue(maxsize=cfg.queue_maxsize)
    cap = StreamCapture(url, vq, aq, cfg)

    frames = 0
    frame_sizes: set[int] = set()
    first_ts = last_ts = None
    last_frame: bytes | None = None
    audio_chunks = 0
    audio_bytes = 0

    print(f"capture {cap.stream_url}: fps={cfg.fps} res={cfg.resolution} "
          f"audio_chunk={cfg.audio_chunk_sec}s, {args.seconds:.0f}s test")
    cap.start()
    t_start = time.monotonic()
    t_first: float | None = None
    try:
        while True:
            now = time.monotonic()
            if t_first is None and now - t_start > args.start_timeout:
                print(f"нет данных за {args.start_timeout:.0f}s — сдаюсь", file=sys.stderr)
                break
            if t_first is not None and now - t_first >= args.seconds:
                break
            got = False
            try:
                ts, data = vq.get(timeout=0.05)
                got = True
                if t_first is None:
                    t_first = time.monotonic()
                    print(f"первый фрейм через {t_first - t_start:.1f}s")
                frames += 1
                frame_sizes.add(len(data))
                first_ts = first_ts or ts
                last_ts = ts
                last_frame = data
            except queue.Empty:
                pass
            try:
                while True:
                    _, pcm = aq.get_nowait()
                    got = True
                    audio_chunks += 1
                    audio_bytes += len(pcm)
            except queue.Empty:
                pass
            if not got and not cap.is_alive():
                print("воркеры завершились", file=sys.stderr)
                break
    except KeyboardInterrupt:
        print("\nCtrl+C — останавливаю")
    finally:
        elapsed = time.monotonic() - t_first if t_first else 0.0
        cap.stop()

    bytes_per_sec = cfg.audio_sample_rate * cfg.audio_sample_width * cfg.audio_channels
    print("\n=== результат ===")
    print(f"захвачено:        {elapsed:.1f}s")
    print(f"video frames:     {frames}  (dropped в очереди: {cap.frames_dropped.value})")
    if elapsed > 0:
        print(f"effective FPS:    {frames / elapsed:.2f}  (target {cfg.fps})")
    if frames > 1 and last_ts > first_ts:
        # HLS приходит сегментами → фреймы прилетают пачками, это нормально
        print(f"arrival span:     {last_ts - first_ts:.1f}s между первым и последним фреймом")
    sizes = ", ".join(f"{s:,}" for s in sorted(frame_sizes)) or "-"
    print(f"frame size:       {sizes} bytes (ожидается {cfg.frame_size:,} = {cfg.width}x{cfg.height}x3)")
    print(f"audio chunks:     {audio_chunks} × {cfg.audio_chunk_size:,} bytes "
          f"= {audio_bytes / bytes_per_sec:.1f}s аудио")
    print(f"reconnects:       video+audio {cap.reconnects.value}")

    if args.save_frame and last_frame:
        frame_to_image(last_frame, cfg.width, cfg.height).save(args.save_frame)
        print(f"последний фрейм:  {args.save_frame}")

    return 0 if frames > 0 else 1


if __name__ == "__main__":
    sys.exit(_standalone_test())
