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
_position_min_roe: dict[str, float] = {}

# Persistent leverage cache lives in config.json under
# symbol_configs[sym].applied_leverage so it survives bot restarts and we
# never call Hyperliquid's update_leverage when the desired value matches
# what's already there. The dashboard preserves this field across edits.

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


def handle_kill_switch(client: HyperliquidClient,
                       symbols: Optional[list[str]] = None) -> None:
    log.critical("Kill switch active: closing all open positions then halting.")
    for symbol in (symbols or config.SYMBOLS):
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
    min_roe_pct: Optional[float],
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
        "trade_max_drawdown_pct": abs(round(min_roe_pct, 2)) if min_roe_pct is not None and min_roe_pct < 0 else 0.0,
        "pnl_usd": round(pnl_usd, 4) if pnl_usd is not None else None,
        "entry_ai_score": meta.get("entry_ai_score"),
        "entry_fng_value": meta.get("entry_fng_value"),
        "entry_rsi": meta.get("entry_rsi"),
        "entry_ema_spread_pct": meta.get("entry_ema_spread_pct"),
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
    """Place an order for `symbol` in the direction of `signal`.

    Supports three scenarios:
    1. FLAT → open a new position (BUY or SELL).
    2. Same direction → add to existing position (left-side averaging),
       guarded by MAX_POSITION_MULTIPLIER to prevent unlimited stacking.
    3. Opposite direction (FLIP) → journal EXIT on old position, then open new.

    On any successful order we journal a new ENTRY and stash entry meta in
    `_position_entry_meta` so the matching EXIT can carry trade_id later.
    """
    pos = client.get_open_position(symbol)
    current_size = float(pos["szi"]) if pos else 0.0
    price = client.get_mid_price(symbol)

    is_same_direction = (
        (signal == "BUY" and current_size > 0) or
        (signal == "SELL" and current_size < 0)
    )

    # --- Same-direction add: check MAX_POSITION_MULTIPLIER guard ---
    if is_same_direction:
        max_notional = trade_size_usd * config.MAX_POSITION_MULTIPLIER
        current_notional = abs(current_size) * price
        if current_notional >= max_notional:
            log.warning(
                f"[{symbol}] ADD blocked: current ${current_notional:.2f} "
                f">= max ${max_notional:.2f} "
                f"(base×mult×{config.MAX_POSITION_MULTIPLIER}); skipping."
            )
            return
        # Order only 1× trade_size_usd increment (not the full target)
        order_notional = min(trade_size_usd, max_notional - current_notional)
        order_size = order_notional / price
        is_buy = signal == "BUY"
        log.info(
            f"[{symbol}] ADD to {('LONG' if current_size > 0 else 'SHORT')}: "
            f"+{'BUY' if is_buy else 'SELL'} {order_size:.4f} (~${order_notional:.2f}), "
            f"current ${current_notional:.2f} / max ${max_notional:.2f}"
        )
    else:
        # New position or flip
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
        order_notional = order_size * price

        is_flip = current_size != 0 and ((current_size > 0) != (target_size > 0))
        if is_flip:
            log.info(
                f"[{symbol}] FLIP: {current_size:.4f} -> {target_size:.4f} "
                f"({'BUY' if is_buy else 'SELL'} {order_size:.4f}, ~${order_notional:.2f})"
            )
            try:
                roe = _compute_roe_pct(pos) if pos else None
                max_roe = _position_max_roe.get(symbol, roe)
                min_roe = _position_min_roe.get(symbol, roe)
                _journal_exit_before_close(
                    client, symbol, pos or {}, "opposite_signal",
                    max_roe, min_roe, roe, exit_price=price,
                )
            except Exception as e:
                log.warning(f"[{symbol}] flip EXIT journal failed: {e}")
            _position_max_roe.pop(symbol, None)
            _position_min_roe.pop(symbol, None)
        else:
            log.info(
                f"[{symbol}] OPEN: target={target_size:.4f} "
                f"({'BUY' if is_buy else 'SELL'} {order_size:.4f}, ~${order_notional:.2f})"
            )

    notional = order_size * price

    # client.market_open raises on:
    #   - HTTP / SDK transport errors
    #   - Hyperliquid inner rejections (szDecimals, min notional, leverage caps)
    # so reaching the post-call code below means the exchange ACCEPTED the order
    # and we have a real oid. Journal stays consistent with what actually
    # executed — no more "ghost trades".
    try:
        result = client.market_open(symbol, is_buy, notional)
    except Exception as e:
        log.error(f"[{symbol}] order submission failed, NOT journalling: {e}")
        return

    # Prefer exchange-reported fill data for the journal; fall back to our own
    # rounded values when the response shape is unfamiliar.
    rounded_sz = result.get("_rounded_sz") if isinstance(result, dict) else None
    filled = result.get("_filled_status") if isinstance(result, dict) else None
    actual_size_units: float = float(rounded_sz) if rounded_sz is not None else abs(target_size)
    actual_fill_price: float = price
    actual_oid: Optional[int] = None
    if isinstance(filled, dict):
        actual_oid = filled.get("oid")
        try:
            ts = filled.get("totalSz")
            if ts is not None:
                actual_size_units = float(ts)
            ap = filled.get("avgPx")
            if ap is not None:
                actual_fill_price = float(ap)
        except (TypeError, ValueError):
            pass

    if actual_size_units <= 0:
        log.error(f"[{symbol}] post-fill size is {actual_size_units}, NOT journalling")
        return

    # Order accepted — record ENTRY in journal and capture meta for EXIT matching.
    trade_id = journal.new_trade_id()
    entry_side = "BUY" if is_buy else "SELL"
    entry_size_usd = actual_size_units * actual_fill_price
    entry_ts = datetime.now(timezone.utc).isoformat()
    try:
        journal.log_entry(
            symbol=symbol,
            side=entry_side,
            fill_price=actual_fill_price,
            size_usd=entry_size_usd,
            size_units=actual_size_units,
            trade_id=trade_id,
            decision_context={**decision_context, "exchange_oid": actual_oid},
        )
    except Exception as e:
        log.warning(f"[{symbol}] ENTRY journal failed: {e}")

    ai_score = decision_context.get("ai_meta", {}).get("last_sentiment")
    fng_value = None
    fng = decision_context.get("ai_meta", {}).get("last_fng")
    if isinstance(fng, dict):
        fng_value = fng.get("value")

    _position_entry_meta[symbol] = {
        "trade_id": trade_id,
        "entry_price": actual_fill_price,
        "entry_ts": entry_ts,
        "side": entry_side,
        "size_usd": entry_size_usd,
        "size_units": actual_size_units,
        "entry_ai_score": ai_score,
        "entry_fng_value": fng_value,
        "entry_rsi": decision_context.get("tech", {}).get("rsi"),
        "entry_ema_spread_pct": decision_context.get("tech", {}).get("ema_spread_pct"),
    }
    # Fresh position -> reset the trailing-stop high-water mark.
    _position_max_roe.pop(symbol, None)
    _position_min_roe.pop(symbol, None)


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


