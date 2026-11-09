"""OCR engine: EasyOCR (GPU, more accurate) or Tesseract (CPU, faster).

OCR runs on ROIs rather than the full frame (URL bar, token header, right
trading panel) — roughly 3x faster than full-frame.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np

try:
    import cv2
except ImportError:  # cv2 is only needed to speed up resize
    cv2 = None

log = logging.getLogger(__name__)

DEFAULT_MIN_CONFIDENCE = 0.4


# --------------------------------------------------------------------------- #
# Frame and OCR-block utilities (used by both the detector and the UI recognizer)
# --------------------------------------------------------------------------- #

def frame_to_array(frame, width: int = 1280, height: int = 720) -> np.ndarray:
    """RGB bytes (or an existing numpy HxWx3) → numpy uint8 (H, W, 3) without copying."""
    if isinstance(frame, np.ndarray):
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError(f"expected an (H, W, 3) array, got {frame.shape}")
        return frame
    expected = width * height * 3
    if len(frame) != expected:
        raise ValueError(
            f"frame size {len(frame)} bytes does not match {width}x{height}x3={expected}"
        )
    return np.frombuffer(frame, dtype=np.uint8).reshape(height, width, 3)


def bbox_height(b: dict) -> int:
    return max(1, b["bbox"][3] - b["bbox"][1])


def bbox_cy(b: dict) -> float:
    return (b["bbox"][1] + b["bbox"][3]) / 2


def group_lines(blocks: list[dict]) -> list[list[dict]]:
    """Groups blocks into lines (by Y center), left to right within a line."""
    lines: list[dict] = []
    for b in sorted(blocks, key=bbox_cy):
        cy, h = bbox_cy(b), bbox_height(b)
        if lines and abs(cy - lines[-1]["cy"]) <= max(h, lines[-1]["h"]) * 0.5:
            line = lines[-1]
            line["items"].append(b)
            n = len(line["items"])
            line["cy"] += (cy - line["cy"]) / n
            line["h"] = max(line["h"], h)
        else:
            lines.append({"cy": cy, "h": h, "items": [b]})
    return [sorted(line["items"], key=lambda x: x["bbox"][0]) for line in lines]


def sort_reading_order(blocks: list[dict]) -> list[dict]:
    """Top-to-bottom, left-to-right, tolerating blocks on the same line
    drifting slightly in Y."""
    return [b for line in group_lines(blocks) for b in line]


def _iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / min(area_a, area_b)  # overlap relative to the smaller box — catches clipped duplicates


def _dedupe(blocks: list[dict]) -> list[dict]:
    """Removes duplicates from the overlap zone of adjacent ROIs."""
    kept: list[dict] = []
    for b in sorted(blocks, key=lambda x: -x["confidence"]):
        if any(_iou(b["bbox"], k["bbox"]) > 0.6 for k in kept):
            continue
        kept.append(b)
    return kept


# --------------------------------------------------------------------------- #
# ROI
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ROI:
    """Region in frame fractions. scale > 1 upscales before OCR (small text)."""

    name: str
    x1: float
    y1: float
    x2: float
    y2: float
    scale: float = 1.0
    pad: float = 0.012  # Y overlap so a line isn't cut at the ROI boundary

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        py = self.pad * height
        return (
            max(0, int(self.x1 * width)),
            max(0, int(self.y1 * height - py)),
            min(width, int(self.x2 * width)),
            min(height, int(self.y2 * height + py)),
        )


# Regions barely overlap: the header starts where the URL bar ends,
# and the trading panel sits below the header, so the same text isn't OCR'd twice.
DEFAULT_ROIS: tuple[ROI, ...] = (
    ROI("url_bar", 0.0, 0.00, 1.0, 0.05, scale=2.0),     # small address-bar font
    ROI("header", 0.0, 0.05, 1.0, 0.15, scale=1.5),      # ticker / mint / MC
    ROI("trade_panel", 0.70, 0.15, 1.0, 1.0, scale=1.0),  # Buy/Sell, toasts on the right
)
FULL_FRAME_ROIS: tuple[ROI, ...] = (ROI("full", 0.0, 0.0, 1.0, 1.0, pad=0.0),)


def _resize(img: np.ndarray, scale: float) -> np.ndarray:
    h, w = img.shape[:2]
    size = (max(1, int(w * scale)), max(1, int(h * scale)))
    if cv2 is not None:
        return cv2.resize(img, size, interpolation=cv2.INTER_CUBIC)
    from PIL import Image
    return np.asarray(Image.fromarray(img).resize(size, Image.BICUBIC))


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

ENGINES = ("auto", "applevision", "easyocr", "tesseract")


def applevision_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Vision  # noqa: F401  (pyobjc-framework-Vision)
        return True
    except ImportError:
        return False


def resolve_engine(engine: str) -> str:
    """`auto` → Apple Vision on macOS (if pyobjc is installed), else EasyOCR."""
    engine = (engine or "auto").lower()
    if engine not in ENGINES:
        raise ValueError(f"OCR engine: {'|'.join(ENGINES)}, got {engine!r}")
    if engine == "auto":
        return "applevision" if applevision_available() else "easyocr"
    return engine


class OCREngine:
    # EasyOCR Reader takes ~2-3 s to load models — keep one per process
    _readers: dict = {}
    _readers_lock = threading.Lock()

    def __init__(
        self,
        engine: str = "easyocr",
        gpu: bool = True,
        languages: tuple[str, ...] = ("en",),
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        rois: tuple[ROI, ...] = DEFAULT_ROIS,
    ):
        engine = resolve_engine(engine)
        self.engine = engine
        self.gpu = gpu
        self.languages = tuple(languages)
        self.min_confidence = min_confidence
        self.rois = tuple(rois)
        # Initialize eagerly, not on the first frame, to avoid losing the first seconds of the stream
        if engine == "easyocr":
            self._reader = self._get_easyocr_reader(self.languages, gpu)
        elif engine == "applevision":
            self._init_applevision()
        else:
            self._init_tesseract()

    # -- init --------------------------------------------------------------- #

    @classmethod
    def _get_easyocr_reader(cls, languages: tuple[str, ...], gpu: bool):
        key = (languages, gpu)
        with cls._readers_lock:
            reader = cls._readers.get(key)
            if reader is None:
                import easyocr

                t0 = time.perf_counter()
                reader = easyocr.Reader(list(languages), gpu=gpu, verbose=False)
                log.info("EasyOCR reader ready in %.1fs (gpu=%s)", time.perf_counter() - t0, gpu)
                cls._readers[key] = reader
        return reader

    def _init_applevision(self) -> None:
        import objc
        import Quartz
        import Vision

        self._objc, self._quartz, self._vision = objc, Quartz, Vision
        self._colorspace = Quartz.CGColorSpaceCreateDeviceRGB()
        # "fast" reads long base58 strings noticeably worse — use accurate
        self._vn_level = Vision.VNRequestTextRecognitionLevelAccurate
        # The first request loads the model (up to a minute cold): warm it up here,
        # not on the first stream frame.
        t0 = time.perf_counter()
        warm = np.full((64, 256, 3), 255, dtype=np.uint8)
        warm[24:40, 16:240] = 0
        list(self._run_applevision(warm))
        log.info("Apple Vision OCR ready in %.1fs (accurate, no language correction)", time.perf_counter() - t0)

    def _init_tesseract(self) -> None:
        import pytesseract

        try:
            version = pytesseract.get_tesseract_version()
        except pytesseract.TesseractNotFoundError as e:
            raise RuntimeError(
                "tesseract not found in PATH (brew install tesseract / apt install tesseract-ocr)"
            ) from e
        log.info("Tesseract %s", version)
        self._pytesseract = pytesseract

    # -- public ------------------------------------------------------------- #

    def extract_text(self, frame_bytes, width: int = 1280, height: int = 720, rois=None) -> list[dict]:
        """RGB bytes → [{text, bbox: (x1,y1,x2,y2), confidence, roi}], in reading order.

        bbox is in source-frame coordinates. rois=None → self.rois.
        """
        return self.extract_text_from_array(frame_to_array(frame_bytes, width, height), rois)

    def extract_text_from_array(self, frame: np.ndarray, rois=None) -> list[dict]:
        h, w = frame.shape[:2]
        blocks: list[dict] = []
        for roi in (self.rois if rois is None else rois):
            x1, y1, x2, y2 = roi.to_pixels(w, h)
            crop = frame[y1:y2, x1:x2]
            if crop.size == 0:
                continue
            if roi.scale != 1.0:
                crop = _resize(crop, roi.scale)
            for text, (bx1, by1, bx2, by2), conf in self._run(crop):
                text = text.strip()
                if not text or conf < self.min_confidence:
                    continue
                s = roi.scale
                blocks.append({
                    "text": text,
                    "bbox": (
                        max(0, int(x1 + bx1 / s)), max(0, int(y1 + by1 / s)),
                        min(w, int(x1 + bx2 / s)), min(h, int(y1 + by2 / s)),
                    ),
                    "confidence": round(float(conf), 4),
                    "roi": roi.name,
                })
        return sort_reading_order(_dedupe(blocks))

    # -- backends ----------------------------------------------------------- #

    def _run(self, img: np.ndarray):
        if self.engine == "easyocr":
            return self._run_easyocr(img)
        if self.engine == "applevision":
            return self._run_applevision(img)
        return self._run_tesseract(img)

    def _run_applevision(self, img: np.ndarray) -> list:
        # PyObjC in to_thread workers: the thread has no NSAutoreleasePool → autoreleased
        # CGImage/VNRequest/results accumulate (~MB leak per frame). One pool per frame;
        # results are materialized into Python objects inside the pool.
        with self._objc.autorelease_pool():
            return list(self._applevision_body(img))

    def _applevision_body(self, img: np.ndarray):
        Q, V = self._quartz, self._vision
        h, w = img.shape[:2]
        data = np.ascontiguousarray(img, dtype=np.uint8).tobytes()
        provider = Q.CGDataProviderCreateWithData(None, data, len(data), None)
        cg = Q.CGImageCreate(w, h, 8, 24, w * 3, self._colorspace, Q.kCGImageAlphaNone,
                             provider, None, False, Q.kCGRenderingIntentDefault)
        req = V.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(self._vn_level)
        # Language correction "fixes" base58 addresses and tickers into words — disable it
        req.setUsesLanguageCorrection_(False)
        handler = V.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            log.warning("Apple Vision OCR failed: %s", err)
            return
        for obs in req.results() or []:
            cand = obs.topCandidates_(1)
            if not cand:
                continue
            top = cand[0]
            bb = obs.boundingBox()  # normalized coordinates, origin at bottom-left
            x1 = bb.origin.x * w
            x2 = (bb.origin.x + bb.size.width) * w
            y2 = (1 - bb.origin.y) * h
            y1 = (1 - bb.origin.y - bb.size.height) * h
            yield str(top.string()), (x1, y1, x2, y2), float(top.confidence())

    def _run_easyocr(self, img: np.ndarray):
        # EasyOCR internally converts to grayscale assuming BGR — pass BGR
        bgr = np.ascontiguousarray(img[:, :, ::-1])
        for points, text, conf in self._reader.readtext(bgr, detail=1, paragraph=False):
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            yield text, (min(xs), min(ys), max(xs), max(ys)), conf

    def _run_tesseract(self, img: np.ndarray):
        from PIL import Image

        gray = (img[:, :, 0] * 0.299 + img[:, :, 1] * 0.587 + img[:, :, 2] * 0.114).astype(np.uint8)
        if gray.mean() < 128:  # dark trading UIs: Tesseract prefers dark text on light
            gray = 255 - gray
        out = self._pytesseract.Output
        data = self._pytesseract.image_to_data(
            Image.fromarray(gray), output_type=out.DICT, config="--psm 11"  # sparse text
        )
        # Words → lines (block, par, line) so "ticker: XXX" is a single block
        lines: dict[tuple, list[int]] = {}
        for i, word in enumerate(data["text"]):
            if not word.strip() or float(data["conf"][i]) < 0:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            lines.setdefault(key, []).append(i)
        for idx in lines.values():
            text = " ".join(data["text"][i] for i in idx)
            x1 = min(data["left"][i] for i in idx)
            y1 = min(data["top"][i] for i in idx)
            x2 = max(data["left"][i] + data["width"][i] for i in idx)
            y2 = max(data["top"][i] + data["height"][i] for i in idx)
            conf = sum(float(data["conf"][i]) for i in idx) / len(idx) / 100.0
            yield text, (x1, y1, x2, y2), conf
