"""Static configuration. Values that should NEVER be touched by the AI live here.

Dynamic, AI-tunable parameters (TRADE_SIZE_MULTIPLIER, RSI thresholds, DAILY_LOSS_LIMIT)
have moved to config.json — see settings.py.

Per-symbol BASE size is static; AI scales it with TRADE_SIZE_MULTIPLIER (typ. 0.4-1.25x).
"""

SYMBOLS = ["SOL", "ETH", "ADA"]

# Per-symbol base trade size — fallback only. The runtime value is read from
# config.json -> symbol_configs[symbol].base_usd, which the dashboard edits.
BASE_TRADE_SIZE_USD = {
    "SOL": 40.0,
    "ETH": 40.0,
    "ADA": 20.0,
}

# Per-symbol leverage default — fallback only. Runtime value lives in
# config.json -> symbol_configs[symbol].leverage. Bot pushes the value to
# Hyperliquid via exchange.update_leverage() whenever it changes.
DEFAULT_LEVERAGE = 20
DEFAULT_LEVERAGE_IS_CROSS = True

# When the dashboard adds a brand-new symbol that has no symbol_configs entry,
# the bot auto-seeds one with these values so it doesn't crash on the next tick.
# Keep base_usd modest — the user can tune up after observing market behaviour.
NEW_SYMBOL_DEFAULT_BASE_USD = 20.0
NEW_SYMBOL_DEFAULT_LEVERAGE = 20

# Max simultaneous open positions PER SYMBOL (each symbol tracked independently).
MAX_OPEN_POSITIONS_PER_SYMBOL = 1

LOOP_SECONDS = 60
CANDLE_INTERVAL = "1m"
CANDLE_LOOKBACK = 100

RSI_PERIOD = 14

# RSI thresholds are LOCKED — AI cannot tune them. Only triggers entries when
# the market is at a true extreme. Same for all symbols.
RSI_OVERSOLD = 25.0
RSI_OVERBOUGHT = 75.0

# EMA trend filter (fast vs slow). EMA_FAST > EMA_SLOW = bull regime.
EMA_FAST_PERIOD = 9
EMA_SLOW_PERIOD = 21

# Bollinger Bands (period, stdev multiplier). Used as breakout entry trigger.
BB_PERIOD = 20
BB_STDEV = 2.0

# Trailing stop tiers (ROE% based, computed as unrealizedPnl / marginUsed).
# When max-seen ROE crosses TIER_ARM, the stop floor becomes TIER_FLOOR.
# A position closes when current ROE drops to the floor it has armed.
TRAILING_TIERS = [
    (15.0, 0.0),    # ROE >= +15% arms breakeven
    (30.0, 15.0),   # ROE >= +30% arms +15% lock-in
]

USE_TESTNET = False  # MAINNET — real money

# Demo flag: when True, after the first tick the bot reports equity as 99% of
# starting equity, forcing the kill switch to trip. Keep False in real runs.
DEMO_FAKE_LOSS = False

# How often the AI module refreshes the news-sentiment score and rewrites
# config.json. 15min: fast enough to ride sentiment shifts, well within free quotas.
AI_REFRESH_SECONDS = 900

# Models
GEMINI_MODEL = "gemini-2.5-flash"
# Higher-tier model used by strategy_reviewer.py — runs daily, low call count,
# worth the upgrade for parameter-tuning recommendations.
GEMINI_REVIEWER_MODEL = "gemini-2.5-pro"
MINIMAX_MODEL = "MiniMax-M2.7"
CLAUDE_MODEL = "claude-sonnet-4-6"

# Round 3 judge: how many times to call MiniMax and take the median score.
JUDGE_MULTI_SHOT = 3

# Trade gate (AI confirmation before placing orders) is now dynamic — see
# settings.DEFAULTS["TRADE_GATE_ENABLED"] / config.json. Toggle from dashboard.
