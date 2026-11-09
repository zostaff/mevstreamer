"""Vision: OCR видеофреймов, детекция Solana адресов/тикеров и торговых UI."""

from .config import VisionConfig, load_config
from .detector import TokenDetector, is_valid_address
from .ocr import DEFAULT_ROIS, FULL_FRAME_ROIS, ROI, OCREngine, frame_to_array
from .pipeline import VisionPipeline, VisionResult
from .ui_recognizer import UIRecognizer

__all__ = [
    "DEFAULT_ROIS",
    "FULL_FRAME_ROIS",
    "OCREngine",
    "ROI",
    "TokenDetector",
    "UIRecognizer",
    "VisionConfig",
    "VisionPipeline",
    "VisionResult",
    "frame_to_array",
    "is_valid_address",
    "load_config",
]
