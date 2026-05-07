"""Main loop. Run with: python bot.py

Multi-symbol: iterates config.SYMBOLS each tick, runs a composite strategy
(EMA trend + RSI/Bollinger) per coin. Per-symbol size = BASE_TRADE_SIZE_USD
× TRADE_SIZE_MULTIPLIER (the multiplier is what AI scales).

Trailing stop: monitors per-symbol unrealised ROE every tick. When max-seen
ROE crosses a tier in config.TRAILING_TIERS, the floor is armed. If current
ROE drops to the armed floor from above, the position is market-closed.
"""
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv

import ai_analyst
import config
import journal
import settings
import trade_gate
from exchange import HyperliquidClient
from logger import get_logger
from risk import KillSwitch
from strategy import _rsi, decide

log = get_logger("bot")

# How often to append a datapoint to journal/equity-YYYYMM.jsonl. The kill
# switch already pulls equity every tick (60s) so this is just a sampled write,
# zero new API load. ~12 lines/hour, ~8 MB/year.
EQUITY_LOG_INTERVAL_SECONDS = 300

# Wall-clock of the last equity-log write. 0.0 forces a write on the first tick
# so the chart has a starting point immediately after a restart.
_last_equity_log_ts: float = 0.0

# Per-symbol max-seen ROE since the current position opened. Reset to None when
# the position goes flat. In-memory only — bot restart resets the high-water
# mark, which means a position already past +30% would re-arm from current ROE.
_position_max_roe: dict[str, float] = {}

# Per-symbol entry meta for matching ENTRY <-> EXIT in the journal. Reset on
# bot restart, so any pre-existing position will EXIT-journal with trade_id=None
# (the matching ENTRY simply lives in a previous run's journal file).
# Shape: {"trade_id": str, "entry_price": float, "entry_ts": iso8601,
#         "side": "BUY"|"SELL", "size_usd": float, "size_units": float}
_position_entry_meta: dict[str, dict[str, Any]] = {}


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
            pos = client.get_open_position(symbol)
            if pos is not None:
                roe = _compute_roe_pct(pos) or 0.0
                max_roe = _position_max_roe.get(symbol, roe)
                _journal_exit_before_close(client, symbol, pos, "kill_switch", max_roe, roe)
            client.market_close(symbol)
        except Exception as e:
            log.error(f"Error while closing {symbol} on kill switch: {e}")
    log.critical("Bot halted by kill switch. Manual intervention required.")
    sys.exit(1)


def _journal_exit_before_close(
    client: HyperliquidClient,
    symbol: str,
    pos: dict,
    exit_reason: str,
    max_roe_pct: Optional[float],
    final_roe_pct: Optional[float],
    exit_price: Optional[float] = None,
) -> None:
    """Snapshot position state and write an EXIT record. MUST run before the
    market_close call, since pos["unrealizedPnl"] disappears once we close.

    Pops `_position_entry_meta[symbol]` so subsequent re-entries get a fresh trade_id.
    """
    meta = _position_entry_meta.get(symbol, {})
    if exit_price is None:
        try:
            exit_price = client.get_mid_price(symbol)
        except Exception:
            exit_price = None

    try:
        pnl_usd: Optional[float] = float(pos.get("unrealizedPnl", 0))
    except (TypeError, ValueError):
        pnl_usd = None

    try:
        szi = float(pos.get("szi", 0))
    except (TypeError, ValueError):
        szi = 0.0
    side = "LONG" if szi > 0 else "SHORT"
    size_units = abs(szi)
    size_usd = size_units * exit_price if exit_price else None

    hold_seconds: Optional[int] = None
    entry_ts = meta.get("entry_ts")
    if entry_ts:
        try:
            entry_dt = datetime.fromisoformat(entry_ts)
            hold_seconds = int((datetime.now(timezone.utc) - entry_dt).total_seconds())
        except ValueError:
            pass

    exit_ctx = {
        "exit_reason": exit_reason,
        "entry_price": meta.get("entry_price"),
        "entry_ts": entry_ts,
        "hold_seconds": hold_seconds,
        "max_roe_pct": round(max_roe_pct, 2) if max_roe_pct is not None else None,
        "final_roe_pct": round(final_roe_pct, 2) if final_roe_pct is not None else None,
        "pnl_usd": round(pnl_usd, 4) if pnl_usd is not None else None,
    }
    journal.log_exit(
        symbol=symbol,
        side=side,
        fill_price=exit_price,
        size_usd=size_usd,
        size_units=size_units,
        trade_id=meta.get("trade_id"),
        exit_context=exit_ctx,
    )
    _position_entry_meta.pop(symbol, None)


