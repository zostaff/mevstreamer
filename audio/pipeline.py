"""Аудио пайплайн: PCM чанк → транскрипт → торговые сигналы + тикеры.

Standalone:
    python pipeline.py stream.wav                 # обработать файл чанками по 3с
    python pipeline.py stream.mp3 --model tiny.en --device cpu
    python pipeline.py --text "aping into PEPE right now"   # без Whisper
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

try:  # как пакет (from audio import AudioPipeline)
    from .keyword_spotter import KeywordSpotter
    from .transcriber import RealtimeTranscriber, resample
except ImportError:  # как скрипт (python pipeline.py)
    from keyword_spotter import KeywordSpotter
    from transcriber import RealtimeTranscriber, resample

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # s16le


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"{name} должен быть числом, получено {raw!r}") from e


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AudioConfig:
    whisper_model: str = "base.en"
    whisper_device: str = "auto"          # auto: cuda, если доступна, иначе cpu
    whisper_compute_type: str | None = None
    vad_rms_threshold: float = 0.01
    use_silero_vad: bool = True
    # Сколько слов прошлого чанка держать для фраз, разрезанных границей
    context_words: int = 8

    @classmethod
    def from_env(cls) -> "AudioConfig":
        if load_dotenv is not None:
            load_dotenv(_PROJECT_ROOT / ".env", override=False)
            load_dotenv(override=False)
        return cls(
            whisper_model=os.getenv("WHISPER_MODEL", "base.en").strip() or "base.en",
            whisper_device=os.getenv("WHISPER_DEVICE", "auto").strip() or "auto",
            whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "").strip() or None,
            vad_rms_threshold=_env_float("AUDIO_VAD_RMS", 0.01),
            use_silero_vad=_env_bool("AUDIO_SILERO_VAD", True),
        )


@dataclass
class AudioResult:
    transcript: str = ""
    signals: list[dict] = field(default_factory=list)   # [{type, phrase, confidence, matched}]
    tickers: list[dict] = field(default_factory=list)   # [{ticker, confidence, source}]
    processing_ms: int = 0
    is_speech: bool = False
    rms: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    def __getitem__(self, key: str):  # result["signals"] тоже работает
        return getattr(self, key)


class AudioPipeline:
    def __init__(self, config: AudioConfig | None = None, transcriber=None):
        config = config or AudioConfig.from_env()
        self.config = config
        # WhisperModel создаётся один раз здесь; transcriber можно подменить в тестах
        self.transcriber = transcriber or RealtimeTranscriber(
            model_size=config.whisper_model,
            device=config.whisper_device,
            compute_type=config.whisper_compute_type,
            rms_threshold=config.vad_rms_threshold,
            use_silero_vad=config.use_silero_vad,
        )
        self.spotter = KeywordSpotter()
        self._context = ""

    def reset(self) -> None:
        """Сбросить межчанковый контекст (смена стрима / реконнект)."""
        self._context = ""

    def process_chunk(self, pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE) -> AudioResult:
        t0 = time.perf_counter()
        tr = self.transcriber.transcribe_chunk(pcm_bytes, sample_rate=sample_rate)
        text = tr["text"]
        result = AudioResult(transcript=text, is_speech=tr.get("is_speech", bool(text)),
                             rms=tr.get("rms", 0.0))

        if text:
            result.signals = self.spotter.spot(text, context=self._context)
            result.tickers = self.spotter.extract_tickers(text, context=self._context)
            words = (self._context + " " + text).split()
            self._context = " ".join(words[-self.config.context_words:])
        else:
            # Фраза не может продолжаться через тишину
            self._context = ""

        result.processing_ms = int((time.perf_counter() - t0) * 1000)
        return result

    async def process_chunk_async(self, pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE) -> AudioResult:
        """Для asyncio-пайплайна: Whisper блокирующий, уводим в thread pool."""
        return await asyncio.to_thread(self.process_chunk, pcm_bytes, sample_rate)


# ---------------------------------------------------------------------- CLI


def load_audio_pcm(path: str) -> bytes:
    """Файл → PCM s16le 16kHz mono. WAV читается напрямую, остальное — через ffmpeg."""
    try:
        with wave.open(path, "rb") as wf:
            rate, ch, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
        if width == 2:
            audio = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
            if ch > 1:
                audio = audio.reshape(-1, ch).mean(axis=1)
            audio = resample(audio, rate, SAMPLE_RATE)
            return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    except (wave.Error, EOFError):
        pass  # не PCM WAV (float, mp3, ...) — пробуем ffmpeg

    if not shutil.which("ffmpeg"):
        raise SystemExit(f"{path}: не 16-bit PCM WAV, а ffmpeg не найден для конвертации")
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path,
         "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"ffmpeg: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def _fmt_detections(res: AudioResult) -> str:
    parts = [f"{s['type'].upper()}:'{s['phrase']}'({s['confidence']})" for s in res.signals]
    parts += [f"${t['ticker']}({t['confidence']})" for t in res.tickers]
    return "  ".join(parts)


def _run_text(text: str, as_json: bool) -> None:
    sp = KeywordSpotter()
    out = {"transcript": text, "signals": sp.spot(text), "tickers": sp.extract_tickers(text)}
    print(json.dumps(out, ensure_ascii=False, indent=2) if as_json else
          f"signals: {out['signals']}\ntickers: {out['tickers']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Транскрипция + детекция торговых фраз")
    ap.add_argument("audio", nargs="?", help="аудиофайл (wav/mp3/...)")
    ap.add_argument("--text", help="проверить только keyword spotter на тексте")
    ap.add_argument("--model", help="whisper модель (по умолчанию WHISPER_MODEL)")
    ap.add_argument("--device", help="cuda|cpu|auto (по умолчанию WHISPER_DEVICE)")
    ap.add_argument("--chunk-sec", type=float, default=3.0)
    ap.add_argument("--json", action="store_true", help="JSON lines на чанк")
    ap.add_argument("--all", action="store_true", help="показывать и чанки без детекций")
    args = ap.parse_args(argv)

    if args.text:
        _run_text(args.text, args.json)
        return 0
    if not args.audio:
        ap.error("укажите аудиофайл или --text")

    cfg = AudioConfig.from_env()
    overrides = {k: v for k, v in (("whisper_model", args.model), ("whisper_device", args.device)) if v}
    if overrides:
        cfg = AudioConfig(**{**asdict(cfg), **overrides})

    pcm = load_audio_pcm(args.audio)
    chunk_bytes = int(args.chunk_sec * SAMPLE_RATE) * SAMPLE_WIDTH
    total_sec = len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH)

    t_load = time.perf_counter()
    pipe = AudioPipeline(cfg)
    tr = pipe.transcriber
    print(f"# model={cfg.whisper_model} device={tr.device}/{tr.compute_type} "
          f"load={time.perf_counter() - t_load:.1f}s audio={total_sec:.1f}s", file=sys.stderr)

    n_chunks = n_signals = 0
    proc_ms: list[int] = []
    for off in range(0, len(pcm), chunk_bytes):
        chunk = pcm[off:off + chunk_bytes]
        ts = off / (SAMPLE_RATE * SAMPLE_WIDTH)
        res = pipe.process_chunk(chunk)
        n_chunks += 1
        n_signals += len(res.signals)
        if res.is_speech:
            proc_ms.append(res.processing_ms)

        if args.json:
            print(json.dumps({"t": round(ts, 2), **res.to_dict()}, ensure_ascii=False))
            continue
        if not (res.signals or res.tickers or args.all):
            continue
        stamp = f"[{int(ts // 60):02d}:{ts % 60:05.2f}]"
        text = res.transcript if res.is_speech else "<silence>"
        print(f"{stamp} {res.processing_ms:4d}ms  {text}")
        if res.signals or res.tickers:
            print(f"{' ' * len(stamp)}   → {_fmt_detections(res)}")

    avg = f"{sum(proc_ms) / len(proc_ms):.0f}ms" if proc_ms else "n/a"
    print(f"# chunks={n_chunks} speech={len(proc_ms)} signals={n_signals} avg_speech_latency={avg}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
