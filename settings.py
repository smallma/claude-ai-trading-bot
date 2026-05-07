"""Atomic load/save for config.json (the bot-AI shared dynamic settings).

Both bot.py and ai_analyst.py touch this file, so writes go via a temp file +
os.replace (atomic on POSIX) and reads use a small retry to absorb the brief
window where the temp file is being moved into place.
"""
import json
import os
import time
from pathlib import Path
from typing import Any

import config

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULTS: dict[str, Any] = {
    "TRADE_SIZE_MULTIPLIER": 1.0,
    "DAILY_LOSS_LIMIT": 0.02,
    # Approval gates — when False, AI/reviewer write SUGGESTIONS into ai_meta
    # and wait for the dashboard operator to apply them. When True, changes
    # take effect automatically.
    "AUTO_CAPITAL_TUNE": False,
    "AUTO_STRATEGY_EVOLVE": False,
    # AI confirmation gate before placing orders. False = pure RSI/EMA/BB
    # signals execute directly; True = both Gemini + MiniMax must vote GO.
    "TRADE_GATE_ENABLED": True,
    # Per-key overrides written by strategy_reviewer.py. Keys mirror config.py
    # constant names (RSI_OVERSOLD, RSI_OVERBOUGHT, EMA_FAST_PERIOD,
    # EMA_SLOW_PERIOD, BB_PERIOD, BB_STDEV). Empty by default = use config.py.
    "strategy_overrides": {},
    # Per-symbol live trading parameters edited from the dashboard. Bot reads
    # these on every tick; leverage changes are pushed to Hyperliquid via
    # exchange.update_leverage(). Seeded from config.py constants on first load.
    "symbol_configs": {
        sym: {
            "base_usd": float(config.BASE_TRADE_SIZE_USD.get(sym, 0.0)),
            "leverage": int(config.DEFAULT_LEVERAGE),
        }
        for sym in config.SYMBOLS
    },
    # Active trading universe — editable from the dashboard. Empty/missing
    # falls back to config.SYMBOLS in callers.
    "symbols": list(config.SYMBOLS),
    # Manual close requests posted by the dashboard. Bot drains this list at
    # the top of each tick: market_close + journal each entry, then resets [].
    "force_close_queue": [],
    "ai_meta": {
        "last_sentiment": None,
        "last_updated": None,
        "last_reason": None,
    },
}


def load() -> dict[str, Any]:
    """Read config.json. Falls back to DEFAULTS for any missing keys."""
    last_err: Exception | None = None
    for _ in range(3):
        try:
            with CONFIG_PATH.open("r") as f:
                data = json.load(f)
            return {**DEFAULTS, **data}
        except (FileNotFoundError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(0.05)
    # If we still can't read, return defaults rather than crash the loop.
    return dict(DEFAULTS)


def save(data: dict[str, Any]) -> None:
    """Atomically replace config.json so concurrent readers never see a partial write."""
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