def execute_signal(client: HyperliquidClient, symbol: str, signal: str,
                   trade_size_usd: float, decision_context: dict) -> None:
    """Bring net position in `symbol` to ±trade_size_usd.

    On FLIP we journal the existing position as an EXIT (reason
    "opposite_signal") before placing the flip order. On any successful order
    we journal a new ENTRY and stash entry meta in `_position_entry_meta` so
    the matching EXIT can carry trade_id, entry_price and hold time later.
    """
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

    is_flip = current_size != 0 and ((current_size > 0) != (target_size > 0))
    if is_flip:
        log.info(
            f"[{symbol}] FLIP: {current_size:.4f} -> {target_size:.4f} "
            f"({'BUY' if is_buy else 'SELL'} {order_size:.4f}, ~${notional:.2f})"
        )
        # Journal the existing position as an EXIT before the flip lands.
        try:
            roe = _compute_roe_pct(pos) if pos else None
            max_roe = _position_max_roe.get(symbol, roe)
            _journal_exit_before_close(
                client, symbol, pos or {}, "opposite_signal",
                max_roe, roe, exit_price=price,
            )
        except Exception as e:
            log.warning(f"[{symbol}] flip EXIT journal failed: {e}")
        _position_max_roe.pop(symbol, None)
    else:
        log.info(
            f"[{symbol}] OPEN: target={target_size:.4f} "
            f"({'BUY' if is_buy else 'SELL'} {order_size:.4f}, ~${notional:.2f})"
        )

    try:
        client.market_open(symbol, is_buy, notional)
    except Exception as e:
        log.error(f"[{symbol}] order submission failed: {e}")
        return

    # Order accepted — record ENTRY in journal and capture meta for EXIT matching.
    trade_id = journal.new_trade_id()
    entry_side = "BUY" if is_buy else "SELL"
    entry_size_units = abs(target_size)
    entry_size_usd = entry_size_units * price
    entry_ts = datetime.now(timezone.utc).isoformat()
    try:
        journal.log_entry(
            symbol=symbol,
            side=entry_side,
            fill_price=price,
            size_usd=entry_size_usd,
            size_units=entry_size_units,
            trade_id=trade_id,
            decision_context=decision_context,
        )
    except Exception as e:
        log.warning(f"[{symbol}] ENTRY journal failed: {e}")

    _position_entry_meta[symbol] = {
        "trade_id": trade_id,
        "entry_price": price,
        "entry_ts": entry_ts,
        "side": entry_side,
        "size_usd": entry_size_usd,
        "size_units": entry_size_units,
    }
    # Fresh position -> reset the trailing-stop high-water mark.
    _position_max_roe.pop(symbol, None)


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


def _compute_roe_pct(pos: dict) -> Optional[float]:
    """ROE% = unrealizedPnl / marginUsed × 100, using Hyperliquid's own fields."""
    try:
        pnl = float(pos.get("unrealizedPnl", 0))
        margin = float(pos.get("marginUsed", 0))
        if margin > 0:
            return pnl / margin * 100.0
    except (TypeError, ValueError):
        pass
    return None


