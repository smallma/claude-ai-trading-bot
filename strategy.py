"""Strategy module — the only function you need to swap is `decide`.

Inputs: a list of recent close prices (oldest -> newest).
Output: ("BUY"|"SELL"|"HOLD", info_dict).
"""
from typing import Literal

import config

Signal = Literal["BUY", "SELL", "HOLD"]


def _rsi(closes: list[float], period: int) -> float:
    if len(closes) < period + 1:
        raise ValueError(f"Need at least {period + 1} closes for RSI({period}), got {len(closes)}")

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    # Wilder's smoothing
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def decide(closes: list[float]) -> tuple[Signal, dict]:
    """Return (signal, debug_info).

    Replace the body of this function to swap strategies. Keep the signature.
    Rule: RSI < 30 -> BUY, RSI > 70 -> SELL, otherwise HOLD.
    """
    rsi = _rsi(closes, config.RSI_PERIOD)
    info = {"rsi": round(rsi, 2), "last_close": closes[-1]}
    if rsi < config.RSI_OVERSOLD:
        return "BUY", info
    if rsi > config.RSI_OVERBOUGHT:
        return "SELL", info
    return "HOLD", info
