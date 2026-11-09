"""Real-time transcription of PCM chunks via faster-whisper (CTranslate2).

Model speed (per 3 s chunk):
    tiny.en   — ~30ms   (CPU ok, low quality)
    small.en  — ~100ms  (GPU recommended, good quality)
    medium.en — ~300ms  (GPU required, best quality)
"""

from __future__ import annotations

import logging
import re
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

# Decoder hint: neutral vocabulary only. On noise/music Whisper "hallucinates"
# the prompt itself, so it contains NO buy phrases or tickers (previously
# "Aping into $PEPE…" from the prompt turned into a false BUY).
DEFAULT_INITIAL_PROMPT = "Solana, pump.fun, contract address, market cap, liquidity, chart."

# Typical Whisper hallucinations on noise / music / silence.
_HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thank you for watching",
    "thanks for watching!", "please subscribe", "bye", "okay", "so",
    "subtitles by the amara.org community",
}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def repeats_prompt(text: str, prompt: str | None, threshold: float = 0.6) -> bool:
    """Transcript mostly repeats initial_prompt (hallucination on noise)."""
    if not prompt:
        return False
    pw = set(_words(prompt))
    tw = _words(text)
    if not tw or not pw:
        return False
    overlap = sum(w in pw for w in tw) / len(tw)
    return overlap >= threshold or " ".join(_words(prompt)) in " ".join(tw)


def _resolve_device(device: str) -> str:
    """cuda → cpu fallback when CUDA is unavailable (e.g. macOS)."""
    if device not in ("cuda", "auto"):
        return device
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:  # noqa: BLE001 — no CUDA runtime means no CUDA
        pass
    if device == "cuda":
        log.warning("CUDA unavailable, Whisper falling back to CPU (int8)")
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
        rms_threshold   — RMS threshold (float32, [-1..1]); 0.01 ≈ -40 dBFS.
                          Quieter chunks skip Whisper entirely.
        use_silero_vad  — additional silero VAD inside faster-whisper
                          (a few ms): cuts music/noise that make
                          Whisper hallucinate. The RMS gate runs first.
        compute_type    — None = float16 on cuda, int8 on cpu.
        """
        from faster_whisper import WhisperModel  # heavy import, done only here

        self.device = _resolve_device(device)
        self.compute_type = compute_type or ("float16" if self.device == "cuda" else "int8")
        self.model_size = model_size
        self.rms_threshold = rms_threshold
        self.use_silero_vad = use_silero_vad
        self.initial_prompt = initial_prompt or None
        self._lock = threading.Lock()

        t0 = time.perf_counter()
        # The model is created ONCE (loading takes 2-5 s).
        self.model = WhisperModel(
            model_size,
            device=self.device,
            compute_type=self.compute_type,
            cpu_threads=cpu_threads,
        )
        log.info(
            "Whisper %s loaded on %s/%s in %.1fs",
            model_size, self.device, self.compute_type, time.perf_counter() - t0,
        )
        if warmup:
            self._warmup()

    def _warmup(self) -> None:
        """The first CTranslate2 call is slow (allocations, CUDA kernels), so
        run it at startup rather than on the first real chunk."""
        rng = np.random.default_rng(0)
        noise = (rng.standard_normal(16000) * 0.05).astype(np.float32)
        with self._lock:
            # vad_filter=False: otherwise silero strips the noise and the decoder never warms up
            segments, _ = self.model.transcribe(
                noise, beam_size=1, language="en", vad_filter=False,
                initial_prompt=self.initial_prompt, without_timestamps=True,
            )
            list(segments)

    @staticmethod
    def pcm_to_float(pcm_bytes: bytes) -> np.ndarray:
        if len(pcm_bytes) % 2:  # corrupt chunk: drop the trailing byte
            pcm_bytes = pcm_bytes[:-1]
        return np.frombuffer(pcm_bytes, np.int16).astype(np.float32) / 32768.0

    @staticmethod
    def rms(audio: np.ndarray) -> float:
        if audio.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))

    def transcribe_chunk(self, pcm_bytes: bytes, sample_rate: int = 16000) -> dict:
        """PCM (s16le, mono) → {text, language, duration_ms, processing_ms, rms, is_speech}.

        duration_ms   — audio duration of the chunk
        processing_ms — processing time
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

        # VAD #1: silence → skip Whisper
        if level < self.rms_threshold:
            result["processing_ms"] = int((time.perf_counter() - t0) * 1000)
            return result

        with self._lock:
            segments, info = self.model.transcribe(
                audio,
                beam_size=1,                 # greedy: minimal latency
                language="en",               # no language detection
                temperature=0.0,             # no temperature fallback re-decoding
                condition_on_previous_text=False,
                without_timestamps=True,
                initial_prompt=self.initial_prompt,
                vad_filter=self.use_silero_vad,  # VAD #2: silero
            )
            # segments is a generator; decoding actually happens here
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
    """Linear resampling: good enough for speech, no scipy."""
    if src_rate == dst_rate or audio.size == 0:
        return audio
    n_out = int(round(audio.size * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)