def _build_decision_context(
    symbol: str,
    signal: str,
    info: dict[str, Any],
    gate_ctx: dict[str, Any],
    gate_votes: dict[str, dict],
    gate_enabled: bool,
    current_settings: dict[str, Any],
    ai_meta: dict[str, Any],
) -> dict[str, Any]:
    """Snapshot of every input that justified this entry. Stored in the journal
    so strategy_reviewer.py can later attribute outcomes to specific readings.
    """
    return {
        "signal": signal,
        "trigger": info.get("trigger"),
        "tech": {
            "rsi": info.get("rsi"),
            "ema_fast": info.get("ema_fast"),
            "ema_slow": info.get("ema_slow"),
            "ema_trend": info.get("ema_trend"),
            "bb_upper": info.get("bb_upper"),
            "bb_lower": info.get("bb_lower"),
            "bb_position": info.get("bb_position"),
        },
        "ai_gate": {
            "enabled": gate_enabled,
            "votes": gate_votes,
        },
        "sentiment": {
            "score": ai_meta.get("last_sentiment"),
            "confidence": ai_meta.get("last_confidence"),
            "reason": ai_meta.get("last_reason"),
            "fng": ai_meta.get("last_fng"),
        },
        "btc_dominance": ai_meta.get("btc_dominance"),
        "funding_rate": gate_ctx.get("funding_rate"),
        "change_24h_pct": gate_ctx.get("change_24h_pct"),
        "session_pnl_pct": gate_ctx.get("session_pnl_pct"),
        "config_snapshot": {
            "TRADE_SIZE_MULTIPLIER": current_settings.get("TRADE_SIZE_MULTIPLIER"),
            "DAILY_LOSS_LIMIT": current_settings.get("DAILY_LOSS_LIMIT"),
            # Effective strategy params for THIS trade — pulled from strategy
            # output rather than raw config.py so reviewer overrides are
            # captured verbatim for later attribution.
            **(info.get("params_used") or {}),
            "TRAILING_TIERS": config.TRAILING_TIERS,
            "BASE_TRADE_SIZE_USD": config.BASE_TRADE_SIZE_USD.get(symbol),
        },
    }


def _check_trailing_stop(symbol: str, roe: float) -> Optional[tuple[float, float]]:
    """Update max-seen ROE and check whether trailing-stop should fire.

    Returns (max_roe_seen, armed_floor) if the stop has tripped, else None.
    """
    prev_max = _position_max_roe.get(symbol)
    new_max = roe if prev_max is None else max(prev_max, roe)
    _position_max_roe[symbol] = new_max

    armed_floor: Optional[float] = None
    for tier_arm, tier_floor in config.TRAILING_TIERS:
        if new_max >= tier_arm:
            armed_floor = tier_floor

    if armed_floor is None:
        return None
    if roe <= armed_floor:
        return new_max, armed_floor
    return None


