"""Vision pipeline: OCR → addresses/tickers → platform/actions.

Standalone test on a screenshot:
    python pipeline.py screenshot.jpg
    python pipeline.py screenshot.jpg --engine tesseract --full --json
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

try:
    from .config import VisionConfig
    from .detector import TokenDetector
    from .metrics import extract_metrics, title_candidate
    from .mint_repair import MintVerifier
    from .ocr import DEFAULT_ROIS, FULL_FRAME_ROIS, OCREngine, frame_to_array, resolve_engine
    from .ui_recognizer import UIRecognizer
except ImportError:  # python pipeline.py from the vision/ directory
    from config import VisionConfig
    from detector import TokenDetector
    from metrics import extract_metrics, title_candidate
    from mint_repair import MintVerifier
    from ocr import DEFAULT_ROIS, FULL_FRAME_ROIS, OCREngine, frame_to_array, resolve_engine
    from ui_recognizer import UIRecognizer


@dataclass
class VisionResult:
    addresses: list[dict]
    tickers: list[dict]
    platform: str | None
    platform_confidence: float
    actions: list[dict]
    timestamp: float
    processing_ms: float
    timings_ms: dict = field(default_factory=dict)
    # on-screen market_cap_usd / liquidity_usd / holders — to identify a token without its address
    metrics: dict = field(default_factory=dict)
    text_blocks: list[dict] = field(default_factory=list)  # raw OCR, for debugging

    def to_dict(self, include_text: bool = False) -> dict:
        d = asdict(self)
        if not include_text:
            d.pop("text_blocks")
        return d

    @property
    def mints(self) -> list[str]:
        return [a["address"] for a in self.addresses if a["type"] == "mint"]


class VisionPipeline:
    def __init__(self, config: VisionConfig | None = None):
        config = config or VisionConfig.from_env()
        self.config = config
        engine = resolve_engine(config.ocr_engine)
        full = config.roi_mode == "full" or (config.roi_mode == "auto" and engine == "applevision")
        rois = FULL_FRAME_ROIS if full else DEFAULT_ROIS
        # Heavy initialization (EasyOCR Reader) happens once, here
        self.ocr = OCREngine(
            engine=engine, gpu=config.ocr_gpu,
            min_confidence=config.ocr_min_confidence, rois=rois,
        )
        self.detector = TokenDetector()
        self.mint_verifier = MintVerifier() if config.mint_repair else None
        self.ui_recognizer = UIRecognizer() if config.ui_detection else None

    def process_frame(self, frame_bytes, width: int = 1280, height: int = 720, timestamp: float | None = None) -> VisionResult:
        """frame_bytes: RGB bytes (W*H*3) or numpy (H, W, 3).
        timestamp: capture time from ingestion (queue ts); defaults to now."""
        t0 = time.perf_counter()
        ts = time.time() if timestamp is None else timestamp
        frame = frame_to_array(frame_bytes, width, height)
        h, w = frame.shape[:2]
        timings = {}

        t = time.perf_counter()
        blocks = self.ocr.extract_text_from_array(frame)
        timings["ocr"] = _ms(t)

        t = time.perf_counter()
        addresses = self.detector.find_addresses(blocks)
        tickers = self.detector.find_tickers(blocks)
        metrics: dict = extract_metrics(blocks)
        if "market_cap_usd" in metrics:
            title = title_candidate(blocks, h)
            if title:
                metrics["title"] = title
        timings["detect"] = _ms(t)

        # OCR corrupted the mint (1→I, 2→Z, …): repair and verify on-chain (cached per string).
        if self.mint_verifier is not None and not any(a["type"] == "mint" for a in addresses):
            near = self.detector.find_near_mints(blocks)
            if near:
                t = time.perf_counter()
                for nm in near:
                    mint = self.mint_verifier.resolve(nm["raw"])
                    if mint:
                        addresses.insert(0, {"address": mint, "type": "mint", "bbox": nm["bbox"],
                                             "confidence": round(nm["confidence"] * 0.9, 4),
                                             "source": nm["source"], "repaired_from": nm["raw"]})
                timings["repair"] = _ms(t)

        platform, platform_conf, actions = None, 0.0, []
        if self.ui_recognizer is not None:
            t = time.perf_counter()
            p = self.ui_recognizer.detect_platform(frame, w, h, ocr_results=blocks)
            platform, platform_conf = p["platform"], p["confidence"]
            actions = self.ui_recognizer.detect_actions(frame, blocks, platform, w, h, addresses=addresses)
            timings["ui"] = _ms(t)

        return VisionResult(
            addresses=addresses,
            tickers=tickers,
            platform=platform,
            platform_confidence=platform_conf,
            actions=actions,
            timestamp=ts,
            processing_ms=_ms(t0),
            timings_ms=timings,
            metrics=metrics,
            text_blocks=blocks,
        )


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _main(argv=None) -> int:
    import argparse
    import json
    import logging

    import numpy as np
    from PIL import Image

    ap = argparse.ArgumentParser(description="Vision pipeline on a single screenshot")
    ap.add_argument("image", help="path to screenshot (jpg/png)")
    ap.add_argument("--engine", choices=["auto", "applevision", "easyocr", "tesseract"], help="overrides OCR_ENGINE")
    ap.add_argument("--cpu", action="store_true", help="EasyOCR without GPU")
    ap.add_argument("--full", action="store_true", help="OCR the full frame instead of ROIs")
    ap.add_argument("--no-ui", action="store_true", help="skip platform/action recognition")
    ap.add_argument("--resize", action="store_true", help="resize to 1280x720 as in the stream")
    ap.add_argument("--text", action="store_true", help="show all OCR blocks")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    env = VisionConfig.from_env()
    cfg = VisionConfig(
        ocr_engine=args.engine or env.ocr_engine,
        ocr_gpu=env.ocr_gpu and not args.cpu,
        ui_detection=env.ui_detection and not args.no_ui,
        roi_mode="full" if args.full else env.roi_mode,
        ocr_min_confidence=env.ocr_min_confidence,
    )

    img = Image.open(args.image).convert("RGB")
    if args.resize:
        img = img.resize((1280, 720), Image.BICUBIC)
    width, height = img.size

    t = time.perf_counter()
    pipe = VisionPipeline(cfg)
    init_ms = _ms(t)
    result = pipe.process_frame(np.asarray(img), width, height)

    if args.json:
        print(json.dumps(result.to_dict(include_text=args.text), ensure_ascii=False, indent=2, default=list))
        return 0

    print(f"\n{args.image}  {width}x{height}  engine={cfg.ocr_engine} rois={cfg.roi_mode}")
    print(f"init {init_ms} ms | frame {result.processing_ms} ms {result.timings_ms}")
    if args.text:
        print(f"\nOCR ({len(result.text_blocks)} blocks):")
        for b in result.text_blocks:
            print(f"  [{b['roi']:<11}] {b['confidence']:.2f} {b['bbox']}  {b['text']}")
    print("\nAddresses:")
    for a in result.addresses or [None]:
        print(f"  {a['type']:<6} {a['confidence']:.2f}  {a['address']}  ({a['source']})" if a else "  —")
    print(f"Metrics: {result.metrics or '—'}")
    print("Tickers:")
    for tk in result.tickers or [None]:
        print(f"  {tk['confidence']:.2f}  ${tk['ticker']}" if tk else "  —")
    if pipe.ui_recognizer is not None:
        print(f"Platform: {result.platform or '—'} ({result.platform_confidence:.2f})")
        print("Actions:")
        for ac in result.actions or [None]:
            print(f"  {ac['confidence']:.2f}  {ac['action']}" if ac else "  —")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
