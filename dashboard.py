"""Flask dashboard for the trading bot.

Endpoints
  GET  /                      Single-page UI
  GET  /api/state             positions, equity, config, latest trades
  GET  /api/trade/<trade_id>  full ENTRY+EXIT pair for one trade_id
  POST /api/config            update whitelisted dynamic settings
  POST /api/apply-suggestion  apply ai_meta.suggested_capital -> live config
  GET  /api/download          one-click zip of all retained journal files

Bind: 127.0.0.1:8080. Public exposure happens via Caddy (HTTPS + Basic Auth)
in deploy/Caddyfile, never directly. NEVER bind 0.0.0.0 here.
"""
import io
import os
import re
import traceback
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from threading import Lock, Thread
from typing import Any, Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file

import config
import journal
import settings
import strategy_reviewer
from exchange import HyperliquidClient
from logger import get_logger

load_dotenv()

log = get_logger("dashboard")
app = Flask(__name__)
_save_lock = Lock()
_client: Optional[HyperliquidClient] = None

# Run-reviewer-on-demand state. Reviewer call (Gemini Pro) takes 20-60s, so we
# kick it off in a background thread and let the UI poll /api/state.
_review_lock = Lock()
_review_state: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    "last_lookback_days": None,
}

# Whitelist of fields the UI is allowed to write into config.json. Anything
# outside this set is rejected — same shape contract as settings.DEFAULTS.
EDITABLE_FIELDS: dict[str, type] = {
    "TRADE_SIZE_MULTIPLIER": float,
    "DAILY_LOSS_LIMIT": float,
    "AUTO_CAPITAL_TUNE": bool,
    "AUTO_STRATEGY_EVOLVE": bool,
    "TRADE_GATE_ENABLED": bool,
}

# Per-symbol sizing/leverage validation. Bounds are sanity ceilings — change
# them here if your account legitimately needs larger orders or higher leverage.
SYMBOL_BASE_USD_BOUNDS = (1.0, 10000.0)
SYMBOL_LEVERAGE_BOUNDS = (1, 50)
# Hyperliquid perp tickers are uppercase letters/numbers, 1-15 chars in practice.
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{1,15}$")


def _validate_symbol_token(sym: Any) -> Optional[str]:
    """Return None if `sym` is a syntactically valid ticker, else error string."""
    if not isinstance(sym, str):
        return f"symbol must be a string, got {type(sym).__name__}"
    if not SYMBOL_PATTERN.match(sym):
        return f"symbol {sym!r} not in allowed format (uppercase A-Z0-9, 1-15 chars)"
    return None


def _validate_symbols_list(payload: Any) -> tuple[Optional[list[str]], Optional[str]]:
    """Validate the dashboard-edited active symbols list. Comma-separated input
    is normalised on the frontend; backend just accepts a list of strings.
    """
    if not isinstance(payload, list):
        return None, "symbols must be a list"
    if not payload:
        return None, "symbols list cannot be empty (need at least 1 ticker)"
    if len(payload) > 20:
        return None, f"too many symbols ({len(payload)}); cap is 20"
    out: list[str] = []
    seen: set[str] = set()
    for raw in payload:
        if not isinstance(raw, str):
            return None, f"symbol must be a string, got {type(raw).__name__}"
        sym = raw.strip().upper()
        err = _validate_symbol_token(sym)
        if err:
            return None, err
        if sym in seen:
            continue  # silent dedupe
        seen.add(sym)
        out.append(sym)
    return out, None


