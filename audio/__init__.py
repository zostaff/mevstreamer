"""Module 3: Audio / Speech Analysis — Whisper transcription + trading phrases."""

from .keyword_spotter import KeywordSpotter
from .pipeline import AudioConfig, AudioPipeline, AudioResult
from .transcriber import RealtimeTranscriber

__all__ = ["AudioConfig", "AudioPipeline", "AudioResult", "KeywordSpotter", "RealtimeTranscriber"]
