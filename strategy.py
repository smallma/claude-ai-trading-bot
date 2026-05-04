"""Composite strategy: EMA trend filter + (RSI extreme OR Bollinger breakout).

decide(closes, settings) -> ("BUY"|"SELL"|"HOLD", info_dict)

BUY  : EMA9 > EMA21  AND  (RSI < RSI_OVERSOLD   OR  close < BB_LOWER)
SELL : EMA9 < EMA21  AND  (RSI > RSI_OVERBOUGHT OR  close > BB_UPPER)
HOLD : otherwise

Pure-Python implementations to avoid the pandas-ta dependency.
"""
from typing import Any, Literal, Optional

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

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(closes: list[float], period: int) -> float:
    """Standard EMA seeded with the first close. Needs ~3x period for full convergence."""
    if not closes:
        raise ValueError("EMA needs at least one close")
    k = 2.0 / (period + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = (c - ema) * k + ema
    return ema


def _bbands(closes: list[float], period: int, stdev_mult: float
            ) -> tuple[float, float, float]:
    """Returns (upper, middle, lower) Bollinger Bands using the last `period` closes."""
    if len(closes) < period:
        raise ValueError(f"Need at least {period} closes for BB, got {len(closes)}")
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    stdev = variance ** 0.5
    return mean + stdev_mult * stdev, mean, mean - stdev_mult * stdev


def _bb_position(close: float, lower: float, upper: float) -> str:
    if close > upper:
        return "above_upper"
    if close < lower:
        return "below_lower"
    return "inside"


def decide(closes: list[float], settings: dict[str, Any]) -> tuple[Signal, dict]:
    rsi = _rsi(closes, config.RSI_PERIOD)
    ema_fast = _ema(closes, config.EMA_FAST_PERIOD)
    ema_slow = _ema(closes, config.EMA_SLOW_PERIOD)
    bb_upper, bb_mid, bb_lower = _bbands(closes, config.BB_PERIOD, config.BB_STDEV)
    last_close = closes[-1]

    ema_trend = "BULL" if ema_fast > ema_slow else "BEAR"
    bb_pos = _bb_position(last_close, bb_lower, bb_upper)

    info = {
        "rsi": round(rsi, 2),
        "ema_fast": round(ema_fast, 4),
        "ema_slow": round(ema_slow, 4),
        "ema_trend": ema_trend,
        "bb_upper": round(bb_upper, 4),
        "bb_mid": round(bb_mid, 4),
        "bb_lower": round(bb_lower, 4),
        "bb_position": bb_pos,
        "last_close": last_close,
        "thresholds": (config.RSI_OVERSOLD, config.RSI_OVERBOUGHT),
    }

    rsi_oversold = rsi < config.RSI_OVERSOLD
    rsi_overbought = rsi > config.RSI_OVERBOUGHT
    bb_break_low = last_close < bb_lower
    bb_break_high = last_close > bb_upper

    if ema_trend == "BULL" and (rsi_oversold or bb_break_low):
        info["trigger"] = "RSI oversold" if rsi_oversold else "BB lower break"
        return "BUY", info
    if ema_trend == "BEAR" and (rsi_overbought or bb_break_high):
        info["trigger"] = "RSI overbought" if rsi_overbought else "BB upper break"
        return "SELL", info
    return "HOLD", info