def _validate_symbol_configs(payload: Any) -> tuple[Optional[dict[str, dict]], Optional[str]]:
    """Type/bounds-check a symbol_configs dict from the dashboard.

    Returns (validated_dict, None) on success or (None, error_message). Symbol
    keys are checked for format only (not a config.SYMBOLS membership) so the
    user can configure a brand-new ticker in the same Save as adding it to the
    active symbols list.
    """
    if not isinstance(payload, dict):
        return None, "symbol_configs must be an object"
    out: dict[str, dict] = {}
    base_lo, base_hi = SYMBOL_BASE_USD_BOUNDS
    lev_lo, lev_hi = SYMBOL_LEVERAGE_BOUNDS
    for sym, body in payload.items():
        err = _validate_symbol_token(sym)
        if err:
            return None, err
        if not isinstance(body, dict):
            return None, f"{sym}: value must be an object"
        if "base_usd" not in body or "leverage" not in body:
            return None, f"{sym}: must include both base_usd and leverage"
        try:
            base = float(body["base_usd"])
        except (TypeError, ValueError):
            return None, f"{sym}.base_usd: not numeric ({body['base_usd']!r})"
        if base != base:  # NaN
            return None, f"{sym}.base_usd: NaN not allowed"
        if not (base_lo <= base <= base_hi):
            return None, f"{sym}.base_usd={base} outside [{base_lo}, {base_hi}]"
        try:
            lev_raw = float(body["leverage"])
        except (TypeError, ValueError):
            return None, f"{sym}.leverage: not numeric ({body['leverage']!r})"
        if lev_raw != int(lev_raw):
            return None, f"{sym}.leverage must be an integer (got {lev_raw})"
        lev = int(lev_raw)
        if not (lev_lo <= lev <= lev_hi):
            return None, f"{sym}.leverage={lev} outside [{lev_lo}, {lev_hi}]"
        out[sym] = {"base_usd": base, "leverage": lev}
    return out, None


def _get_client() -> Optional[HyperliquidClient]:
    """Lazy singleton — read-only Hyperliquid client for position snapshots.
    Returns None if creds are missing so the dashboard still renders."""
    global _client
    if _client is not None:
        return _client
    pk = os.getenv("HYPERLIQUID_PRIVATE_KEY")
    addr = os.getenv("HYPERLIQUID_ADDRESS")
    if not pk or not addr:
        log.warning("HYPERLIQUID creds missing — dashboard runs without live positions")
        return None
    try:
        _client = HyperliquidClient(pk, addr, use_testnet=config.USE_TESTNET)
    except Exception as e:
        log.error(f"Could not init Hyperliquid client: {e}")
        return None
    return _client


def _coerce(field: str, value: Any) -> Any:
    caster = EDITABLE_FIELDS[field]
    if caster is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)
    return caster(value)


def _gather_positions(client: Optional[HyperliquidClient],
                      symbols: list[str]) -> list[dict]:
    out: list[dict] = []
    if client is None:
        for sym in symbols:
            out.append({"symbol": sym, "side": "UNKNOWN", "error": "no client"})
        return out
    for sym in symbols:
        try:
            pos = client.get_open_position(sym)
        except Exception as e:
            out.append({"symbol": sym, "side": "ERROR", "error": str(e)})
            continue
        if pos is None:
            out.append({"symbol": sym, "side": "FLAT"})
            continue
        try:
            szi = float(pos.get("szi", 0))
            pnl = float(pos.get("unrealizedPnl", 0))
            margin = float(pos.get("marginUsed", 0))
        except (TypeError, ValueError):
            szi, pnl, margin = 0.0, 0.0, 0.0
        roe = (pnl / margin * 100) if margin > 0 else None
        out.append({
            "symbol": sym,
            "side": "LONG" if szi > 0 else "SHORT" if szi < 0 else "FLAT",
            "size": abs(szi),
            "entry_price": float(pos.get("entryPx") or 0) or None,
            "unrealized_pnl": pnl,
            "margin_used": margin,
            "roe_pct": round(roe, 2) if roe is not None else None,
        })
    return out


@app.route("/")
def index():
    return render_template("dashboard.html")


def _active_symbols(cfg: dict) -> list[str]:
    """Mirror of bot._active_symbols — keep state-API symbols list aligned with
    whatever the bot would iterate this tick."""
    syms = cfg.get("symbols")
    if isinstance(syms, list) and syms:
        return [str(s).upper() for s in syms]
    return list(config.SYMBOLS)