def _gather_basket_ctx(client: HyperliquidClient, kill: KillSwitch,
                        symbols: list[str]) -> Optional[dict]:
    """Aggregate per-symbol state + account-level PnL into the multi-symbol ctx
    expected by ai_analyst's prompt builder."""
    try:
        per_symbol = [_gather_symbol_state(client, s) for s in symbols]
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


def maybe_run_ai(last_ai_run: float, client: HyperliquidClient, kill: KillSwitch,
                 symbols: list[str]) -> float:
    now = time.time()
    if now - last_ai_run < config.AI_REFRESH_SECONDS:
        return last_ai_run
    log.info(f"Running AI analyst on {symbols} (refresh interval: {config.AI_REFRESH_SECONDS}s)")
    try:
        ctx = _gather_basket_ctx(client, kill, symbols)
        ai_analyst.run_once(market_ctx=ctx, client=client, symbols=symbols)
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
            # Per-symbol effective sizing & leverage at the moment of entry,
            # so the journal reflects whatever was active on the dashboard.
            "BASE_TRADE_SIZE_USD": _resolve_symbol_config(current_settings, symbol)[0],
            "LEVERAGE": _resolve_symbol_config(current_settings, symbol)[1],
        },
    }


def _check_trailing_stop(symbol: str, roe: float) -> Optional[tuple[float, float]]:
    """Update max-seen ROE and check whether trailing-stop should fire.

    Returns (max_roe_seen, armed_floor) if the stop has tripped, else None.
    """
    prev_max = _position_max_roe.get(symbol)
    new_max = roe if prev_max is None else max(prev_max, roe)
    _position_max_roe[symbol] = new_max

    prev_min = _position_min_roe.get(symbol)
    new_min = roe if prev_min is None else min(prev_min, roe)
    _position_min_roe[symbol] = new_min

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
    """One symbol per tick: take-profit -> trailing-stop -> compute signal -> gate -> execute."""
    # 1. Position checks — runs every tick regardless of signal.
    try:
        pos = client.get_open_position(symbol)
    except Exception as e:
        log.error(f"[{symbol}] position fetch failed: {e}")
        pos = None

    if pos is None:
        _position_max_roe.pop(symbol, None)
        _position_min_roe.pop(symbol, None)
        # Don't drop entry meta here — flat-on-restart is fine, but if the
        # position was closed externally (manual UI close) we lose the EXIT
        # journal record. That's acceptable: meta will be overwritten on next
        # ENTRY anyway.
    else:
        roe = _compute_roe_pct(pos)
        if roe is not None:
            # 1a. Auto take-profit — fires BEFORE trailing stop.
            tp_pct = float(current_settings.get("AUTO_TAKE_PROFIT_PCT", 10.0))
            if roe >= tp_pct:
                max_seen = _position_max_roe.get(symbol, roe)
                min_seen = _position_min_roe.get(symbol, roe)
                log.warning(
                    f"[{symbol}] [Take Profit] ROE {roe:+.2f}% >= "
                    f"{tp_pct:+.2f}% threshold; closing position."
                )
                _journal_exit_before_close(
                    client, symbol, pos, "take_profit",
                    max_seen, min_seen, roe,
                )
                try:
                    client.market_close(symbol)
                except Exception as e:
                    log.error(f"[{symbol}] take-profit close failed: {e}")
                _position_max_roe.pop(symbol, None)
                _position_min_roe.pop(symbol, None)
                return

            # 1b. Trailing stop check.
            triggered = _check_trailing_stop(symbol, roe)
            if triggered:
                max_seen, floor = triggered
                log.warning(
                    f"[{symbol}] [Trailing Stop Triggered] max ROE {max_seen:+.2f}% -> "
                    f"floor {floor:+.2f}%, current {roe:+.2f}%; closing position."
                )
                min_seen = _position_min_roe.get(symbol, roe)
                _journal_exit_before_close(client, symbol, pos, "trailing_stop", max_seen, min_seen, roe)
                try:
                    client.market_close(symbol)
                except Exception as e:
                    log.error(f"[{symbol}] trailing-stop close failed: {e}")
                _position_max_roe.pop(symbol, None)
                _position_min_roe.pop(symbol, None)
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

    signal, info = decide(closes, current_settings,
                         fng_value=((ai_meta or {}).get("last_fng") or {}).get("value"))

    _ai_score = (ai_meta or {}).get("last_sentiment")

    if signal == "HOLD":
        log.info(
            f"[{symbol}] HOLD | RSI={info['rsi']} EMA{config.EMA_FAST_PERIOD}/{config.EMA_SLOW_PERIOD}={info['ema_trend']} "
            f"BB={info['bb_position']}"
        )
        journal.log_judgment(symbol, "HOLD", info, ai_score=_ai_score)
        return

    log.info(
        f"[{symbol}] Signal: {signal} via {info.get('trigger', '?')} | "
        f"RSI={info['rsi']} EMA={info['ema_trend']} BB={info['bb_position']}"
    )

    multiplier = float(current_settings.get("TRADE_SIZE_MULTIPLIER", 1.0))
    base_size, _leverage = _resolve_symbol_config(current_settings, symbol)
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
            journal.log_judgment(symbol, "SKIP", info, ai_score=_ai_score,
                                 gate_result="REJECT", gate_reason=reason)
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
    journal.log_judgment(symbol, signal, info, ai_score=_ai_score,
                         gate_result="GO" if gate_enabled else None)
    execute_signal(client, symbol, signal, trade_size, decision_context)


