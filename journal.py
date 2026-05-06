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
RETENTION_MONTHS = 6

_LOCK = threading.Lock()
_FILE_RE = re.compile(r"journal-(\d{6})\.jsonl$")


def _ensure_dir() -> None:
    JOURNAL_DIR.mkdir(exist_ok=True)


def _current_path() -> Path:
    return JOURNAL_DIR / f"journal-{datetime.now(timezone.utc).strftime('%Y%m')}.jsonl"


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


def purge_old(months: int = RETENTION_MONTHS) -> int:
    """Delete journal-YYYYMM.jsonl files older than `months` calendar months."""
    if not JOURNAL_DIR.exists():
        return 0
    now = datetime.now(timezone.utc).replace(day=1)
    # Step back `months` months by walking month-by-month (avoids 31-day drift).
    cutoff = now
    for _ in range(months):
        cutoff = (cutoff - timedelta(days=1)).replace(day=1)
    cutoff_yyyymm = cutoff.strftime("%Y%m")

    deleted = 0
    for p in JOURNAL_DIR.glob("journal-*.jsonl"):
        m = _FILE_RE.search(p.name)
        if not m:
            continue
        if m.group(1) < cutoff_yyyymm:
            try:
                p.unlink()
                deleted += 1
                log.info(f"Purged old journal {p.name}")
            except OSError as e:
                log.warning(f"Could not delete {p.name}: {e}")
    return deleted


def list_files() -> list[Path]:
    """Return all journal files newest-first (used by dashboard download)."""
    if not JOURNAL_DIR.exists():
        return []
    return sorted(JOURNAL_DIR.glob("journal-*.jsonl"), reverse=True)


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