@app.route("/api/state")
def api_state():
    cfg = settings.load()
    client = _get_client()
    active = _active_symbols(cfg)
    positions = _gather_positions(client, active)

    equity: Optional[float] = None
    if client is not None:
        try:
            equity = client.get_account_equity()
        except Exception as e:
            log.warning(f"equity fetch failed: {e}")

    # Last 50 trades, newest first. Each record already has trade_id so the UI
    # can group ENTRY+EXIT pairs.
    all_records = list(journal.iter_records())
    recent = all_records[-50:][::-1]

    return jsonify({
        "config": cfg,
        "positions": positions,
        "equity": equity,
        "trades": recent,
        "static": {
            # Active universe (dashboard-editable) — lets the UI render
            # per-symbol controls without a second round trip.
            "SYMBOLS": active,
            # Hardcoded defaults from config.py for "default" labels in the UI.
            "DEFAULT_SYMBOLS": list(config.SYMBOLS),
            "BASE_TRADE_SIZE_USD": config.BASE_TRADE_SIZE_USD,
            "RSI_OVERSOLD": config.RSI_OVERSOLD,
            "RSI_OVERBOUGHT": config.RSI_OVERBOUGHT,
            "EMA_FAST_PERIOD": config.EMA_FAST_PERIOD,
            "EMA_SLOW_PERIOD": config.EMA_SLOW_PERIOD,
            "BB_PERIOD": config.BB_PERIOD,
            "BB_STDEV": config.BB_STDEV,
            "AI_REFRESH_SECONDS": config.AI_REFRESH_SECONDS,
            "NEW_SYMBOL_DEFAULT_BASE_USD": config.NEW_SYMBOL_DEFAULT_BASE_USD,
            "NEW_SYMBOL_DEFAULT_LEVERAGE": config.NEW_SYMBOL_DEFAULT_LEVERAGE,
            "USE_TESTNET": config.USE_TESTNET,
        },
        "now": datetime.now(timezone.utc).isoformat(),
        "journal_files": [p.name for p in journal.list_files()],
        "editable_fields": list(EDITABLE_FIELDS.keys()),
        "review_state": dict(_review_state),
    })


@app.route("/api/trade/<trade_id>")
def api_trade_detail(trade_id: str):
    """Return the ENTRY + EXIT (if any) records for one trade_id."""
    matches = [r for r in journal.iter_records() if r.get("trade_id") == trade_id]
    if not matches:
        return jsonify({"ok": False, "error": "trade_id not found"}), 404
    return jsonify({"ok": True, "records": matches})


@app.route("/api/config", methods=["POST"])
def api_set_config():
    payload = request.get_json(force=True, silent=True) or {}
    if not isinstance(payload, dict) or not payload:
        return jsonify({"ok": False, "error": "empty or non-object body"}), 400

    # Compound fields handled separately from the simple scalar whitelist.
    symbol_configs_raw = payload.pop("symbol_configs", None)
    symbols_raw = payload.pop("symbols", None)

    bad = [k for k in payload if k not in EDITABLE_FIELDS]
    if bad:
        return jsonify({"ok": False, "error": f"fields not editable: {bad}"}), 400

    coerced: dict[str, Any] = {}
    for k, v in payload.items():
        try:
            coerced[k] = _coerce(k, v)
        except (TypeError, ValueError) as e:
            return jsonify({"ok": False, "error": f"bad value for {k}: {e}"}), 400

    validated_symbol_configs: Optional[dict] = None
    if symbol_configs_raw is not None:
        validated_symbol_configs, err = _validate_symbol_configs(symbol_configs_raw)
        if err:
            return jsonify({"ok": False, "error": err}), 400

    validated_symbols: Optional[list[str]] = None
    if symbols_raw is not None:
        validated_symbols, err = _validate_symbols_list(symbols_raw)
        if err:
            return jsonify({"ok": False, "error": err}), 400

    changed: dict[str, Any] = {}
    with _save_lock:
        cfg = settings.load()
        for k, v in coerced.items():
            if cfg.get(k) != v:
                changed[k] = {"from": cfg.get(k), "to": v}
                cfg[k] = v
        if validated_symbol_configs is not None:
            # Merge — partial submissions only affect the symbols included.
            current_sc = dict(cfg.get("symbol_configs") or {})
            sc_diff: dict[str, Any] = {}
            for sym, body in validated_symbol_configs.items():
                if current_sc.get(sym) != body:
                    sc_diff[sym] = {"from": current_sc.get(sym), "to": body}
                    current_sc[sym] = body
            if sc_diff:
                changed["symbol_configs"] = sc_diff
                cfg["symbol_configs"] = current_sc
        if validated_symbols is not None:
            if cfg.get("symbols") != validated_symbols:
                changed["symbols"] = {"from": cfg.get("symbols"), "to": validated_symbols}
                cfg["symbols"] = validated_symbols
                # Auto-seed any newly-added symbols so bot doesn't crash on
                # first tick. Mirrors bot._ensure_symbol_configs's defaults.
                sc = dict(cfg.get("symbol_configs") or {})
                seeded: dict[str, dict] = {}
                for sym in validated_symbols:
                    if sym not in sc:
                        sc[sym] = {
                            "base_usd": float(config.NEW_SYMBOL_DEFAULT_BASE_USD),
                            "leverage": int(config.NEW_SYMBOL_DEFAULT_LEVERAGE),
                        }
                        seeded[sym] = sc[sym]
                if seeded:
                    cfg["symbol_configs"] = sc
                    changed.setdefault("symbol_configs", {}).update({
                        sym: {"from": None, "to": v, "auto_seeded": True}
                        for sym, v in seeded.items()
                    })
        settings.save(cfg)

    log.info(f"config update via dashboard: {changed}")
    return jsonify({"ok": True, "changed": changed})


