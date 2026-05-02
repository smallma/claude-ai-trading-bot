"""Main loop. Run with: python bot.py"""
import os
import sys
import time

from dotenv import load_dotenv

import config
from exchange import HyperliquidClient
from logger import get_logger
from risk import KillSwitch
from strategy import decide

log = get_logger("bot")


def load_env() -> tuple[str, str]:
    load_dotenv()
    pk = os.getenv("HYPERLIQUID_PRIVATE_KEY")
    addr = os.getenv("HYPERLIQUID_ADDRESS")
    if not pk or not addr:
        log.critical("Missing HYPERLIQUID_PRIVATE_KEY or HYPERLIQUID_ADDRESS in .env")
        sys.exit(1)
    return pk, addr


def handle_kill_switch(client: HyperliquidClient) -> None:
    """Close any open position and halt the script."""
    log.critical("Kill switch active: closing open positions then halting.")
    try:
        client.market_close(config.SYMBOL)
    except Exception as e:
        log.error(f"Error while closing position on kill switch: {e}")
    log.critical("Bot halted by kill switch. Manual intervention required.")
    sys.exit(1)


def execute_signal(client: HyperliquidClient, signal: str) -> None:
    """Translate a BUY/SELL signal into an order, accounting for any existing position.

    Net target position:
        BUY  -> +TRADE_SIZE_USD worth of SYMBOL (long)
        SELL -> -TRADE_SIZE_USD worth of SYMBOL (short)

    The order delta is (target - current), so a flip from long $100 to short $100
    submits a single order sized for ~$200 worth in the opposite direction.
    """
    pos = client.get_open_position(config.SYMBOL)
    current_size = float(pos["szi"]) if pos else 0.0  # signed; >0 long, <0 short
    price = client.get_mid_price(config.SYMBOL)

    target_usd = config.TRADE_SIZE_USD if signal == "BUY" else -config.TRADE_SIZE_USD
    target_size = target_usd / price
    delta_size = target_size - current_size

    # Skip if delta is dust (< $1 of notional) — already at target.
    if abs(delta_size * price) < 1.0:
        log.info(
            f"Already at target {signal} position "
            f"(current={current_size:.4f}, target={target_size:.4f}); no order needed."
        )
        return

    is_buy = delta_size > 0
    order_size = abs(delta_size)
    notional = order_size * price

    if current_size != 0 and ((current_size > 0) != (target_size > 0)):
        log.info(
            f"FLIP: current={current_size:.4f} -> target={target_size:.4f} "
            f"(submitting {'BUY' if is_buy else 'SELL'} {order_size:.4f} {config.SYMBOL}, "
            f"~${notional:.2f})"
        )
    else:
        log.info(
            f"OPEN: target={target_size:.4f} {config.SYMBOL} "
            f"({'BUY' if is_buy else 'SELL'} {order_size:.4f}, ~${notional:.2f})"
        )

    try:
        client.market_open(config.SYMBOL, is_buy, notional)
    except Exception as e:
        log.error(f"Order submission failed: {e}")


def tick(client: HyperliquidClient, kill: KillSwitch) -> None:
    real_equity = client.get_account_equity()
    kill.set_anchor(real_equity)
    equity = kill.observe(real_equity)
    if kill.check(equity):
        handle_kill_switch(client)
        return

    closes = client.get_recent_closes(
        config.SYMBOL, config.CANDLE_INTERVAL, config.CANDLE_LOOKBACK
    )
    if len(closes) < config.RSI_PERIOD + 1:
        log.warning(f"Not enough candles ({len(closes)}); skipping decision.")
        return

    signal, info = decide(closes)
    rsi = info["rsi"]

    if signal == "HOLD":
        log.info(f"RSI at {rsi}, holding position...")
        return

    log.info(f"Signal: {signal} | RSI={rsi} last_close={info['last_close']}")
    execute_signal(client, signal)


def main() -> None:
    pk, addr = load_env()
    client = HyperliquidClient(pk, addr, use_testnet=config.USE_TESTNET)
    kill = KillSwitch()

    log.info(
        f"Bot starting | symbol={config.SYMBOL} size=${config.TRADE_SIZE_USD} "
        f"loop={config.LOOP_SECONDS}s loss_limit={config.DAILY_LOSS_LIMIT * 100:.2f}% "
        f"demo_fake_loss={config.DEMO_FAKE_LOSS}"
    )

    while True:
        start = time.time()
        try:
            tick(client, kill)
        except SystemExit:
            raise
        except Exception as e:
            log.error(f"Unhandled error in tick: {e}", exc_info=True)

        elapsed = time.time() - start
        sleep_for = max(0.0, config.LOOP_SECONDS - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Interrupted by user. Shutting down.")
