"""Central configuration. Tweak constants here, not in other modules."""

SYMBOL = "SOL"
TRADE_SIZE_USD = 12.0
MAX_OPEN_POSITIONS = 1

# Kill switch: halt if equity drops by this fraction from the bot's STARTING equity
# (anchored once at startup, not reset daily).
DAILY_LOSS_LIMIT = 0.02  # 2%

LOOP_SECONDS = 60
CANDLE_INTERVAL = "1m"
CANDLE_LOOKBACK = 100

RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0

USE_TESTNET = False  # MAINNET — real money

# Demo flag: when True, after the first tick the bot reports equity as 99% of
# starting equity, forcing the kill switch to trip and proving the
# close-then-halt mechanics work end-to-end. Always set False in real runs.
DEMO_FAKE_LOSS = False