@app.route("/api/close-position/<symbol>", methods=["POST"])
def api_close_position(symbol: str):
    """Append `symbol` to config.json -> force_close_queue. The bot drains the
    queue at the top of its next tick, market_closes the position, journals
    the EXIT with reason "manual_close", and removes the entry.

    Idempotent: clicking twice quickly only queues once (deduped). Bot also
    no-ops gracefully if the symbol has no live position by the time it
    drains the queue.
    """
    sym = (symbol or "").strip().upper()
    err = _validate_symbol_token(sym)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    with _save_lock:
        cfg = settings.load()
        queue = list(cfg.get("force_close_queue") or [])
        already = sym in {str(s).upper() for s in queue}
        if not already:
            queue.append(sym)
            cfg["force_close_queue"] = queue
            settings.save(cfg)

    log.warning(f"manual close queued for {sym} (already={already}, queue={queue})")
    return jsonify({"ok": True, "queued": sym, "already_queued": already, "queue": queue})


@app.route("/api/apply-strategy-suggestion", methods=["POST"])
def api_apply_strategy_suggestion():
    """Promote ai_meta.suggested_strategy.suggested_overrides -> live strategy_overrides.

    Used when AUTO_STRATEGY_EVOLVE=False — strategy_reviewer.py stages the
    suggestion, the operator clicks Apply on the dashboard.
    """
    with _save_lock:
        cfg = settings.load()
        meta = cfg.get("ai_meta") or {}
        sugg = meta.get("suggested_strategy")
        if not sugg:
            return jsonify({"ok": False, "error": "no strategy suggestion staged"}), 404
        overrides = sugg.get("suggested_overrides") or {}
        if not overrides:
            return jsonify({"ok": False, "error": "suggestion has no overrides to apply"}), 400

        merged = {**(cfg.get("strategy_overrides") or {}), **overrides}
        cfg["strategy_overrides"] = merged
        sugg["applied"] = True
        sugg["applied_at"] = datetime.now(timezone.utc).isoformat()
        meta["suggested_strategy"] = sugg
        cfg["ai_meta"] = meta
        settings.save(cfg)

    log.info(f"strategy suggestion applied: {overrides}")
    return jsonify({"ok": True, "applied": overrides})


