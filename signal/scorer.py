"""Confidence scoring: (VisionObservation, AudioObservation) → Signal."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

try:
    from .aggregator import AudioObservation, VisionObservation
except ImportError:  # run as a script: signal/ is on sys.path
    from aggregator import AudioObservation, VisionObservation

Action = Literal["buy", "watch", "sell"]
Source = Literal["vision", "audio", "both"]


def short_mint(mint: str | None) -> str:
    if not mint:
        return "-"
    return f"{mint[:4]}...{mint[-4:]}" if len(mint) > 12 else mint


@dataclass(frozen=True)
class Signal:
    mint: str | None
    ticker: str | None
    action: Action
    confidence: float
    source: Source
    context: str
    timestamp: float
    # Below: logging/debugging only, not sent to the executor
    components: dict[str, float] = field(default_factory=dict, compare=False)
    details: tuple[str, ...] = field(default=(), compare=False)
    capture_ts: float | None = field(default=None, compare=False)

    @property
    def token_key(self) -> str | None:
        if self.mint:
            return self.mint
        return f"${self.ticker}" if self.ticker else None

    def to_dict(self) -> dict:
        """Wire format for the Rust executor (see ipc.py)."""
        return {
            "mint": self.mint,
            "ticker": self.ticker or "",  # Rust: ticker: String — null does not deserialize
            "action": self.action,
            "confidence": round(self.confidence, 4),
            "source": self.source,
            "context": self.context,
            "timestamp": round(self.timestamp, 3),
        }


class SignalScorer:
    WEIGHTS = {
        "visual_mint_detected":      0.50,  # OCR found a mint address on screen
        "visual_ticker_detected":    0.20,  # OCR found a ticker
        "audio_buy_phrase":          0.30,  # Whisper detected a buy phrase
        "audio_ticker":              0.15,  # Whisper extracted a ticker from speech
        "platform_detected":         0.15,  # Trading platform identified
        "action_viewing_token":      0.10,  # Streamer is viewing the token page
        "action_about_to_buy":       0.30,  # Streamer is on the Buy button
        "action_bought":             0.40,  # Streamer already bought (toast)
        "both_channels_confirm":     0.20,  # Visual and audio both see the same token
    }
    TICKER_WITHOUT_MINT_PENALTY = 0.15

    _VISION_KEYS = frozenset({
        "visual_mint_detected", "visual_ticker_detected", "platform_detected",
        "action_viewing_token", "action_about_to_buy", "action_bought",
    })
    _AUDIO_KEYS = frozenset({"audio_buy_phrase", "audio_ticker"})

    def __init__(self, weights: dict[str, float] | None = None):
        self.weights = {**self.WEIGHTS, **(weights or {})}

    def score(
        self,
        vision_result: VisionObservation | None,
        audio_result: AudioObservation | None,
        now: float | None = None,
    ) -> Signal | None:
        """Return a Signal, or None if there is nothing to attach it to (no mint and no ticker).

        Order: sum of weights (including +0.20 for confirmation by both channels)
        → cap 1.0 → −0.15 penalty for a ticker without a mint → clamp [0, 1].
        The penalty is applied after the cap, otherwise for strong ticker-only signals it
        would just be swallowed by the clip to 1.0.
        """
        v, a = vision_result, audio_result
        v_mints = v.mints if v else ()
        v_tickers = v.tickers if v else ()
        a_mints = a.mints if a else ()
        a_tickers = a.tickers if a else ()

        common_tickers = [t for t in v_tickers if t in a_tickers]
        confirmed_mint = next((m for m in v_mints if m in a_mints), None)

        mint = confirmed_mint or (v_mints[0] if v_mints else None) or (a_mints[0] if a_mints else None)
        if common_tickers:
            ticker = common_tickers[0]
        elif v_tickers:
            ticker = v_tickers[0]
        elif a_tickers:
            ticker = a_tickers[0]
        else:
            ticker = None
        if not mint and not ticker:
            return None

        w = self.weights
        comps: dict[str, float] = {}
        if v_mints:
            # mint weight × OCR/address confidence (mint_repair results ≤0.9)
            f = v.mint_factor(mint) if mint in v_mints else v.mint_factor(v_mints[0])
            comps["visual_mint_detected"] = round(w["visual_mint_detected"] * f, 4)
        if v_tickers:
            comps["visual_ticker_detected"] = w["visual_ticker_detected"]
        if v and v.platform:
            comps["platform_detected"] = w["platform_detected"]
        if v and v.action in ("viewing_token", "about_to_buy", "bought"):
            comps[f"action_{v.action}"] = w[f"action_{v.action}"]
        if a and a.buy_phrase:
            comps["audio_buy_phrase"] = w["audio_buy_phrase"]
        if a_tickers:
            comps["audio_ticker"] = w["audio_ticker"]
        if confirmed_mint or common_tickers:
            comps["both_channels_confirm"] = w["both_channels_confirm"]

        confidence = min(1.0, sum(comps.values()))
        if ticker and not mint:
            comps["penalty_ticker_without_mint"] = -self.TICKER_WITHOUT_MINT_PENALTY
            confidence -= self.TICKER_WITHOUT_MINT_PENALTY
        confidence = max(0.0, min(1.0, round(confidence, 4)))

        v_active = bool(self._VISION_KEYS & comps.keys()) or bool(v and v.action in ("sold", "about_to_sell"))
        a_active = bool(self._AUDIO_KEYS & comps.keys()) or bool(a and (a.sell_phrase or a_mints))
        source: Source = "both" if v_active and a_active else ("vision" if v_active else "audio")

        context, details = self._describe(v, a, mint, ticker)
        action = self._action(v, a)
        if action == "buy" and v is not None and v.ambiguous_mints:
            # >1 distinct mints on screen (chat overlay?): don't guess which one to buy
            action = "watch"
            details = (*details, "ambiguous mints on screen — BUY suppressed")
        # wire timestamp = capture time of the freshest evidence (frame or
        # end of audio chunk), not scoring time: the executor drops stale signals
        evidence_ts = [o.timestamp for o in (v, a) if o is not None and o.timestamp]
        return Signal(
            mint=mint,
            ticker=ticker,
            action=action,
            confidence=confidence,
            source=source,
            context=context,
            timestamp=max(evidence_ts) if evidence_ts else (time.time() if now is None else now),
            components=comps,
            details=details,
        )

    @staticmethod
    def _action(v: VisionObservation | None, a: AudioObservation | None) -> Action:
        v_action = v.action if v else None
        buy_ui = v_action in ("about_to_buy", "bought")
        if v_action in ("sold", "about_to_sell"):
            return "sell"
        if a and a.sell_phrase and not buy_ui and not a.buy_phrase:
            return "sell"
        if buy_ui or (a and a.buy_phrase):
            return "buy"
        return "watch"

    @staticmethod
    def _describe(v, a, mint, ticker) -> tuple[str, tuple[str, ...]]:
        ctx: list[str] = []
        details: list[str] = []
        ui = {
            "viewing_token": "on token page",
            "about_to_buy": "streamer on Buy button",
            "bought": "buy toast",
            "sold": "sell toast",
            "about_to_sell": "streamer on Sell confirm",
        }
        if v:
            where = f" on {v.platform}" if v.platform else ""
            what = "mint" if v.mints else (f"${v.tickers[0]}" if v.tickers else None)
            parts = []
            if what:
                parts.append(f"{what} detected{where}")
            elif v.platform:
                parts.append(f"platform {v.platform}")
            if v.action:
                parts.append(ui[v.action])
            if parts:
                details.append("Visual: " + ", ".join(parts))
                ctx.append("OCR: " + ", ".join(p.replace(" detected", "") for p in parts))
        if a:
            phrase = a.buy_phrase or a.sell_phrase
            if phrase:
                details.append(f'Audio: "{phrase}"')
                ctx.append(f"Audio: '{phrase}'")
            elif a.tickers:
                snippet = a.transcript.strip()[-80:] or f"${a.tickers[0]}"
                details.append(f'Audio: "{snippet}"')
                ctx.append(f"Audio: ${a.tickers[0]}")
        return " + ".join(ctx) or f"token {short_mint(mint) if mint else '$' + str(ticker)}", tuple(details)
