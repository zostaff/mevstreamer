"""Распознавание торговой платформы и действий стримера.

Только дешёвые эвристики (цвета, формы блобов, ключевые слова OCR) — без ML.
Работает на прореженном фрейме (каждый 4-й пиксель → 320x180 при 720p).
"""

from __future__ import annotations

import re
from collections import deque

import numpy as np

try:
    import cv2
except ImportError:
    cv2 = None

try:
    from .ocr import bbox_cy, bbox_height, frame_to_array
except ImportError:
    from ocr import bbox_cy, bbox_height, frame_to_array

_STRIDE = 4

# Самый сильный сигнал — домен/бренд в адресной строке или в логотипе
_TEXT_HINTS = {
    "photon": ("photon-sol", "photon"),
    "bullx": ("bullx",),
    "gmgn": ("gmgn",),
    "pumpfun": ("pump.fun",),
    "dexscreener": ("dexscreener",),
}

_SUCCESS_RE = re.compile(r"\b(success|successful|successfully|confirmed|completed|landed)\b")
_CONFIRM_RE = re.compile(r"\bconfirm\b")
_BUY_RE = re.compile(r"\b(buy|bought|buying)\b")
_SELL_RE = re.compile(r"\b(sell|sold|selling)\b")
_SEARCH_RE = re.compile(r"\bsearch")
_TOKEN_PAGE_RE = re.compile(
    r"\b(market\s*cap|mcap|liquidity|holders|bonding\s*curve|top\s*10|dev\s*holds|fdv)\b"
)


def _color_mask(img: np.ndarray, rgb, tol: int = 60) -> np.ndarray:
    d = img.astype(np.int32) - np.asarray(rgb, dtype=np.int32)  # int16 переполнится на d*d
    return (d * d).sum(axis=-1) <= tol * tol