def _run_reviewer_bg(lookback_days: int) -> None:
    """Background thread body — runs reviewer, captures result/error in module state."""
    try:
        result = strategy_reviewer.run_once(lookback_days=lookback_days, dry_run=False)
        with _review_lock:
            if result is None:
                # run_once returned None — either too few trades or Gemini failed.
                # Distinguish by checking journal length quickly.
                _review_state["last_error"] = (
                    "reviewer returned no result — check VM logs (likely "
                    "<5 closed trades or Gemini call failed)"
                )
            else:
                _review_state["last_error"] = None
    except Exception as e:
        log.error(f"reviewer bg thread crashed: {e}\n{traceback.format_exc()}")
        with _review_lock:
            _review_state["last_error"] = f"{type(e).__name__}: {e}"
    finally:
        with _review_lock:
            _review_state["running"] = False
            _review_state["finished_at"] = datetime.now(timezone.utc).isoformat()


@app.route("/api/run-reviewer", methods=["POST"])
def api_run_reviewer():
    """Kick off strategy_reviewer in a background thread. Returns 202 + state.

    Optional body: {"lookback_days": 30}. UI polls /api/state.review_state to
    see when it finishes; the suggestion shows up under ai_meta.suggested_strategy.
    """
    payload = request.get_json(force=True, silent=True) or {}
    try:
        lookback = int(payload.get("lookback_days", strategy_reviewer.DEFAULT_LOOKBACK_DAYS))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "lookback_days must be an integer"}), 400
    if not (1 <= lookback <= 180):
        return jsonify({"ok": False, "error": "lookback_days must be 1-180"}), 400

    with _review_lock:
        if _review_state["running"]:
            return jsonify({"ok": False, "error": "reviewer already running",
                            "state": dict(_review_state)}), 409
        _review_state.update({
            "running": True,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "last_error": None,
            "last_lookback_days": lookback,
        })

    Thread(target=_run_reviewer_bg, args=(lookback,), daemon=True).start()
    log.info(f"reviewer dispatched (lookback={lookback}d)")
    return jsonify({"ok": True, "state": dict(_review_state)}), 202


@app.route("/api/strategy-overrides", methods=["POST"])
def api_set_strategy_overrides():
    """Manual strategy override write. Strict validation — refuses to clamp
    silently like the reviewer does, because a human typo should produce a
    clear error instead of a stealth value change.

    Body: {"RSI_OVERSOLD": 22, "EMA_FAST_PERIOD": 7, "BB_STDEV": null, ...}
    A null/empty value REMOVES that override (revert to config.py default for
    that single key). Unspecified keys are left untouched.

    Bounds and integer constraints come from strategy_reviewer.TUNABLE_BOUNDS
    so reviewer + manual edits stay in lockstep.
    """
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict) or not payload:
        return jsonify({"ok": False, "error": "empty or non-object body"}), 400

    bounds = strategy_reviewer.TUNABLE_BOUNDS
    int_fields = strategy_reviewer.INT_FIELDS

    bad = [k for k in payload if k not in bounds]
    if bad:
        return jsonify({"ok": False,
                        "error": f"unknown fields: {bad}",
                        "allowed": list(bounds.keys())}), 400

    # Phase 1 — coerce + range-check each field. We compute the FULL effective
    # set (current overrides merged with payload) before cross-field checks so
    # users can change one half of a constrained pair if the other half is
    # already a compatible default/override.
    cfg = settings.load()
    current_overrides: dict[str, Any] = dict(cfg.get("strategy_overrides") or {})
    pending = dict(current_overrides)
    removals: list[str] = []

    for k, v in payload.items():
        # Treat null / empty as "remove this override".
        if v is None or (isinstance(v, str) and v.strip() == ""):
            if k in pending:
                removals.append(k)
                pending.pop(k, None)
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            return jsonify({"ok": False,
                            "error": f"{k}: not numeric ({v!r})"}), 400
        if num != num:  # NaN check
            return jsonify({"ok": False, "error": f"{k}: NaN not allowed"}), 400
        lo, hi = bounds[k]
        if not (lo <= num <= hi):
            return jsonify({"ok": False,
                            "error": f"{k}={num} outside allowed range [{lo}, {hi}]"}), 400
        if k in int_fields:
            if num != int(num):
                return jsonify({"ok": False,
                                "error": f"{k} must be an integer (got {num})"}), 400
            num = int(num)
        # If the submitted value equals the config.py default, treat as a
        # remove instead of storing a redundant override. Saves UI churn when
        # the user just hits Save without changing anything.
        default_val = getattr(config, k, None)
        if default_val is not None and num == default_val:
            if k in pending:
                removals.append(k)
                pending.pop(k, None)
            continue
        pending[k] = num

    # Phase 2 — cross-field constraints on the EFFECTIVE values (override or default).
    def effective(key: str) -> float:
        if key in pending:
            return float(pending[key])
        return float(getattr(config, key))

    rsi_lo = effective("RSI_OVERSOLD")
    rsi_hi = effective("RSI_OVERBOUGHT")
    if rsi_hi - rsi_lo < 30:
        return jsonify({"ok": False,
                        "error": (f"RSI gap too small: {rsi_lo}/{rsi_hi} (need "
                                  "RSI_OVERBOUGHT - RSI_OVERSOLD >= 30)")}), 400

    ema_fast = effective("EMA_FAST_PERIOD")
    ema_slow = effective("EMA_SLOW_PERIOD")
    if ema_slow < ema_fast + 5:
        return jsonify({"ok": False,
                        "error": (f"EMA periods too close: fast={ema_fast} "
                                  f"slow={ema_slow} (need slow >= fast + 5)")}), 400

    # Phase 3 — persist.
    diff: dict[str, Any] = {}
    for k in payload.keys():
        before = current_overrides.get(k)
        after = pending.get(k) if k not in removals else None
        if before != after:
            diff[k] = {"from": before, "to": after}

    with _save_lock:
        cfg = settings.load()
        cfg["strategy_overrides"] = pending
        settings.save(cfg)

    log.info(f"strategy_overrides updated via dashboard: {diff}")
    return jsonify({
        "ok": True,
        "overrides": pending,
        "changed": diff,
        "removed": removals,
    })


