"""Stream capture module configuration.

Sources (by priority): environment variables → .env in the project root (../.env)
→ .env in the current directory → defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional; the environment still works
    load_dotenv = None

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    if load_dotenv is None:
        return
    # override=False: real environment variables take precedence over .env
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    load_dotenv(override=False)


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from e
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")
    return value


def _parse_resolution(raw: str) -> tuple[int, int]:
    raw = raw.strip().lower().replace("x", ":")
    try:
        w, h = (int(p) for p in raw.split(":"))
    except ValueError as e:
        raise ValueError(f"STREAM_RESOLUTION must look like 1280:720, got {raw!r}") from e
    if w <= 0 or h <= 0:
        raise ValueError(f"STREAM_RESOLUTION must be positive, got {raw!r}")
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

    # Audio format (fixed for Whisper: 16 kHz, s16le, mono)
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    audio_sample_width: int = 2  # bytes (s16le)

    queue_maxsize: int = 30

    # Reconnect backoff: 1s, 2s, 4s, ... max 30s
    backoff_initial: float = 1.0
    backoff_max: float = 30.0
    # If the stream ran longer than this, backoff resets to initial
    backoff_reset_after: float = 60.0

    resolve_timeout: float = 30.0

    # Watchdog: ffmpeg silent longer than this → kill → normal reconnect.
    # Allow more before the first chunk (HLS buffering at startup).
    stall_timeout: float = 15.0
    startup_timeout: float = 45.0

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
            stall_timeout=float(_get_int("CAPTURE_STALL_TIMEOUT_SEC", 15)),
        )


def load_config() -> CaptureConfig:
    return CaptureConfig.from_env()