def _process_symbol(client: HyperliquidClient, kill: KillSwitch, symbol: str,
                    current_settings: dict[str, Any], ai_meta: dict[str, Any]) -> None:
    """One symbol per tick: trailing-stop check -> compute signal -> gate -> execute."""
    # 1. Trailing stop check — runs every tick regardless of signal.
    try:
        pos = client.get_open_position(symbol)
    except Exception as e:
        log.error(f"[{symbol}] position fetch failed: {e}")
        pos = None

    if pos is None:
        _position_max_roe.pop(symbol, None)
        # Don't drop entry meta here — flat-on-restart is fine, but if the
        # position was closed externally (manual UI close) we lose the EXIT
        # journal record. That's acceptable: meta will be overwritten on next
        # ENTRY anyway.
    else:
        roe = _compute_roe_pct(pos)
        if roe is not None:
            triggered = _check_trailing_stop(symbol, roe)
            if triggered:
                max_seen, floor = triggered
                log.warning(
                    f"[{symbol}] [Trailing Stop Triggered] max ROE {max_seen:+.2f}% -> "
                    f"floor {floor:+.2f}%, current {roe:+.2f}%; closing position."
                )
                _journal_exit_before_close(client, symbol, pos, "trailing_stop", max_seen, roe)
                try:
                    client.market_close(symbol)
                except Exception as e:
                    log.error(f"[{symbol}] trailing-stop close failed: {e}")
                _position_max_roe.pop(symbol, None)
                return
            else:
                log.info(f"[{symbol}] ROE {roe:+.2f}% (max {_position_max_roe.get(symbol, roe):+.2f}%)")

    # 2. Fetch candles + compute signal.
    try:
        closes = client.get_recent_closes(symbol, config.CANDLE_INTERVAL, config.CANDLE_LOOKBACK)
    except Exception as e:
        log.error(f"[{symbol}] candle fetch failed: {e}")
        return

    if len(closes) < max(config.RSI_PERIOD + 1, config.EMA_SLOW_PERIOD * 3, config.BB_PERIOD):
        log.warning(f"[{symbol}] not enough candles ({len(closes)}); skipping.")
        return

    signal, info = decide(closes, current_settings)

    if signal == "HOLD":
        log.info(
            f"[{symbol}] HOLD | RSI={info['rsi']} EMA{config.EMA_FAST_PERIOD}/{config.EMA_SLOW_PERIOD}={info['ema_trend']} "
            f"BB={info['bb_position']}"
        )
        return

    log.info(
        f"[{symbol}] Signal: {signal} via {info.get('trigger', '?')} | "
        f"RSI={info['rsi']} EMA={info['ema_trend']} BB={info['bb_position']}"
    )

    multiplier = float(current_settings.get("TRADE_SIZE_MULTIPLIER", 1.0))
    base_size = float(config.BASE_TRADE_SIZE_USD.get(symbol, 0.0))
    trade_size = base_size * multiplier
    if trade_size < 1.0:
        log.warning(f"[{symbol}] trade size ${trade_size:.2f} below $1 — skipping.")
        return

    funding_rates = (ai_meta or {}).get("funding_rates") or {}
    gate_ctx = _gather_symbol_state(client, symbol)
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
        "ema_trend": info["ema_trend"],
        "ema_fast": info["ema_fast"],
        "ema_slow": info["ema_slow"],
        "bb_upper": info["bb_upper"],
        "bb_lower": info["bb_lower"],
        "bb_position": info["bb_position"],
        "signal_trigger": info.get("trigger"),
    })

    gate_votes: dict[str, dict] = {}
    gate_enabled = bool(current_settings.get("TRADE_GATE_ENABLED", True))
    if gate_enabled:
        allow, source, reason, gate_votes = trade_gate.judge_trade(signal, gate_ctx)
        if not allow:
            log.info(f"[{symbol}] Trade gate SKIP via {source}: {reason}")
            return

    decision_context = _build_decision_context(
        symbol=symbol,
        signal=signal,
        info=info,
        gate_ctx=gate_ctx,
        gate_votes=gate_votes,
        gate_enabled=gate_enabled,
        current_settings=current_settings,
        ai_meta=ai_meta,
    )
    execute_signal(client, symbol, signal, trade_size, decision_context)


def _maybe_log_equity(equity: float, kill: KillSwitch) -> None:
    """Sample equity to journal/equity-*.jsonl every EQUITY_LOG_INTERVAL_SECONDS.
    Used by the dashboard's equity curve chart."""
    global _last_equity_log_ts
    now = time.time()
    if now - _last_equity_log_ts < EQUITY_LOG_INTERVAL_SECONDS:
        return
    _last_equity_log_ts = now
    session_pnl_pct: Optional[float] = None
    if kill.anchor_equity:
        try:
            session_pnl_pct = (equity - kill.anchor_equity) / kill.anchor_equity * 100.0
        except ZeroDivisionError:
            pass
    journal.log_equity(equity, kill.anchor_equity, session_pnl_pct)


def tick(client: HyperliquidClient, kill: KillSwitch, current_settings: dict[str, Any]) -> None:
    real_equity = client.get_account_equity()
    kill.set_anchor(real_equity, current_settings)
    equity = kill.observe(real_equity)
    _maybe_log_equity(equity, kill)
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

    purged = journal.purge_old()
    if purged:
        log.info(f"Journal purge: removed {purged} file(s) older than {journal.RETENTION_MONTHS} months")

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