@app.route("/api/clear-strategy-overrides", methods=["POST"])
def api_clear_strategy_overrides():
    """Wipe strategy_overrides, reverting to config.py defaults."""
    with _save_lock:
        cfg = settings.load()
        prev = cfg.get("strategy_overrides") or {}
        cfg["strategy_overrides"] = {}
        settings.save(cfg)
    log.info(f"strategy_overrides cleared (was: {prev})")
    return jsonify({"ok": True, "cleared": prev})


@app.route("/api/apply-suggestion", methods=["POST"])
def api_apply_suggestion():
    """Promote ai_meta.suggested_capital -> live TRADE_SIZE_MULTIPLIER / DAILY_LOSS_LIMIT.

    Used when AUTO_CAPITAL_TUNE=False — the AI cycle stages a suggestion, the
    operator clicks Apply on the dashboard.
    """
    with _save_lock:
        cfg = settings.load()
        meta = cfg.get("ai_meta") or {}
        sugg = meta.get("suggested_capital")
        if not sugg:
            return jsonify({"ok": False, "error": "no suggestion staged"}), 404
        try:
            new_mult = float(sugg["TRADE_SIZE_MULTIPLIER"])
            new_loss = float(sugg["DAILY_LOSS_LIMIT"])
        except (KeyError, TypeError, ValueError) as e:
            return jsonify({"ok": False, "error": f"malformed suggestion: {e}"}), 500

        cfg["TRADE_SIZE_MULTIPLIER"] = new_mult
        cfg["DAILY_LOSS_LIMIT"] = new_loss
        sugg["applied"] = True
        sugg["applied_at"] = datetime.now(timezone.utc).isoformat()
        meta["suggested_capital"] = sugg
        cfg["ai_meta"] = meta
        settings.save(cfg)

    log.info(f"suggestion applied: mult={new_mult} loss={new_loss}")
    return jsonify({
        "ok": True,
        "applied": {"TRADE_SIZE_MULTIPLIER": new_mult, "DAILY_LOSS_LIMIT": new_loss},
    })


def _parse_days(default: int = 30, cap: int = 365) -> int:
    """Parse ?days=N from query string, clamped to [1, cap]."""
    try:
        n = int(request.args.get("days", default))
    except (TypeError, ValueError):
        n = default
    return max(1, min(cap, n))


