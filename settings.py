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

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULTS: dict[str, Any] = {
    "TRADE_SIZE_MULTIPLIER": 1.0,
    "DAILY_LOSS_LIMIT": 0.02,
    # Approval gates — when False, AI/reviewer write SUGGESTIONS into ai_meta
    # and wait for the dashboard operator to apply them. When True, changes
    # take effect automatically.
    "AUTO_CAPITAL_TUNE": False,
    "AUTO_STRATEGY_EVOLVE": False,
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