def _active_symbols(current_settings: dict[str, Any]) -> list[str]:
    """The list the bot iterates THIS tick — dashboard-editable, falls back to
    config.SYMBOLS so an empty/missing field never silently disables trading."""
    syms = current_settings.get("symbols")
    if isinstance(syms, list) and syms:
        return [str(s).upper() for s in syms]
    return list(config.SYMBOLS)


def _ensure_symbol_configs(current_settings: dict[str, Any],
                            symbols: list[str]) -> bool:
    """Make sure every symbol the bot is about to trade has a symbol_configs
    entry. Brand-new symbols (e.g. user just typed "BTC" on the dashboard) get
    seeded with config.NEW_SYMBOL_DEFAULT_* values so we never crash on a
    KeyError mid-tick.

    Mutates `current_settings["symbol_configs"]` in place. Returns True if a
    seed happened so the caller can persist the update.
    """
    sc = current_settings.setdefault("symbol_configs", {})
    changed = False
    for sym in symbols:
        if sym in sc:
            continue
        sc[sym] = {
            "base_usd": float(config.NEW_SYMBOL_DEFAULT_BASE_USD),
            "leverage": int(config.NEW_SYMBOL_DEFAULT_LEVERAGE),
        }
        changed = True
        log.info(
            f"Auto-seeded symbol_configs[{sym}] = "
            f"base_usd=${config.NEW_SYMBOL_DEFAULT_BASE_USD} "
            f"leverage={config.NEW_SYMBOL_DEFAULT_LEVERAGE}x"
        )
    return changed


