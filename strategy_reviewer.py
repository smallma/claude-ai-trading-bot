"""Daily strategy reviewer — high-tier Gemini model digests journal performance
and proposes config tweaks (RSI / EMA / BB / size).

Run via cron once a day, e.g.:
  5 0 * * *  /home/rain/claude-ai-trading-bot/.venv/bin/python \
             /home/rain/claude-ai-trading-bot/strategy_reviewer.py

Flow:
  1. Walk journal records for the lookback window (default 30d).
  2. Pair ENTRY <-> EXIT by trade_id, compute per-symbol / per-trigger /
     per-exit-reason performance stats.
  3. Send a structured prompt to Gemini Pro asking for:
       - one-paragraph diagnosis
       - JSON object of suggested overrides for tunable params
       - per-change rationale + overall confidence
  4. Validate suggestions against safety bounds.
  5. Write the suggestion to config.json under ai_meta.suggested_strategy.
  6. If AUTO_STRATEGY_EVOLVE is True, also write to top-level strategy_overrides
     so the bot picks them up on the next tick.
"""
import argparse
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dotenv import load_dotenv

import config
import journal
import settings
from logger import get_logger

log = get_logger("reviewer")

DEFAULT_LOOKBACK_DAYS = 30
MIN_CLOSED_TRADES = 5  # below this we won't ask the AI — not enough signal

# Safety bounds for any override the reviewer is allowed to suggest. Anything
# outside the bound is silently clamped (with a log line). Cross-field
# constraints (e.g. EMA_SLOW > EMA_FAST) are enforced after clamping.
TUNABLE_BOUNDS: dict[str, tuple[float, float]] = {
    "RSI_OVERSOLD":     (10.0, 40.0),
    "RSI_OVERBOUGHT":   (60.0, 90.0),
    "EMA_FAST_PERIOD":  (5,    20),
    "EMA_SLOW_PERIOD":  (15,   60),
    "BB_PERIOD":        (10,   40),
    "BB_STDEV":         (1.0,  3.5),
}
INT_FIELDS = {"EMA_FAST_PERIOD", "EMA_SLOW_PERIOD", "BB_PERIOD"}


# ---------- Performance stats ----------

