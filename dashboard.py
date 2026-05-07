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


def _gather_positions(client: Optional[HyperliquidClient]) -> list[dict]:
    out: list[dict] = []
    if client is None:
        for sym in config.SYMBOLS:
            out.append({"symbol": sym, "side": "UNKNOWN", "error": "no client"})
        return out
    for sym in config.SYMBOLS:
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
    return render_template("dashboard.html", symbols=config.SYMBOLS)


@app.route("/api/state")
def api_state():
    cfg = settings.load()
    client = _get_client()
    positions = _gather_positions(client)

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
            "SYMBOLS": config.SYMBOLS,
            "BASE_TRADE_SIZE_USD": config.BASE_TRADE_SIZE_USD,
            "RSI_OVERSOLD": config.RSI_OVERSOLD,
            "RSI_OVERBOUGHT": config.RSI_OVERBOUGHT,
            "EMA_FAST_PERIOD": config.EMA_FAST_PERIOD,
            "EMA_SLOW_PERIOD": config.EMA_SLOW_PERIOD,
            "BB_PERIOD": config.BB_PERIOD,
            "BB_STDEV": config.BB_STDEV,
            "AI_REFRESH_SECONDS": config.AI_REFRESH_SECONDS,
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

    bad = [k for k in payload if k not in EDITABLE_FIELDS]
    if bad:
        return jsonify({"ok": False, "error": f"fields not editable: {bad}"}), 400

    changed: dict[str, Any] = {}
    coerced: dict[str, Any] = {}
    for k, v in payload.items():
        try:
            coerced[k] = _coerce(k, v)
        except (TypeError, ValueError) as e:
            return jsonify({"ok": False, "error": f"bad value for {k}: {e}"}), 400

    with _save_lock:
        cfg = settings.load()
        for k, v in coerced.items():
            if cfg.get(k) != v:
                changed[k] = {"from": cfg.get(k), "to": v}
                cfg[k] = v
        settings.save(cfg)

    log.info(f"config update via dashboard: {changed}")
    return jsonify({"ok": True, "changed": changed})


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
