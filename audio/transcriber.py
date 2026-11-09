"""Real-time транскрипция PCM чанков через faster-whisper (CTranslate2).

Скорость моделей (на чанк 3 сек):
    tiny.en   — ~30ms   (CPU ok, низкое качество)
    small.en  — ~100ms  (GPU recommended, хорошее качество)
    medium.en — ~300ms  (GPU required, лучшее качество)
"""

from __future__ import annotations

import logging
import re
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

# Подсказка декодеру: только нейтральная лексика. На шуме/музыке Whisper
# «галлюцинирует» сам prompt — поэтому в нём НЕТ buy-фраз и тикеров (раньше
# "Aping into $PEPE…" из prompt'а превращался в ложный BUY).
DEFAULT_INITIAL_PROMPT = "Solana, pump.fun, contract address, market cap, liquidity, chart."

# Типичные галлюцинации Whisper на шуме / музыке / тишине.
_HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thank you for watching",
    "thanks for watching!", "please subscribe", "bye", "okay", "so",
    "subtitles by the amara.org community",
}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def repeats_prompt(text: str, prompt: str | None, threshold: float = 0.6) -> bool:
    """Транскрипт в основном повторяет initial_prompt (галлюцинация на шуме)."""
    if not prompt:
        return False
    pw = set(_words(prompt))
    tw = _words(text)
    if not tw or not pw:
        return False
    overlap = sum(w in pw for w in tw) / len(tw)
    return overlap >= threshold or " ".join(_words(prompt)) in " ".join(tw)


def _resolve_device(device: str) -> str:
    """cuda → cpu fallback, если CUDA недоступна (например, macOS)."""
    if device not in ("cuda", "auto"):
        return device
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:  # noqa: BLE001 — нет CUDA-рантайма = нет CUDA
        pass
    if device == "cuda":
        log.warning("CUDA недоступна — Whisper переключён на CPU (int8)")
    return "cpu"


class RealtimeTranscriber:
    def __init__(
        self,
        model_size: str = "base.en",
        device: str = "auto",
        compute_type: str | None = None,
        rms_threshold: float = 0.01,
        use_silero_vad: bool = True,
        initial_prompt: str | None = DEFAULT_INITIAL_PROMPT,
        cpu_threads: int = 0,
        warmup: bool = True,
    ):
        """
        rms_threshold   — порог RMS (float32, [-1..1]); 0.01 ≈ -40 dBFS.
                          Чанки тише — Whisper не запускается вообще.
        use_silero_vad  — дополнительно silero-VAD внутри faster-whisper
                          (~несколько ms): режет музыку/шум, на которых
                          Whisper галлюцинирует. RMS-гейт идёт первым.
        compute_type    — None = float16 на cuda, int8 на cpu.
        """
        from faster_whisper import WhisperModel  # тяжёлый импорт — только здесь

        self.device = _resolve_device(device)
        self.compute_type = compute_type or ("float16" if self.device == "cuda" else "int8")
        self.model_size = model_size
        self.rms_threshold = rms_threshold
        self.use_silero_vad = use_silero_vad
        self.initial_prompt = initial_prompt or None
        self._lock = threading.Lock()

        t0 = time.perf_counter()
        # Модель создаётся ОДИН раз (загрузка 2-5 сек).
        self.model = WhisperModel(
            model_size,
            device=self.device,
            compute_type=self.compute_type,
            cpu_threads=cpu_threads,
        )
        log.info(
            "Whisper %s загружен на %s/%s за %.1fs",
            model_size, self.device, self.compute_type, time.perf_counter() - t0,
        )
        if warmup:
            self._warmup()

    def _warmup(self) -> None:
        """Первый вызов CTranslate2 медленный (аллокации, CUDA kernels) —
        прогоняем его при старте, а не на первом реальном чанке."""
        rng = np.random.default_rng(0)
        noise = (rng.standard_normal(16000) * 0.05).astype(np.float32)
        with self._lock:
            # vad_filter=False: иначе silero вырежет шум и декодер не прогреется
            segments, _ = self.model.transcribe(
                noise, beam_size=1, language="en", vad_filter=False,
                initial_prompt=self.initial_prompt, without_timestamps=True,
            )
            list(segments)

    @staticmethod
    def pcm_to_float(pcm_bytes: bytes) -> np.ndarray:
        if len(pcm_bytes) % 2:  # битый чанк — отбрасываем последний байт
            pcm_bytes = pcm_bytes[:-1]
        return np.frombuffer(pcm_bytes, np.int16).astype(np.float32) / 32768.0

    @staticmethod
    def rms(audio: np.ndarray) -> float:
        if audio.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))

    def transcribe_chunk(self, pcm_bytes: bytes, sample_rate: int = 16000) -> dict:
        """PCM (s16le, mono) → {text, language, duration_ms, processing_ms, rms, is_speech}.

        duration_ms   — длительность аудио в чанке
        processing_ms — сколько заняла обработка
        """
        t0 = time.perf_counter()
        audio = self.pcm_to_float(pcm_bytes)
        if sample_rate != 16000:
            audio = resample(audio, sample_rate, 16000)
        duration_ms = int(audio.size * 1000 / 16000)
        level = self.rms(audio)

        result = {
            "text": "",
            "language": "en",
            "duration_ms": duration_ms,
            "processing_ms": 0,
            "rms": round(level, 5),
            "is_speech": False,
        }

        # VAD #1: тишина → Whisper не трогаем
        if level < self.rms_threshold:
            result["processing_ms"] = int((time.perf_counter() - t0) * 1000)
            return result

        with self._lock:
            segments, info = self.model.transcribe(
                audio,
                beam_size=1,                 # greedy — минимальная latency
                language="en",               # без language detection
                temperature=0.0,             # без fallback-перекодирования
                condition_on_previous_text=False,
                without_timestamps=True,
                initial_prompt=self.initial_prompt,
                vad_filter=self.use_silero_vad,  # VAD #2: silero
            )
            # segments — генератор; декодирование реально происходит здесь
            parts = [s.text.strip() for s in segments if s.no_speech_prob < 0.8]

        text = " ".join(p for p in parts if p).strip()
        if text.lower().strip(" .!?,") in _HALLUCINATIONS:
            text = ""
        elif repeats_prompt(text, self.initial_prompt):
            log.debug("drop prompt echo: %r", text)
            text = ""

        result.update(
            text=text,
            language=info.language or "en",
            is_speech=bool(text),
            processing_ms=int((time.perf_counter() - t0) * 1000),
        )
        return result


def resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Линейный ресемплинг — достаточно для речи, без scipy."""
    if src_rate == dst_rate or audio.size == 0:
        return audio
    n_out = int(round(audio.size * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)
