"""Main loop. Run with: python bot.py

Multi-symbol: iterates config.SYMBOLS each tick, runs the same RSI strategy
per coin with shared AI-tuned thresholds. Per-symbol size = BASE_TRADE_SIZE_USD
× TRADE_SIZE_MULTIPLIER (the latter is what AI scales).
"""
import os
import sys
import time
from typing import Any, Optional

from dotenv import load_dotenv

import ai_analyst
import config
import settings
import trade_gate
from exchange import HyperliquidClient
from logger import get_logger
from risk import KillSwitch
from strategy import _rsi, decide

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
    log.critical("Kill switch active: closing all open positions then halting.")
    for symbol in config.SYMBOLS:
        try:
            client.market_close(symbol)
        except Exception as e:
            log.error(f"Error while closing {symbol} on kill switch: {e}")
    log.critical("Bot halted by kill switch. Manual intervention required.")
    sys.exit(1)


def execute_signal(client: HyperliquidClient, symbol: str, signal: str,
                   trade_size_usd: float) -> None:
    """Bring net position in `symbol` to ±trade_size_usd."""
    pos = client.get_open_position(symbol)
    current_size = float(pos["szi"]) if pos else 0.0
    price = client.get_mid_price(symbol)

    target_usd = trade_size_usd if signal == "BUY" else -trade_size_usd
    target_size = target_usd / price
    delta_size = target_size - current_size

    if abs(delta_size * price) < 1.0:
        log.info(
            f"[{symbol}] already at target {signal} "
            f"(current={current_size:.4f}, target={target_size:.4f}); no order."
        )
        return

    is_buy = delta_size > 0
    order_size = abs(delta_size)
    notional = order_size * price

    if current_size != 0 and ((current_size > 0) != (target_size > 0)):
        log.info(
            f"[{symbol}] FLIP: {current_size:.4f} -> {target_size:.4f} "
            f"({'BUY' if is_buy else 'SELL'} {order_size:.4f}, ~${notional:.2f})"
        )
    else:
        log.info(
            f"[{symbol}] OPEN: target={target_size:.4f} "
            f"({'BUY' if is_buy else 'SELL'} {order_size:.4f}, ~${notional:.2f})"
        )

    try:
        client.market_open(symbol, is_buy, notional)
    except Exception as e:
        log.error(f"[{symbol}] order submission failed: {e}")


def _gather_symbol_state(client: HyperliquidClient, symbol: str) -> dict:
    """Per-symbol snapshot used both by AI cycle and trade gate."""
    state: dict[str, Any] = {"symbol": symbol}
    try:
        state["price"] = client.get_mid_price(symbol)
    except Exception as e:
        log.warning(f"[{symbol}] price fetch failed: {e}")
        state["price"] = None

    try:
        hour_ms = 60 * 60 * 1000
        end_ms = int(time.time() * 1000)
        candles = client.info.candles_snapshot(symbol, "1h", end_ms - 26 * hour_ms, end_ms)
        if state["price"] is not None and len(candles) >= 24:
            old_close = float(candles[-25]["c"]) if len(candles) >= 25 else float(candles[0]["c"])
            state["change_24h_pct"] = (state["price"] - old_close) / old_close * 100
        else:
            state["change_24h_pct"] = None
    except Exception as e:
        log.warning(f"[{symbol}] 24h change fetch failed: {e}")
        state["change_24h_pct"] = None

    try:
        closes = client.get_recent_closes(symbol, config.CANDLE_INTERVAL, config.CANDLE_LOOKBACK)
        if len(closes) >= config.RSI_PERIOD + 1:
            state["rsi"] = round(_rsi(closes, config.RSI_PERIOD), 2)
        else:
            state["rsi"] = None
    except Exception as e:
        log.warning(f"[{symbol}] RSI fetch failed: {e}")
        state["rsi"] = None

    try:
        pos = client.get_open_position(symbol)
        if pos is None:
            state["position"] = "FLAT"
        else:
            szi = float(pos.get("szi", 0))
            side = "LONG" if szi > 0 else "SHORT"
            state["position"] = f"{side} {abs(szi):.4f} {symbol}"
    except Exception as e:
        log.warning(f"[{symbol}] position fetch failed: {e}")
        state["position"] = "UNKNOWN"

    return state


def _gather_basket_ctx(client: HyperliquidClient, kill: KillSwitch) -> Optional[dict]:
    """Aggregate per-symbol state + account-level PnL into the multi-symbol ctx
    expected by ai_analyst's prompt builder."""
    try:
        per_symbol = [_gather_symbol_state(client, s) for s in config.SYMBOLS]
        ctx: dict[str, Any] = {"symbols": per_symbol}
        if kill.anchor_equity:
            try:
                equity = client.get_account_equity()
                ctx["session_pnl_pct"] = (equity - kill.anchor_equity) / kill.anchor_equity * 100
            except Exception as e:
                log.warning(f"Session PnL unavailable: {e}")
                ctx["session_pnl_pct"] = None
        return ctx
    except Exception as e:
        log.warning(f"Failed to build basket ctx: {e}")
        return None