def _downsample(points: list[dict], max_points: int) -> list[dict]:
    """Reservoir-style stride sampling. Keeps first + last point intact."""
    n = len(points)
    if n <= max_points or max_points < 3:
        return points
    stride = (n - 1) / (max_points - 1)
    out = [points[int(round(i * stride))] for i in range(max_points - 1)]
    out.append(points[-1])
    return out


@app.route("/api/equity-history")
def api_equity_history():
    """Equity datapoints for charting. Returns oldest-first.

    Query: ?days=N (1-365, default 30). Auto-downsamples to <=2000 points so
    long lookbacks stay snappy in the browser.
    """
    days = _parse_days(default=30, cap=365)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    points = [
        {"ts": r["ts"], "equity": r["equity"],
         "session_pnl_pct": r.get("session_pnl_pct")}
        for r in journal.iter_equity(since=since)
    ]
    sampled = _downsample(points, max_points=2000)
    return jsonify({
        "ok": True,
        "days": days,
        "raw_count": len(points),
        "returned_count": len(sampled),
        "points": sampled,
    })


@app.route("/api/ai-confidence-history")
def api_ai_confidence_history():
    """Time series of sentiment + gate confidence captured at every ENTRY.

    Each ENTRY in the journal carries the sentiment snapshot that justified
    the trade, plus the structured gate votes. We project that into three
    series the UI can plot:

      - sentiment_score    (0-10)
      - sentiment_confidence (0-1)
      - gate_go_ratio      (0-1) — share of analysts who voted GO

    Query: ?days=N (1-365, default 30). Returns oldest-first, downsampled.
    """
    days = _parse_days(default=30, cap=365)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    points: list[dict[str, Any]] = []
    for r in journal.iter_records(since=since):
        if r.get("type") != "ENTRY":
            continue
        ctx = r.get("decision_context") or {}
        sent = ctx.get("sentiment") or {}
        score = sent.get("score")
        if score is None:
            continue  # nothing meaningful to plot for this entry
        confidence = sent.get("confidence")

        votes = ((ctx.get("ai_gate") or {}).get("votes")) or {}
        go = sum(1 for v in votes.values() if (v or {}).get("decision") == "GO")
        total = len(votes)
        gate_ratio = (go / total) if total else None

        points.append({
            "ts": r.get("ts"),
            "symbol": r.get("symbol"),
            "sentiment_score": score,
            "sentiment_confidence": confidence,
            "gate_go_ratio": gate_ratio,
        })

    sampled = _downsample(points, max_points=2000)
    return jsonify({
        "ok": True,
        "days": days,
        "raw_count": len(points),
        "returned_count": len(sampled),
        "points": sampled,
    })


def _aggregate_pnl_by(records, key_extractor):
    """Shared aggregator: walks EXIT records, groups by `key_extractor(rec)`."""
    agg: dict[str, dict[str, float]] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "losses": 0, "total_pnl_usd": 0.0}
    )
    for r in records:
        if r.get("type") != "EXIT":
            continue
        key = key_extractor(r)
        if key is None:
            continue
        pnl = (r.get("exit_context") or {}).get("pnl_usd")
        if pnl is None:
            continue
        try:
            pnl_f = float(pnl)
        except (TypeError, ValueError):
            continue
        b = agg[str(key)]
        b["trades"] += 1
        b["total_pnl_usd"] += pnl_f
        if pnl_f > 0:
            b["wins"] += 1
        elif pnl_f < 0:
            b["losses"] += 1

    rows = []
    for k in sorted(agg.keys()):
        b = agg[k]
        n = b["trades"]
        rows.append({
            "key": k,
            "trades": n,
            "wins": b["wins"],
            "losses": b["losses"],
            "win_rate": round(b["wins"] / n, 3) if n else 0.0,
            "total_pnl_usd": round(b["total_pnl_usd"], 4),
            "avg_pnl_usd": round(b["total_pnl_usd"] / n, 4) if n else 0.0,
        })
    rows.sort(key=lambda r: r["total_pnl_usd"], reverse=True)
    return rows