def _pair_trades(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group records by trade_id; return (closed_pairs, open_entries).

    A closed pair is {"entry": rec, "exit": rec, "trade_id": str}.
    Records without a trade_id (legacy / older runs) are skipped.
    """
    by_id: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in records:
        tid = r.get("trade_id")
        if not tid:
            continue
        by_id[tid][r["type"]] = r

    closed = []
    open_only = []
    for tid, parts in by_id.items():
        entry = parts.get("ENTRY")
        exit_ = parts.get("EXIT")
        if entry and exit_:
            closed.append({"trade_id": tid, "entry": entry, "exit": exit_})
        elif entry:
            open_only.append(entry)
    return closed, open_only


def _summarize(closed: list[dict]) -> dict[str, Any]:
    """Crunch closed pairs into per-axis stats consumable by the prompt."""
    def empty():
        return {"trades": 0, "wins": 0, "losses": 0,
                "total_pnl_usd": 0.0, "wins_pnl": 0.0, "losses_pnl": 0.0,
                "hold_seconds": [], "max_roe": [], "final_roe": []}

    by_symbol: dict[str, dict] = defaultdict(empty)
    by_trigger: dict[str, dict] = defaultdict(empty)
    by_exit: dict[str, dict] = defaultdict(empty)
    by_params: dict[str, dict] = defaultdict(empty)
    overall = empty()

    for pair in closed:
        e, x = pair["entry"], pair["exit"]
        sym = e.get("symbol", "?")
        ec = e.get("decision_context") or {}
        xc = x.get("exit_context") or {}
        trigger = ec.get("trigger") or "unknown"
        exit_reason = xc.get("exit_reason") or "unknown"
        pnl = float(xc.get("pnl_usd") or 0.0)
        max_roe = xc.get("max_roe_pct")
        final_roe = xc.get("final_roe_pct")
        hold = xc.get("hold_seconds")

        cs = ec.get("config_snapshot") or {}
        param_key = (
            f"RSI={cs.get('RSI_OVERSOLD')}/{cs.get('RSI_OVERBOUGHT')}"
            f" EMA={cs.get('EMA_FAST_PERIOD')}/{cs.get('EMA_SLOW_PERIOD')}"
            f" BB={cs.get('BB_PERIOD')},{cs.get('BB_STDEV')}"
        )

        for bucket in (overall, by_symbol[sym], by_trigger[trigger],
                       by_exit[exit_reason], by_params[param_key]):
            bucket["trades"] += 1
            bucket["total_pnl_usd"] += pnl
            if pnl > 0:
                bucket["wins"] += 1
                bucket["wins_pnl"] += pnl
            elif pnl < 0:
                bucket["losses"] += 1
                bucket["losses_pnl"] += pnl
            if hold is not None:
                bucket["hold_seconds"].append(hold)
            if max_roe is not None:
                bucket["max_roe"].append(max_roe)
            if final_roe is not None:
                bucket["final_roe"].append(final_roe)

    def finalize(b: dict) -> dict:
        n = b["trades"]
        win_rate = (b["wins"] / n) if n else 0.0
        loss_abs = abs(b["losses_pnl"])
        profit_factor = (b["wins_pnl"] / loss_abs) if loss_abs > 0 else None
        return {
            "trades": n,
            "wins": b["wins"],
            "losses": b["losses"],
            "win_rate": round(win_rate, 3),
            "total_pnl_usd": round(b["total_pnl_usd"], 4),
            "avg_pnl_usd": round(b["total_pnl_usd"] / n, 4) if n else 0.0,
            "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
            "avg_hold_seconds": int(statistics.mean(b["hold_seconds"])) if b["hold_seconds"] else None,
            "avg_max_roe_pct": round(statistics.mean(b["max_roe"]), 2) if b["max_roe"] else None,
            "avg_final_roe_pct": round(statistics.mean(b["final_roe"]), 2) if b["final_roe"] else None,
        }

    return {
        "overall": finalize(overall),
        "by_symbol":  {k: finalize(v) for k, v in by_symbol.items()},
        "by_trigger": {k: finalize(v) for k, v in by_trigger.items()},
        "by_exit_reason": {k: finalize(v) for k, v in by_exit.items()},
        "by_params": {k: finalize(v) for k, v in by_params.items()},
    }


# ---------- Prompt + Gemini call ----------

def _current_params_block(cfg: dict) -> dict:
    overrides = cfg.get("strategy_overrides") or {}
    return {
        "RSI_OVERSOLD":    overrides.get("RSI_OVERSOLD",    config.RSI_OVERSOLD),
        "RSI_OVERBOUGHT":  overrides.get("RSI_OVERBOUGHT",  config.RSI_OVERBOUGHT),
        "EMA_FAST_PERIOD": overrides.get("EMA_FAST_PERIOD", config.EMA_FAST_PERIOD),
        "EMA_SLOW_PERIOD": overrides.get("EMA_SLOW_PERIOD", config.EMA_SLOW_PERIOD),
        "BB_PERIOD":       overrides.get("BB_PERIOD",       config.BB_PERIOD),
        "BB_STDEV":        overrides.get("BB_STDEV",        config.BB_STDEV),
    }


def _build_prompt(stats: dict, current_params: dict, lookback_days: int,
                  closed_count: int) -> str:
    bounds_block = "\n".join(
        f"  - {k}: [{lo}, {hi}]" + (" (integer)" if k in INT_FIELDS else "")
        for k, (lo, hi) in TUNABLE_BOUNDS.items()
    )
    return f"""You are a senior quantitative analyst reviewing {closed_count}
closed trades over the last {lookback_days} days from a multi-coin perp bot.
Your job: diagnose what's underperforming and propose concrete tweaks to the
strategy parameters.

=== Current strategy parameters ===
{json.dumps(current_params, indent=2)}

=== Performance stats ===
{json.dumps(stats, indent=2)}

=== Tunable parameters and bounds ===
You may suggest new values for any subset of these (omit a key to leave it
unchanged):
{bounds_block}

Cross-field constraints (also enforced after your output):
  - RSI_OVERBOUGHT - RSI_OVERSOLD must be >= 30
  - EMA_SLOW_PERIOD must be >= EMA_FAST_PERIOD + 5

=== How to reason ===
- If win_rate is low but avg_max_roe is high, trades are giving back gains —
  suggest tightening trailing/entry rather than widening RSI.
- If most exits are "opposite_signal" with negative PnL, entries are too
  trigger-happy — widen RSI thresholds or lengthen EMA periods.
- If "trailing_stop" exits dominate with positive PnL, trailing is working;
  don't change BB/RSI just to chase more entries.
- If by_symbol shows one coin bleeding, mention it but ONLY suggest a global
  param change (per-symbol overrides aren't supported yet).
- Confidence < 0.5 means "not enough signal yet, make minimal or no changes".

=== Output ===
Reply with a SINGLE JSON object (no markdown, no prose outside the JSON):

{{
  "diagnosis": "one paragraph (<= 120 words) explaining the dominant pattern",
  "suggested_overrides": {{
    "RSI_OVERSOLD": 22.0,
    "RSI_OVERBOUGHT": 78.0
  }},
  "rationale_per_change": {{
    "RSI_OVERSOLD": "short reason tied to a specific stat above"
  }},
  "confidence": 0.7
}}
"""


def _strip_codefence(text: str) -> str:
    """Tolerate Gemini wrapping JSON in ```json ... ``` despite the ask."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def call_gemini_pro(prompt: str) -> Optional[dict]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log.error("GEMINI_API_KEY not set — cannot run reviewer")
        return None
    try:
        from google import genai
    except ImportError:
        log.error("google-genai not installed")
        return None
    try:
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=config.GEMINI_REVIEWER_MODEL,
            contents=prompt,
        )
        raw = (resp.text or "").strip()
        cleaned = _strip_codefence(raw)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            log.error(f"Gemini reviewer returned non-JSON: {e}\n--- raw ---\n{raw}\n---")
            return None
    except Exception as e:
        log.error(f"Gemini Pro call failed: {e}")
        return None


# ---------- Validation ----------

def _validate_overrides(raw: dict) -> tuple[dict, list[str]]:
    """Clamp to bounds, drop unknown keys, enforce cross-field constraints."""
    notes: list[str] = []
    out: dict[str, Any] = {}

    if not isinstance(raw, dict):
        return out, ["suggested_overrides was not an object; ignored"]

    for key, val in raw.items():
        if key not in TUNABLE_BOUNDS:
            notes.append(f"dropped unknown key {key}")
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            notes.append(f"dropped {key}: not numeric ({val!r})")
            continue
        lo, hi = TUNABLE_BOUNDS[key]
        clamped = max(lo, min(hi, num))
        if clamped != num:
            notes.append(f"clamped {key}: {num} -> {clamped}")
        if key in INT_FIELDS:
            clamped = int(round(clamped))
        out[key] = clamped

    # Cross-field: RSI gap >= 30
    rsi_lo = out.get("RSI_OVERSOLD")
    rsi_hi = out.get("RSI_OVERBOUGHT")
    if rsi_lo is not None and rsi_hi is not None and (rsi_hi - rsi_lo) < 30:
        notes.append(f"RSI gap too small ({rsi_lo}/{rsi_hi}) — both dropped")
        out.pop("RSI_OVERSOLD", None)
        out.pop("RSI_OVERBOUGHT", None)

    # Cross-field: EMA_SLOW >= EMA_FAST + 5
    ef = out.get("EMA_FAST_PERIOD")
    es = out.get("EMA_SLOW_PERIOD")
    if ef is not None and es is not None and es < ef + 5:
        notes.append(f"EMA periods too close ({ef}/{es}) — both dropped")
        out.pop("EMA_FAST_PERIOD", None)
        out.pop("EMA_SLOW_PERIOD", None)

    return out, notes


# ---------- Main ----------

def run_once(lookback_days: int = DEFAULT_LOOKBACK_DAYS, dry_run: bool = False
             ) -> Optional[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    records = list(journal.iter_records(since=since))
    closed, _open = _pair_trades(records)

    if len(closed) < MIN_CLOSED_TRADES:
        log.warning(
            f"Only {len(closed)} closed trades in last {lookback_days}d "
            f"(need >= {MIN_CLOSED_TRADES}); skipping review."
        )
        return None

    stats = _summarize(closed)
    log.info(f"Stats: {len(closed)} closed trades, overall={stats['overall']}")

    cfg = settings.load()
    current_params = _current_params_block(cfg)
    prompt = _build_prompt(stats, current_params, lookback_days, len(closed))

    parsed = call_gemini_pro(prompt)
    if not parsed:
        log.error("Reviewer aborted: no parseable Gemini response")
        return None

    raw_overrides = parsed.get("suggested_overrides") or {}
    validated, notes = _validate_overrides(raw_overrides)
    for n in notes:
        log.warning(f"validation: {n}")

    suggestion = {
        "diagnosis": (parsed.get("diagnosis") or "").strip()[:1500],
        "suggested_overrides": validated,
        "rationale_per_change": parsed.get("rationale_per_change") or {},
        "confidence": float(parsed.get("confidence") or 0.0),
        "validation_notes": notes,
        "current_params": current_params,
        "stats_summary": {
            "lookback_days": lookback_days,
            "closed_trades": len(closed),
            "overall": stats["overall"],
        },
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "applied": False,
    }

    if dry_run:
        print(json.dumps(suggestion, indent=2))
        return suggestion

    auto_evolve = bool(cfg.get("AUTO_STRATEGY_EVOLVE", False))
    cfg.setdefault("ai_meta", {})
    cfg["ai_meta"]["suggested_strategy"] = suggestion

    if auto_evolve and validated:
        merged = {**(cfg.get("strategy_overrides") or {}), **validated}
        cfg["strategy_overrides"] = merged
        suggestion["applied"] = True
        suggestion["applied_at"] = datetime.now(timezone.utc).isoformat()
        log.info(f"AUTO_STRATEGY_EVOLVE=True — applied overrides: {validated}")
    else:
        log.info("AUTO_STRATEGY_EVOLVE=False — suggestion staged only.")

    settings.save(cfg)
    return suggestion


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily strategy reviewer")
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print suggestion to stdout without writing config.json")
    args = ap.parse_args()

    load_dotenv()
    result = run_once(lookback_days=args.lookback_days, dry_run=args.dry_run)
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