def _process_force_close_queue(client: HyperliquidClient,
                                current_settings: dict[str, Any]) -> bool:
    """Drain config.json -> force_close_queue at the top of each tick.

    For every queued symbol with a live position: journal an EXIT with
    reason "manual_close" THEN issue market_close (so the journal record can't
    miss the position state). Then write back an empty queue so the action
    isn't repeated. Returns True if any work happened.
    """
    queue = current_settings.get("force_close_queue") or []
    if not isinstance(queue, list) or not queue:
        return False

    log.warning(f"Manual close requested via dashboard: {queue}")
    for symbol in list(queue):
        symbol = str(symbol).upper()
        try:
            pos = client.get_open_position(symbol)
        except Exception as e:
            log.error(f"[{symbol}] manual close: position fetch failed: {e}")
            pos = None

        if pos is None:
            log.warning(f"[{symbol}] manual close requested but no live position; skipping")
        else:
            roe = _compute_roe_pct(pos) or 0.0
            max_roe = _position_max_roe.get(symbol, roe)
            min_roe = _position_min_roe.get(symbol, roe)
            try:
                _journal_exit_before_close(client, symbol, pos, "manual_close", max_roe, min_roe, roe)
            except Exception as e:
                log.warning(f"[{symbol}] manual close journal failed: {e}")
            try:
                client.market_close(symbol)
                log.info(f"[{symbol}] manual close executed")
            except Exception as e:
                log.error(f"[{symbol}] manual close failed: {e}")
            _position_max_roe.pop(symbol, None)
            _position_min_roe.pop(symbol, None)

    # Re-load before write-back so we don't clobber any close requests the
    # dashboard added DURING our market_close round trip. Only remove the
    # symbols we actually processed in this drain.
    processed = {str(s).upper() for s in queue}
    cfg = settings.load()
    remaining = [s for s in (cfg.get("force_close_queue") or [])
                 if str(s).upper() not in processed]
    cfg["force_close_queue"] = remaining
    settings.save(cfg)
    return True


def _resolve_symbol_config(current_settings: dict[str, Any], symbol: str
                           ) -> tuple[float, int]:
    """Effective (base_usd, leverage) for `symbol`. Falls back to config.py
    constants when config.json doesn't have an entry."""
    sc = (current_settings.get("symbol_configs") or {}).get(symbol) or {}
    try:
        base = float(sc.get("base_usd", config.BASE_TRADE_SIZE_USD.get(symbol, 0.0)))
    except (TypeError, ValueError):
        base = float(config.BASE_TRADE_SIZE_USD.get(symbol, 0.0))
    try:
        lev = int(sc.get("leverage", config.DEFAULT_LEVERAGE))
    except (TypeError, ValueError):
        lev = int(config.DEFAULT_LEVERAGE)
    return base, lev


