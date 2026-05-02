"""Equity-based kill switch.

Anchors on the bot's starting equity (set once on the first call) and trips
when current equity drops by DAILY_LOSS_LIMIT or more from that anchor.

Also exposes `trigger_fake_loss_demo` for end-to-end testing of the
close-then-halt mechanics: with DEMO_FAKE_LOSS = True, the second tick onward
reports equity as 99% of the anchor, forcing the kill switch to trip.
"""
import config
from logger import get_logger

log = get_logger("risk")


class KillSwitch:
    def __init__(self):
        self.anchor_equity: float | None = None
        self.tripped: bool = False
        self.tick_count: int = 0

    def set_anchor(self, equity: float) -> None:
        if self.anchor_equity is None:
            self.anchor_equity = equity
            log.info(
                f"Starting equity anchor set: ${equity:.2f} | "
                f"loss limit: {config.DAILY_LOSS_LIMIT * 100:.2f}% "
                f"(halt below ${equity * (1 - config.DAILY_LOSS_LIMIT):.2f})"
            )

    def observe(self, real_equity: float) -> float:
        """Apply demo-mode equity injection if enabled. Returns equity to act on."""
        self.tick_count += 1
        if config.DEMO_FAKE_LOSS and self.tick_count >= 2 and self.anchor_equity is not None:
            return trigger_fake_loss_demo(real_equity, self.anchor_equity)
        return real_equity

    def check(self, equity: float) -> bool:
        """Returns True if the kill switch should trip THIS tick."""
        if self.tripped or self.anchor_equity is None:
            return False
        drawdown = (self.anchor_equity - equity) / self.anchor_equity
        log.info(
            f"Equity check: now=${equity:.2f} anchor=${self.anchor_equity:.2f} "
            f"drawdown={drawdown * 100:.3f}%"
        )
        if drawdown >= config.DAILY_LOSS_LIMIT:
            self.tripped = True
            log.critical(
                f"KILL SWITCH TRIPPED: drawdown {drawdown * 100:.3f}% "
                f">= {config.DAILY_LOSS_LIMIT * 100:.3f}%"
            )
            return True
        return False


def trigger_fake_loss_demo(real_equity: float, anchor_equity: float) -> float:
    """Return an artificial equity 1% below anchor to force a kill-switch trip."""
    fake = anchor_equity * 0.99
    log.warning(
        f"[DEMO] trigger_fake_loss_demo active: real=${real_equity:.2f} "
        f"-> reporting fake=${fake:.2f} (1% below ${anchor_equity:.2f})"
    )
    return fake