def _blobs(mask: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """Связные компоненты → [(x, y, w, h, area)]."""
    if not mask.any():
        return []
    if cv2 is not None:
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
        return [tuple(int(v) for v in stats[i]) for i in range(1, n)]
    # fallback без cv2: BFS по прореженной маске (маленькая, это быстро)
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    out = []
    for sy, sx in zip(*np.nonzero(mask)):
        if seen[sy, sx]:
            continue
        q = deque([(sy, sx)])
        seen[sy, sx] = True
        x1 = x2 = sx
        y1 = y2 = sy
        area = 0
        while q:
            y, x = q.popleft()
            area += 1
            x1, x2, y1, y2 = min(x1, x), max(x2, x), min(y1, y), max(y2, y)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        q.append((ny, nx))
        out.append((int(x1), int(y1), int(x2 - x1 + 1), int(y2 - y1 + 1), area))
    return out


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


class UIRecognizer:
    PLATFORMS = {
        "photon": {
            "colors": {"dominant_dark": True, "accent_green": (0, 255, 100), "accent_red": (255, 50, 50)},
            "mint_location": "top_bar",
            "buy_button_color": (0, 200, 80),
        },
        "bullx": {
            "colors": {"dominant_dark": True, "accent_blue": (50, 100, 255)},
            "mint_location": "header",
            "buy_button_color": (50, 120, 255),
        },
        "gmgn": {
            "colors": {"dominant_dark": True, "accent_green": (0, 200, 100)},
            "mint_location": "header",
        },
        "pumpfun": {
            "colors": {"accent_yellow": (255, 200, 0), "accent_green": (0, 200, 100)},
            "mint_location": "url_bar",
            "has_progress_bar": True,
        },
        "dexscreener": {
            "colors": {"dominant_dark": True, "chart_green": (0, 200, 0)},
            "mint_location": "header",
        },
    }

    MIN_PLATFORM_CONFIDENCE = 0.35

    # -- platform ----------------------------------------------------------- #

    def detect_platform(self, frame_bytes, width: int = 1280, height: int = 720, ocr_results=None) -> dict:
        """→ {platform: str|None, confidence: float, scores: {platform: score}}.

        ocr_results (опционально) — даёт текстовые подсказки (домен в URL bar),
        которые надёжнее цветов: у photon/gmgn/dexscreener почти одинаковые тёмные темы.
        """
        small = frame_to_array(frame_bytes, width, height)[::_STRIDE, ::_STRIDE]
        sh, sw = small.shape[:2]
        lum = small @ np.array([0.299, 0.587, 0.114])
        dark_ratio = float((lum < 60).mean())

        right = small[:, int(sw * 0.7):]
        text = " ".join(b["text"].lower() for b in ocr_results) if ocr_results else ""

        scores: dict[str, float] = {}
        for name, spec in self.PLATFORMS.items():
            colors = spec["colors"]
            score = 0.0
            if colors.get("dominant_dark"):
                score += 0.25 * _clamp(dark_ratio / 0.6)
            accents = [v for k, v in colors.items() if k != "dominant_dark"]
            if accents:
                # 1% пикселей нужного цвета — уже характерный акцент
                fracs = [_clamp(float(_color_mask(small, c, 50).mean()) / 0.01) for c in accents]
                score += 0.2 * sum(fracs) / len(fracs)
            if "buy_button_color" in spec and self._has_button(right, spec["buy_button_color"], sh * sw):
                score += 0.2
            if spec.get("has_progress_bar") and self._has_progress_bar(small, accents):
                score += 0.25
            if text and any(h in text for h in _TEXT_HINTS.get(name, ())):
                score += 0.6
            scores[name] = round(score, 3)

        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        (best, s1), (_, s2) = ranked[0], ranked[1]
        # штраф за неоднозначность: если второй кандидат близко — уверенность низкая
        confidence = round(_clamp(s1 - 0.5 * s2), 3)
        platform = best if confidence >= self.MIN_PLATFORM_CONFIDENCE else None
        return {"platform": platform, "confidence": confidence, "scores": scores}

    @staticmethod
    def _has_button(region: np.ndarray, rgb, frame_area: int) -> bool:
        """Залитый прямоугольник цвета кнопки: вытянутый по X, 0.05–3% площади фрейма."""
        for x, y, w, h, area in _blobs(_color_mask(region, rgb, 45)):
            if h < 3 or not 1.5 <= w / h <= 10:
                continue
            if not 0.0005 * frame_area <= area <= 0.03 * frame_area:
                continue
            if area / (w * h) > 0.7:  # «заливка», а не контур/свечи графика
                return True
        return False

    @staticmethod
    def _has_progress_bar(img: np.ndarray, colors) -> bool:
        """Тонкая длинная горизонтальная полоса (bonding curve progress)."""
        sw = img.shape[1]
        for rgb in colors:
            for x, y, w, h, area in _blobs(_color_mask(img, rgb, 50)):
                if w >= 0.08 * sw and h <= 6 and w / max(h, 1) >= 8 and area / (w * h) > 0.75:
                    return True
        return False

    # -- actions ------------------------------------------------------------ #

    def detect_actions(
        self, frame_bytes, ocr_results, platform, width: int = 1280, height: int = 720, addresses=None
    ) -> list[dict]:
        """→ [{action, confidence}] по убыванию confidence.

        Без известной платформы эвристики тоже работают, но с пониженной
        уверенностью (×0.7) — стример может сидеть на неизвестном терминале.
        addresses (опционально) — результат TokenDetector.find_addresses.
        """
        frame = frame_to_array(frame_bytes, width, height)
        fh = frame.shape[0]
        base = 1.0 if platform else 0.7
        actions: dict[str, float] = {}

        def add(name: str, conf: float) -> None:
            conf = round(_clamp(conf * base), 3)
            if conf > actions.get(name, 0.0):
                actions[name] = conf

        blocks = ocr_results or []
        modal = self._detect_modal(frame[::_STRIDE, ::_STRIDE])

        for b in blocks:
            t = b["text"].lower()
            ctx = self._nearby_text(blocks, b)

            # bought / sold: тост "Success"/"Confirmed", лучше всего — на зелёном фоне
            if _SUCCESS_RE.search(t):
                green = self._is_greenish_around(frame, b["bbox"])
                is_sell = bool(_SELL_RE.search(ctx))
                explicit = is_sell or bool(_BUY_RE.search(ctx))
                add("sold" if is_sell else "bought", 0.5 + 0.3 * green + 0.15 * explicit)

            # about_to_buy / about_to_sell: окно подтверждения
            if _CONFIRM_RE.search(t):
                if _SELL_RE.search(ctx):
                    add("about_to_sell", 0.65 + 0.2 * modal)
                elif _BUY_RE.search(ctx):
                    add("about_to_buy", 0.65 + 0.2 * modal)

            # searching: плейсхолдер "Search..." висит в header всегда, поэтому
            # считаем только если поиск открыт в модалке/дропдауне ниже header'а
            if _SEARCH_RE.search(t):
                if modal:
                    add("searching", 0.75)
                elif bbox_cy(b) > 0.15 * fh:
                    add("searching", 0.55)

        # viewing_token: суммируем признаки страницы токена
        alltext = " ".join(b["text"].lower() for b in blocks)
        signal = 0.0
        if addresses and any(a["type"] == "mint" for a in addresses):
            signal += 0.4
        if _BUY_RE.search(alltext) and _SELL_RE.search(alltext):
            signal += 0.25
        if _TOKEN_PAGE_RE.search(alltext):
            signal += 0.2
        if platform:
            signal += 0.15
        if signal >= 0.4:
            add("viewing_token", signal)

        return [{"action": k, "confidence": v} for k, v in sorted(actions.items(), key=lambda kv: -kv[1])]

    @staticmethod
    def _nearby_text(blocks: list[dict], b: dict) -> str:
        """Текст блока + соседей в пределах ~3 строк (заголовок/кнопки тоста или попапа)."""
        h = bbox_height(b)
        cy = bbox_cy(b)
        x1, _, x2, _ = b["bbox"]
        out = []
        for o in blocks:
            ox1, _, ox2, _ = o["bbox"]
            if abs(bbox_cy(o) - cy) <= 3 * h and ox1 <= x2 + 300 and ox2 >= x1 - 300:
                out.append(o["text"].lower())
        return " ".join(out)

    @staticmethod
    def _is_greenish_around(frame: np.ndarray, bbox) -> bool:
        """Зелёный фон/иконка вокруг текста — признак success-тоста."""
        x1, y1, x2, y2 = bbox
        pad = max(4, (y2 - y1) // 2)
        h, w = frame.shape[:2]
        region = frame[max(0, y1 - pad):min(h, y2 + pad), max(0, x1 - 3 * pad):min(w, x2 + pad)]
        if region.size == 0:
            return False
        r, g, b_ = (region[..., i].astype(np.int16) for i in range(3))
        green = (g > 100) & (g > r + 40) & (g > b_ + 20)
        return float(green.mean()) > 0.12

    @staticmethod
    def _detect_modal(small: np.ndarray) -> bool:
        """Модалка: в центре большой однотонный прямоугольник, цвета которого
        почти нет по краям экрана (фон под оверлеем другой)."""
        h, w = small.shape[:2]
        q = (small >> 4).astype(np.int32)
        codes = (q[..., 0] << 8) | (q[..., 1] << 4) | q[..., 2]
        center = codes[int(h * 0.3):int(h * 0.7), int(w * 0.35):int(w * 0.65)]
        vals, counts = np.unique(center, return_counts=True)
        mode = vals[counts.argmax()]
        center_frac = counts.max() / center.size
        ring = np.concatenate([
            codes[: int(h * 0.1)].ravel(), codes[int(h * 0.9):].ravel(),
            codes[:, : int(w * 0.1)].ravel(), codes[:, int(w * 0.9):].ravel(),
        ])
        ring_frac = float((ring == mode).mean())
        return bool(center_frac > 0.35 and ring_frac < 0.08)
