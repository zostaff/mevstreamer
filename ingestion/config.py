"""Конфигурация модуля захвата стримов.

Источники (по приоритету): переменные окружения → .env в корне проекта (../.env)
→ .env в текущей директории → значения по умолчанию.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv опционален — environment всё равно работает
    load_dotenv = None

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    if load_dotenv is None:
        return
    # override=False: реальные переменные окружения важнее .env
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    load_dotenv(override=False)


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"{name} должен быть целым числом, получено {raw!r}") from e
    if value <= 0:
        raise ValueError(f"{name} должен быть > 0, получено {value}")
    return value


def _parse_resolution(raw: str) -> tuple[int, int]:
    raw = raw.strip().lower().replace("x", ":")
    try:
        w, h = (int(p) for p in raw.split(":"))
    except ValueError as e:
        raise ValueError(f"STREAM_RESOLUTION должен быть вида 1280:720, получено {raw!r}") from e
    if w <= 0 or h <= 0:
        raise ValueError(f"STREAM_RESOLUTION должен быть положительным, получено {raw!r}")
    return w, h


def _parse_urls(raw: str) -> list[str]:
    return [u.strip() for u in raw.split(",") if u.strip()]


@dataclass(frozen=True)
class CaptureConfig:
    stream_urls: list[str] = field(default_factory=list)
    fps: int = 3
    audio_chunk_sec: int = 3
    width: int = 1280
    height: int = 720

    # Аудио формат (фиксирован под Whisper: 16 kHz, s16le, mono)
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    audio_sample_width: int = 2  # bytes (s16le)

    queue_maxsize: int = 30

    # Reconnect backoff: 1s, 2s, 4s, ... max 30s
    backoff_initial: float = 1.0
    backoff_max: float = 30.0
    # Если поток проработал дольше этого — backoff сбрасывается до initial
    backoff_reset_after: float = 60.0

    resolve_timeout: float = 30.0

    @property
    def resolution(self) -> str:
        return f"{self.width}:{self.height}"

    @property
    def frame_size(self) -> int:
        return self.width * self.height * 3  # rgb24

    @property
    def audio_chunk_size(self) -> int:
        return (
            self.audio_sample_rate
            * self.audio_sample_width
            * self.audio_channels
            * self.audio_chunk_sec
        )

    @property
    def stream_url(self) -> str | None:
        return self.stream_urls[0] if self.stream_urls else None

    @classmethod
    def from_env(cls) -> "CaptureConfig":
        _load_env()
        width, height = _parse_resolution(os.getenv("STREAM_RESOLUTION", "1280:720"))
        return cls(
            stream_urls=_parse_urls(os.getenv("STREAM_URL", "")),
            fps=_get_int("STREAM_FPS", 3),
            audio_chunk_sec=_get_int("STREAM_AUDIO_CHUNK_SEC", 3),
            width=width,
            height=height,
        )


def load_config() -> CaptureConfig:
    return CaptureConfig.from_env()
