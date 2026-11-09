"""Ingestion: захват видео фреймов и аудио чанков из live-стримов."""

from .capture import (
    MultiStreamCapture,
    ResolveError,
    StreamCapture,
    frame_to_image,
    frame_to_numpy,
    pcm_to_numpy,
    resolve_stream_url,
)
from .config import CaptureConfig, load_config

__all__ = [
    "CaptureConfig",
    "MultiStreamCapture",
    "ResolveError",
    "StreamCapture",
    "frame_to_image",
    "frame_to_numpy",
    "load_config",
    "pcm_to_numpy",
    "resolve_stream_url",
]