@app.route("/api/pnl-by-trigger")
def api_pnl_by_trigger():
    """Realised PnL grouped by the `decision_context.trigger` of the matching ENTRY.

    EXIT records don't carry the trigger themselves, so we build a trade_id ->
    trigger map from ENTRYs first, then aggregate.
    """
    days = _parse_days(default=30, cap=365)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    records = list(journal.iter_records(since=since))
    trigger_by_id: dict[str, str] = {}
    for r in records:
        if r.get("type") == "ENTRY":
            tid = r.get("trade_id")
            trig = (r.get("decision_context") or {}).get("trigger")
            if tid and trig:
                trigger_by_id[tid] = trig

    rows = _aggregate_pnl_by(
        records,
        lambda rec: trigger_by_id.get(rec.get("trade_id")),
    )
    return jsonify({
        "ok": True, "days": days,
        "by_trigger": rows,
        "total_pnl_usd": round(sum(r["total_pnl_usd"] for r in rows), 4),
        "total_trades": sum(r["trades"] for r in rows),
    })


@app.route("/api/pnl-by-exit-reason")
def api_pnl_by_exit_reason():
    """Realised PnL grouped by EXIT's exit_reason (trailing_stop / opposite_signal / kill_switch)."""
    days = _parse_days(default=30, cap=365)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = _aggregate_pnl_by(
        journal.iter_records(since=since),
        lambda rec: (rec.get("exit_context") or {}).get("exit_reason"),
    )
    return jsonify({
        "ok": True, "days": days,
        "by_exit_reason": rows,
        "total_pnl_usd": round(sum(r["total_pnl_usd"] for r in rows), 4),
        "total_trades": sum(r["trades"] for r in rows),
    })


@app.route("/api/pnl-by-symbol")
def api_pnl_by_symbol():
    """Aggregate REALISED PnL per symbol from EXIT records over the lookback window.

    Query: ?days=N (1-365, default 30).
    """
    days = _parse_days(default=30, cap=365)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    agg: dict[str, dict[str, float]] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "losses": 0, "total_pnl_usd": 0.0}
    )
    for r in journal.iter_records(since=since):
        if r.get("type") != "EXIT":
            continue
        sym = r.get("symbol") or "?"
        pnl = (r.get("exit_context") or {}).get("pnl_usd")
        if pnl is None:
            continue
        try:
            pnl_f = float(pnl)
        except (TypeError, ValueError):
            continue
        b = agg[sym]
        b["trades"] += 1
        b["total_pnl_usd"] += pnl_f
        if pnl_f > 0:
            b["wins"] += 1
        elif pnl_f < 0:
            b["losses"] += 1

    by_symbol = []
    for sym in sorted(agg.keys()):
        b = agg[sym]
        n = b["trades"]
        by_symbol.append({
            "symbol": sym,
            "trades": n,
            "wins": b["wins"],
            "losses": b["losses"],
            "win_rate": round(b["wins"] / n, 3) if n else 0.0,
            "total_pnl_usd": round(b["total_pnl_usd"], 4),
            "avg_pnl_usd": round(b["total_pnl_usd"] / n, 4) if n else 0.0,
        })

    return jsonify({
        "ok": True,
        "days": days,
        "by_symbol": by_symbol,
        "total_pnl_usd": round(sum(b["total_pnl_usd"] for b in agg.values()), 4),
        "total_trades": sum(b["trades"] for b in agg.values()),
    })


@app.route("/api/download")
def api_download():
    """Stream all retained journal files as a zip."""
    files = journal.list_files()
    if not files:
        return jsonify({"ok": False, "error": "no journal files yet"}), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, arcname=p.name)
    buf.seek(0)
    fname = f"journal-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.zip"
    return send_file(buf, download_name=fname, mimetype="application/zip", as_attachment=True)


if __name__ == "__main__":
    # Loopback only — Caddy in front handles TLS + Basic Auth.
    app.run(host="127.0.0.1", port=int(os.getenv("DASHBOARD_PORT", "8080")), debug=False)
