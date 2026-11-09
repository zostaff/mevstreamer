"""Vision module configuration.

Sources (by priority): environment variables → .env in the project root (../.env)
→ .env in the current directory → defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional; the environment still works
    load_dotenv = None

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _load_env() -> None:
    if load_dotenv is None:
        return
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    load_dotenv(override=False)


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ValueError(f"{name} must be true/false, got {raw!r}")


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be a number, got {raw!r}") from e


@dataclass(frozen=True)
class VisionConfig:
    ocr_engine: str = "auto"         # auto | applevision | easyocr | tesseract
    ocr_gpu: bool = True
    ui_detection: bool = True
    # "roi" — OCR only the URL bar / header / right panel (fast);
    # "full" — whole frame (slower, but sees addresses in the center/chat)
    # "auto" — full for applevision (faster than upscaled ROI), roi for the rest
    roi_mode: str = "auto"
    ocr_min_confidence: float = 0.4
    # Repair OCR-corrupted pump mints with on-chain verification (RPC_URL)
    mint_repair: bool = True

    def __post_init__(self):
        if self.ocr_engine not in ("auto", "applevision", "easyocr", "tesseract"):
            raise ValueError(f"OCR_ENGINE: auto|applevision|easyocr|tesseract, got {self.ocr_engine!r}")
        if self.roi_mode not in ("auto", "roi", "full"):
            raise ValueError(f"VISION_ROI_MODE: auto|roi|full, got {self.roi_mode!r}")
        if not 0.0 <= self.ocr_min_confidence <= 1.0:
            raise ValueError("OCR_MIN_CONFIDENCE must be in [0, 1]")

    @classmethod
    def from_env(cls) -> "VisionConfig":
        _load_env()
        return cls(
            ocr_engine=os.getenv("OCR_ENGINE", "auto").strip().lower() or "auto",
            ocr_gpu=_get_bool("OCR_GPU", True),
            ui_detection=_get_bool("UI_DETECTION", True),
            roi_mode=os.getenv("VISION_ROI_MODE", "auto").strip().lower() or "auto",
            ocr_min_confidence=_get_float("OCR_MIN_CONFIDENCE", 0.4),
            mint_repair=_get_bool("VISION_MINT_REPAIR", True),
        )


def load_config() -> VisionConfig:
    return VisionConfig.from_env()
