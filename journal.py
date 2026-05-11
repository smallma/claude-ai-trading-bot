"""Trade journal: append-only JSONL log of every ENTRY/EXIT.

One file per calendar month at journal/journal-YYYYMM.jsonl. `purge_old()`
drops files older than RETENTION_MONTHS so we keep ~6 months of history.

ENTRY records carry the full decision_context (RSI/EMA/BB readings, AI gate
votes, sentiment, funding, config snapshot) so strategy_reviewer.py can later
attribute outcomes to the parameters that produced them. EXIT records carry
exit_context (reason, entry/exit prices, max & final ROE, PnL) and share a
trade_id with the matching ENTRY.
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from logger import get_logger

log = get_logger("journal")

JOURNAL_DIR = Path(__file__).parent / "journal"
JUDGMENTS_DIR = Path(__file__).parent / "data"
JUDGMENTS_FILE = JUDGMENTS_DIR / "judgments.jsonl"
JUDGMENTS_MAX_LINES = 10_000
JUDGMENTS_KEEP_LINES = 5_000
RETENTION_MONTHS = 6

_LOCK = threading.Lock()
_JUDGMENT_LOCK = threading.Lock()
_FILE_RE = re.compile(r"journal-(\d{6})\.jsonl$")
_EQUITY_FILE_RE = re.compile(r"equity-(\d{6})\.jsonl$")


def _ensure_dir() -> None:
    JOURNAL_DIR.mkdir(exist_ok=True)


def _current_path() -> Path:
    return JOURNAL_DIR / f"journal-{datetime.now(timezone.utc).strftime('%Y%m')}.jsonl"


def _current_equity_path() -> Path:
    return JOURNAL_DIR / f"equity-{datetime.now(timezone.utc).strftime('%Y%m')}.jsonl"


def new_trade_id() -> str:
    return uuid.uuid4().hex[:12]


def _write(record: dict[str, Any]) -> None:
    _ensure_dir()
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _LOCK:
        with _current_path().open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def log_entry(symbol: str, side: str, fill_price: float, size_usd: float,
              size_units: float, trade_id: str, decision_context: dict) -> None:
    try:
        _write({
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "ENTRY",
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side,
            "fill_price": fill_price,
            "size_usd": size_usd,
            "size_units": size_units,
            "decision_context": decision_context,
        })
    except Exception as e:
        log.error(f"journal.log_entry failed for {symbol}: {e}")


def log_exit(symbol: str, side: str, fill_price: Optional[float], size_usd: Optional[float],
             size_units: float, trade_id: Optional[str], exit_context: dict) -> None:
    try:
        _write({
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": "EXIT",
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side,
            "fill_price": fill_price,
            "size_usd": size_usd,
            "size_units": size_units,
            "exit_context": exit_context,
        })
    except Exception as e:
        log.error(f"journal.log_exit failed for {symbol}: {e}")


def log_equity(equity: float, anchor_equity: Optional[float] = None,
               session_pnl_pct: Optional[float] = None) -> None:
    """Append one equity datapoint. Cheap (~80 bytes) so callers can ratelimit
    purely by wall-clock interval — see bot._maybe_log_equity (every 5 min)."""
    rec: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "equity": float(equity),
    }
    if anchor_equity is not None:
        rec["anchor_equity"] = float(anchor_equity)
    if session_pnl_pct is not None:
        rec["session_pnl_pct"] = float(session_pnl_pct)
    try:
        _ensure_dir()
        line = json.dumps(rec, default=str)
        with _LOCK:
            with _current_equity_path().open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        log.error(f"journal.log_equity failed: {e}")


def iter_equity(since: Optional[datetime] = None):
    """Yield equity datapoints across all retained equity files, oldest-first."""
    if not JOURNAL_DIR.exists():
        return
    for p in sorted(JOURNAL_DIR.glob("equity-*.jsonl")):
        try:
            with p.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if since:
                        ts = rec.get("ts")
                        if ts:
                            try:
                                if datetime.fromisoformat(ts) < since:
                                    continue
                            except ValueError:
                                pass
                    yield rec
        except OSError as e:
            log.warning(f"Could not read {p.name}: {e}")


def log_judgment(symbol: str, decision: str, info: dict[str, Any],
                 ai_score: Any = None, gate_result: Optional[str] = None,
                 gate_reason: Optional[str] = None) -> None:
    """Append one judgment record to data/judgments.jsonl.

    Called for EVERY outcome in _process_symbol: HOLD, SKIP (gate rejected),
    and BUY/SELL (executed). Auto-truncates when the file exceeds
    JUDGMENTS_MAX_LINES to keep disk bounded.
    """
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "decision": decision,
        "rsi": info.get("rsi"),
        "ema_trend": info.get("ema_trend"),
        "ema_spread_pct": info.get("ema_spread_pct"),
        "bb_position": info.get("bb_position"),
        "trigger": info.get("trigger"),
        "ai_score": ai_score,
        "gate_result": gate_result,
        "gate_reason": gate_reason,
    }
    try:
        JUDGMENTS_DIR.mkdir(exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        with _JUDGMENT_LOCK:
            with JUDGMENTS_FILE.open("a", encoding="utf-8") as f:
                f.write(line)
            _maybe_truncate_judgments()
    except Exception as e:
        log.error(f"journal.log_judgment failed for {symbol}: {e}")


def _maybe_truncate_judgments() -> None:
    """If judgments.jsonl exceeds JUDGMENTS_MAX_LINES, keep the newest
    JUDGMENTS_KEEP_LINES. Caller must already hold _JUDGMENT_LOCK."""
    try:
        if not JUDGMENTS_FILE.exists():
            return
        with JUDGMENTS_FILE.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= JUDGMENTS_MAX_LINES:
            return
        keep = lines[-JUDGMENTS_KEEP_LINES:]
        tmp = JUDGMENTS_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.writelines(keep)
        tmp.replace(JUDGMENTS_FILE)
        log.info(f"Truncated judgments.jsonl: {len(lines)} -> {len(keep)} lines")
    except Exception as e:
        log.warning(f"judgments truncation failed: {e}")


def iter_judgments(limit: int = 1000) -> list[dict[str, Any]]:
    """Return the last `limit` judgment records, newest-first."""
    if not JUDGMENTS_FILE.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        with JUDGMENTS_FILE.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    records.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        log.warning(f"Could not read judgments: {e}")
        return []
    # Return newest first, capped at limit.
    return records[-limit:][::-1]


def purge_old(months: int = RETENTION_MONTHS) -> int:
    """Delete journal-YYYYMM.jsonl AND equity-YYYYMM.jsonl files older than
    `months` calendar months. Returns total count deleted."""
    if not JOURNAL_DIR.exists():
        return 0
    now = datetime.now(timezone.utc).replace(day=1)
    # Step back `months` months by walking month-by-month (avoids 31-day drift).
    cutoff = now
    for _ in range(months):
        cutoff = (cutoff - timedelta(days=1)).replace(day=1)
    cutoff_yyyymm = cutoff.strftime("%Y%m")

    deleted = 0
    for pattern, regex in (("journal-*.jsonl", _FILE_RE),
                           ("equity-*.jsonl", _EQUITY_FILE_RE)):
        for p in JOURNAL_DIR.glob(pattern):
            m = regex.search(p.name)
            if not m:
                continue
            if m.group(1) < cutoff_yyyymm:
                try:
                    p.unlink()
                    deleted += 1
                    log.info(f"Purged old file {p.name}")
                except OSError as e:
                    log.warning(f"Could not delete {p.name}: {e}")
    return deleted


def list_files() -> list[Path]:
    """All retained files (trade journal + equity log) newest-first.
    Used by dashboard download zip."""
    if not JOURNAL_DIR.exists():
        return []
    files = list(JOURNAL_DIR.glob("journal-*.jsonl")) + list(JOURNAL_DIR.glob("equity-*.jsonl"))
    return sorted(files, reverse=True)


def iter_records(since: Optional[datetime] = None):
    """Yield records across all retained files, oldest-first.

    Used by dashboard listing and strategy_reviewer.py. Skips malformed lines
    rather than crashing — journal corruption shouldn't take down readers.
    """
    if not JOURNAL_DIR.exists():
        return
    for p in sorted(JOURNAL_DIR.glob("journal-*.jsonl")):
        try:
            with p.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if since:
                        ts = rec.get("ts")
                        if ts:
                            try:
                                if datetime.fromisoformat(ts) < since:
                                    continue
                            except ValueError:
                                pass
                    yield rec
        except OSError as e:
            log.warning(f"Could not read {p.name}: {e}")
