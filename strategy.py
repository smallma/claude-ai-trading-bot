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


def _ov(settings: dict[str, Any], key: str, default: Any) -> Any:
    """Read from settings['strategy_overrides'][key] if present, else config default.

    Lets strategy_reviewer.py tune RSI/EMA/BB parameters via config.json without
    touching config.py. Bot picks the new value up on the next tick.
    """
    overrides = (settings or {}).get("strategy_overrides") or {}
    val = overrides.get(key)
    return val if val is not None else default


def decide(closes: list[float], settings: dict[str, Any]) -> tuple[Signal, dict]:
    rsi_oversold_thr = float(_ov(settings, "RSI_OVERSOLD", config.RSI_OVERSOLD))
    rsi_overbought_thr = float(_ov(settings, "RSI_OVERBOUGHT", config.RSI_OVERBOUGHT))
    ema_fast_period = int(_ov(settings, "EMA_FAST_PERIOD", config.EMA_FAST_PERIOD))
    ema_slow_period = int(_ov(settings, "EMA_SLOW_PERIOD", config.EMA_SLOW_PERIOD))
    bb_period = int(_ov(settings, "BB_PERIOD", config.BB_PERIOD))
    bb_stdev = float(_ov(settings, "BB_STDEV", config.BB_STDEV))

    rsi = _rsi(closes, config.RSI_PERIOD)
    ema_fast = _ema(closes, ema_fast_period)
    ema_slow = _ema(closes, ema_slow_period)
    bb_upper, bb_mid, bb_lower = _bbands(closes, bb_period, bb_stdev)
    last_close = closes[-1]

    # EMA spread threshold: require >0.05% gap to declare a trend.
    # If fast/slow are too close ("glued"), force FLAT to avoid false breakouts.
    ema_spread = abs(ema_fast - ema_slow) / ema_slow if ema_slow != 0 else 0.0
    EMA_SPREAD_THRESHOLD = 0.0005  # 0.05%
    if ema_spread < EMA_SPREAD_THRESHOLD:
        ema_trend = "FLAT"
    elif ema_fast > ema_slow:
        ema_trend = "BULL"
    else:
        ema_trend = "BEAR"
    bb_pos = _bb_position(last_close, bb_lower, bb_upper)

    info = {
        "rsi": round(rsi, 2),
        "ema_fast": round(ema_fast, 4),
        "ema_slow": round(ema_slow, 4),
        "ema_trend": ema_trend,
        "ema_spread_pct": round(ema_spread * 100, 4),
        "bb_upper": round(bb_upper, 4),
        "bb_mid": round(bb_mid, 4),
        "bb_lower": round(bb_lower, 4),
        "bb_position": bb_pos,
        "last_close": last_close,
        "thresholds": (rsi_oversold_thr, rsi_overbought_thr),
        "params_used": {
            "RSI_OVERSOLD": rsi_oversold_thr,
            "RSI_OVERBOUGHT": rsi_overbought_thr,
            "EMA_FAST_PERIOD": ema_fast_period,
            "EMA_SLOW_PERIOD": ema_slow_period,
            "BB_PERIOD": bb_period,
            "BB_STDEV": bb_stdev,
        },
    }

    rsi_oversold = rsi < rsi_oversold_thr
    rsi_overbought = rsi > rsi_overbought_thr
    bb_break_low = last_close < bb_lower
    bb_break_high = last_close > bb_upper

    # EMA FLAT = consolidation -> no entries
    if ema_trend == "FLAT":
        return "HOLD", info

    if ema_trend == "BULL":
        if rsi_oversold:
            info["trigger"] = "RSI oversold"
            return "BUY", info
        # BB lower break requires RSI dual confirmation (prevent catching knives)
        if bb_break_low and rsi_oversold:
            info["trigger"] = "BB lower break + RSI confirm"
            return "BUY", info

    if ema_trend == "BEAR":
        if rsi_overbought:
            info["trigger"] = "RSI overbought"
            return "SELL", info
        # BB upper break requires RSI dual confirmation
        if bb_break_high and rsi_overbought:
            info["trigger"] = "BB upper break + RSI confirm"
            return "SELL", info

    return "HOLD", info