def _sync_leverage(client: HyperliquidClient, current_settings: dict[str, Any],
                   symbols: list[str]) -> None:
    """Push leverage to Hyperliquid ONLY when the desired value differs from
    the last successfully-applied value.

    Cache lives in config.json under symbol_configs[sym].applied_leverage so
    it survives bot restarts. Without persistence the first tick after every
    restart would re-push every symbol's leverage even when nothing changed.
    """
    sc = current_settings.get("symbol_configs") or {}
    pushed: dict[str, int] = {}
    for symbol in symbols:
        entry = sc.get(symbol) or {}
        try:
            desired = int(entry.get("leverage", config.DEFAULT_LEVERAGE))
        except (TypeError, ValueError):
            desired = int(config.DEFAULT_LEVERAGE)
        applied = entry.get("applied_leverage")
        try:
            applied_int = int(applied) if applied is not None else None
        except (TypeError, ValueError):
            applied_int = None
        if applied_int == desired:
            log.debug(f"[{symbol}] leverage already {desired}x — skip push")
            continue
        try:
            client.update_leverage(symbol, desired, config.DEFAULT_LEVERAGE_IS_CROSS)
            pushed[symbol] = desired
        except Exception as e:
            log.error(f"[{symbol}] update_leverage to {desired}x failed: {e}")

    # Persist successful pushes so the next tick / restart can short-circuit.
    # Re-load to merge with any concurrent dashboard edits to other fields.
    if pushed:
        cfg = settings.load()
        live_sc = cfg.setdefault("symbol_configs", {})
        for sym, lev in pushed.items():
            live_sc.setdefault(sym, {})["applied_leverage"] = lev
        settings.save(cfg)


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
    symbols = _active_symbols(current_settings)

    # 1. Auto-seed any newly-added symbols with safe defaults so the rest of
    #    the tick (sizing, leverage push, gate ctx) doesn't trip on KeyErrors.
    if _ensure_symbol_configs(current_settings, symbols):
        cfg = settings.load()
        cfg.setdefault("symbol_configs", {}).update(current_settings["symbol_configs"])
        settings.save(cfg)

    # 2. Drain dashboard-issued manual close requests BEFORE leverage sync /
    #    new-order logic — operator intent always wins.
    _process_force_close_queue(client, current_settings)

    # 3. Push any leverage edits from the dashboard to Hyperliquid. No-op when
    #    nothing changed.
    _sync_leverage(client, current_settings, symbols)

    real_equity = client.get_account_equity()
    kill.set_anchor(real_equity, current_settings)
    equity = kill.observe(real_equity)
    _maybe_log_equity(equity, kill)
    if kill.check(equity, current_settings):
        handle_kill_switch(client, symbols)
        return

    ai_meta = current_settings.get("ai_meta") or {}
    for symbol in symbols:
        _process_symbol(client, kill, symbol, current_settings, ai_meta)


def main() -> None:
    pk, addr = load_env()
    client = HyperliquidClient(pk, addr, use_testnet=config.USE_TESTNET)
    kill = KillSwitch()

    purged = journal.purge_old()
    if purged:
        log.info(f"Journal purge: removed {purged} file(s) older than {journal.RETENTION_MONTHS} months")

    boot = settings.load()
    boot_symbols = _active_symbols(boot)
    if _ensure_symbol_configs(boot, boot_symbols):
        settings.save(boot)
    sizes = ", ".join(
        f"{s}=${_resolve_symbol_config(boot, s)[0] * boot['TRADE_SIZE_MULTIPLIER']:.0f}"
        f"@{_resolve_symbol_config(boot, s)[1]}x"
        for s in boot_symbols
    )
    log.info(
        f"Bot starting | symbols={boot_symbols} loop={config.LOOP_SECONDS}s "
        f"sizes=({sizes}) mult={boot['TRADE_SIZE_MULTIPLIER']} "
        f"loss_limit={boot['DAILY_LOSS_LIMIT'] * 100:.2f}% "
        f"rsi=({config.RSI_OVERSOLD}/{config.RSI_OVERBOUGHT}, locked) "
        f"ai_refresh={config.AI_REFRESH_SECONDS}s demo_fake_loss={config.DEMO_FAKE_LOSS}"
    )

    last_ai_run = 0.0  # force AI to run on the first tick

    while True:
        start = time.time()
        try:
            # AI cycle may overwrite config.json (suggestions / live tune), so
            # we re-load AFTER it returns to use the freshest values for tick.
            pre_settings = settings.load()
            last_ai_run = maybe_run_ai(last_ai_run, client, kill,
                                       _active_symbols(pre_settings))
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
