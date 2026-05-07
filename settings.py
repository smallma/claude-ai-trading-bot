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
    "AUTO_CAPITAL_TUNE": True,
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
    # Editable prompt templates for AI
    "AI_ROUND1_PROMPT": "You are a crypto market sentiment analyst tuning SHARED parameters\nfor an automated perpetual futures bot trading a basket of {symbols_str}.\nYour scoring should reflect the broad crypto regime (it applies to all symbols).\n\nRecent crypto headlines (last hour, multi-source):\n{bullets}\n\nFear & Greed Index: {fng_block}\n\nCurrent per-symbol market state:\n{market_ctx_str}\n\nOutput strictly three lines:\nSCORE: <integer 1-10>\nCONFIDENCE: <decimal 0.0-1.0>\nREASON: <one short sentence>",
    "AI_JUDGE_PROMPT": "You are the FINAL JUDGE re-evaluating an initial analyst opinion\nagainst fresh macro/on-chain data, producing ONE shared parameter decision for a\nmulti-symbol perp bot trading the basket [{symbols_str}].\n\n=== CRITICAL RULES (highest priority — override all other heuristics) ===\n1. You MUST evaluate the provided Sentiment Score and Reason. If the sentiment\n   indicates bearish conditions OR warns of rising BTC dominance (>60% or\n   climbing), you MUST apply a HEAVY penalty to any BUY signals. Do NOT\n   blindly follow the EMA trend if the macro sentiment is explicitly negative.\n2. Conversely, if sentiment is highly bullish (score >= 8), restrict SELL signals\n   — require stronger technical confirmation before scoring bearishly.\n3. When Fear & Greed is below 25 (Extreme Fear), presume continued downside\n   unless strong reversal evidence exists. When above 75 (Extreme Greed),\n   presume overextension and penalize aggressive longs.\n\n=== Initial analyst opinion (Round 1) ===\n{r1_summary}\n\n=== Macro & on-chain (Round 2) ===\n- BTC Dominance: {dom_block}  (rising = capital fleeing alts to BTC)\n- Per-symbol funding (8h, positive = longs pay shorts; >0.05% = crowded long):\n{funding_block}\n- Fear & Greed: {fng_block}\n\n=== Per-symbol market state ===\n{market_ctx_str}\n\n=== Reference headlines (top 8) ===\n{bullets}\n\n=== Your job ===\nRe-examine the analyst view against the fresh data. The basket includes alts\n(SOL, ADA), so penalize bullishness when BTC dominance is climbing. Penalize\nbullishness if multiple symbols show crowded-long funding. Reward bearishness\nif multiple symbols are already RSI-stretched in the opposite direction of the news.\n\nBe more decisive than the initial analyst alone (use supplementary data to\nsharpen the call). Stay within the same output schema.\n\nOutput strictly three lines:\nSCORE: <integer 1-10>\nCONFIDENCE: <decimal 0.0-1.0>\nREASON: <one short sentence on what shifted vs the initial analyst view>",
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