def maybe_run_ai(last_ai_run: float, client: HyperliquidClient, kill: KillSwitch) -> float:
    now = time.time()
    if now - last_ai_run < config.AI_REFRESH_SECONDS:
        return last_ai_run
    log.info(f"Running AI analyst (refresh interval: {config.AI_REFRESH_SECONDS}s)")
    try:
        ctx = _gather_basket_ctx(client, kill)
        ai_analyst.run_once(market_ctx=ctx, client=client)
    except Exception as e:
        log.error(f"AI analyst failed: {e}", exc_info=True)
    return now


def _process_symbol(client: HyperliquidClient, kill: KillSwitch, symbol: str,
                    current_settings: dict[str, Any], ai_meta: dict[str, Any]) -> None:
    """Single-symbol slice of a tick: fetch closes -> decide -> gate -> execute."""
    try:
        closes = client.get_recent_closes(symbol, config.CANDLE_INTERVAL, config.CANDLE_LOOKBACK)
    except Exception as e:
        log.error(f"[{symbol}] candle fetch failed: {e}")
        return

    if len(closes) < config.RSI_PERIOD + 1:
        log.warning(f"[{symbol}] not enough candles ({len(closes)}); skipping.")
        return

    signal, info = decide(closes, current_settings)
    rsi = info["rsi"]

    if signal == "HOLD":
        log.info(f"[{symbol}] HOLD (RSI={rsi})")
        return

    log.info(f"[{symbol}] Signal: {signal} | RSI={rsi} thresholds={info['thresholds']}")

    multiplier = float(current_settings.get("TRADE_SIZE_MULTIPLIER", 1.0))
    base_size = float(config.BASE_TRADE_SIZE_USD.get(symbol, 0.0))
    trade_size = base_size * multiplier
    if trade_size < 1.0:
        log.warning(f"[{symbol}] trade size ${trade_size:.2f} below $1 — skipping.")
        return

    if config.TRADE_GATE_ENABLED:
        gate_ctx = _gather_symbol_state(client, symbol)
        funding_rates = (ai_meta or {}).get("funding_rates") or {}
        if kill.anchor_equity:
            try:
                eq = client.get_account_equity()
                gate_ctx["session_pnl_pct"] = (eq - kill.anchor_equity) / kill.anchor_equity * 100
            except Exception:
                gate_ctx["session_pnl_pct"] = None
        gate_ctx.update({
            "last_sentiment": ai_meta.get("last_sentiment"),
            "last_reason": ai_meta.get("last_reason"),
            "btc_dominance": ai_meta.get("btc_dominance"),
            "funding_rate": funding_rates.get(symbol),
        })
        allow, source, reason = trade_gate.judge_trade(signal, gate_ctx)
        if not allow:
            log.info(f"[{symbol}] Trade gate SKIP via {source}: {reason}")
            return

    execute_signal(client, symbol, signal, trade_size)


def tick(client: HyperliquidClient, kill: KillSwitch, current_settings: dict[str, Any]) -> None:
    real_equity = client.get_account_equity()
    kill.set_anchor(real_equity, current_settings)
    equity = kill.observe(real_equity)
    if kill.check(equity, current_settings):
        handle_kill_switch(client)
        return

    ai_meta = current_settings.get("ai_meta") or {}
    for symbol in config.SYMBOLS:
        _process_symbol(client, kill, symbol, current_settings, ai_meta)


def main() -> None:
    pk, addr = load_env()
    client = HyperliquidClient(pk, addr, use_testnet=config.USE_TESTNET)
    kill = KillSwitch()

    boot = settings.load()
    sizes = ", ".join(
        f"{s}=${config.BASE_TRADE_SIZE_USD[s] * boot['TRADE_SIZE_MULTIPLIER']:.0f}"
        for s in config.SYMBOLS
    )
    log.info(
        f"Bot starting | symbols={config.SYMBOLS} loop={config.LOOP_SECONDS}s "
        f"sizes=({sizes}) mult={boot['TRADE_SIZE_MULTIPLIER']} "
        f"loss_limit={boot['DAILY_LOSS_LIMIT'] * 100:.2f}% "
        f"rsi=({config.RSI_OVERSOLD}/{config.RSI_OVERBOUGHT}, locked) "
        f"ai_refresh={config.AI_REFRESH_SECONDS}s demo_fake_loss={config.DEMO_FAKE_LOSS}"
    )

    last_ai_run = 0.0  # force AI to run on the first tick

    while True:
        start = time.time()
        try:
            last_ai_run = maybe_run_ai(last_ai_run, client, kill)
            current_settings = settings.load()
            ai_meta = current_settings.get("ai_meta") or {}
            if ai_meta.get("last_sentiment") is not None:
                log.info(
                    f"Active: mult={current_settings['TRADE_SIZE_MULTIPLIER']} "
                    f"loss={current_settings['DAILY_LOSS_LIMIT'] * 100:.2f}% "
                    f"sentiment={ai_meta['last_sentiment']}/10"
                )
            tick(client, kill, current_settings)
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
