"""Hyperliquid testnet client wrapper.

Isolates SDK calls so the rest of the bot speaks plain Python.
"""
import math
import time
from typing import Any, Optional

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

import config
from logger import get_logger

log = get_logger("exchange")


class HyperliquidClient:
    def __init__(self, private_key: str, address: str, use_testnet: bool = True):
        base_url = constants.TESTNET_API_URL if use_testnet else constants.MAINNET_API_URL
        self.address = address
        self.account = Account.from_key(private_key)
        self.info = Info(base_url, skip_ws=True)
        self.exchange = Exchange(self.account, base_url, account_address=address)
        # Per-symbol szDecimals from /info meta. Lazy + cached — Hyperliquid
        # only changes these on listings/delistings, so a single fetch per
        # process is fine.
        self._sz_decimals: Optional[dict[str, int]] = None
        log.info(f"Connected to Hyperliquid {'testnet' if use_testnet else 'mainnet'} as {address}")

    def _ensure_sz_decimals(self) -> dict[str, int]:
        if self._sz_decimals is None:
            meta = self.info.meta()
            self._sz_decimals = {
                a["name"]: int(a["szDecimals"])
                for a in meta.get("universe", [])
                if "name" in a and "szDecimals" in a
            }
            log.info(f"Loaded szDecimals for {len(self._sz_decimals)} symbols")
        return self._sz_decimals

    def round_size_for_symbol(self, symbol: str, sz: float) -> float:
        """Round size DOWN to the symbol's szDecimals precision.

        Floor (not nearest) so we never overshoot the intended notional.
        Example: ADA has szDecimals=0 → 75.534 becomes 75 (not 76); SOL has
        szDecimals=2 → 0.4759 becomes 0.47.

        Hyperliquid rejects any size with finer precision than szDecimals,
        which is what was producing the "ghost trades" — orders rejected by
        the exchange but journal-logged as successful.
        """
        decimals = self._ensure_sz_decimals().get(symbol)
        if decimals is None:
            raise ValueError(f"[{symbol}] no szDecimals available — symbol unknown to Hyperliquid")
        factor = 10 ** decimals
        return math.floor(sz * factor) / factor

    @staticmethod
    def _check_order_response(symbol: str, result: Any) -> dict:
        """Parse the SDK response and raise unless we see at least one filled
        or resting order ID. Hyperliquid returns HTTP 200 + status="ok" even
        when the order is rejected — the rejection lives inside
        response.data.statuses[i].error, which the SDK does NOT raise on.

        Returns the first valid status dict (with `oid`) for caller logging.
        """
        if not isinstance(result, dict):
            raise RuntimeError(f"[{symbol}] order response was not a dict: {result!r}")
        if result.get("status") != "ok":
            raise RuntimeError(f"[{symbol}] order rejected: status={result.get('status')!r} body={result}")

        response = result.get("response") or {}
        data = response.get("data") or {}
        statuses = data.get("statuses") or []
        if not statuses:
            raise RuntimeError(f"[{symbol}] order rejected: empty statuses ({result})")

        errors: list[str] = []
        valid: list[dict] = []
        for s in statuses:
            if not isinstance(s, dict):
                errors.append(repr(s))
                continue
            if s.get("error"):
                errors.append(str(s["error"]))
            elif "filled" in s and isinstance(s["filled"], dict) and s["filled"].get("oid") is not None:
                valid.append(s["filled"])
            elif "resting" in s and isinstance(s["resting"], dict) and s["resting"].get("oid") is not None:
                valid.append(s["resting"])
            else:
                # Unknown shape — refuse to assume success.
                errors.append(f"unrecognized status entry: {s}")

        if errors or not valid:
            raise RuntimeError(f"[{symbol}] order rejected by exchange: errors={errors} statuses={statuses}")
        return valid[0]

    def get_mid_price(self, symbol: str) -> float:
        mids = self.info.all_mids()
        price = mids.get(symbol)
        if price is None:
            raise RuntimeError(f"No mid price returned for {symbol}")
        return float(price)

    def get_recent_closes(self, symbol: str, interval: str, lookback: int) -> list[float]:
        # Hyperliquid candle endpoint expects ms timestamps.
        # Pull a generous window to ensure we get >= lookback candles.
        end_ms = int(time.time() * 1000)
        # 1m * lookback * 2 to be safe against gaps.
        minute_ms = 60 * 1000
        start_ms = end_ms - (lookback * 2 * minute_ms)
        candles = self.info.candles_snapshot(symbol, interval, start_ms, end_ms)
        closes = [float(c["c"]) for c in candles]
        return closes[-lookback:]

    def get_account_equity(self) -> float:
        """Total Net Equity used by the kill switch and dashboard equity curve.

        Uses crossMarginSummary.accountValue which already includes:
            totalMarginUsed + withdrawable + unrealised PnL

        This matches Hyperliquid's "Total Equity" on the Portfolio page.
        Do NOT add spot USDC on top — in cross-margin mode the same USDC
        appears in both perp and spot API responses, causing double-count.
        """
        perp_state = self.info.user_state(self.address)
        cms = perp_state.get("crossMarginSummary") or perp_state.get("marginSummary") or {}
        try:
            total = float(cms.get("accountValue") or 0.0)
        except (TypeError, ValueError):
            total = 0.0

        log.info(
            f"[equity] total=${total:.4f} "
            f"(used={cms.get('totalMarginUsed')}, withdrawable={perp_state.get('withdrawable')})"
        )
        return total

    def get_open_position(self, symbol: str) -> Optional[dict]:
        """Return position dict if user has a non-zero position in symbol, else None."""
        state = self.info.user_state(self.address)
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == symbol and float(pos.get("szi", 0)) != 0.0:
                return pos
        return None

    def market_open(self, symbol: str, is_buy: bool, usd_size: float) -> dict:
        """Open a market position sized in USD.

        Returns the raw SDK response on success. Raises RuntimeError if the
        exchange rejected the order (so callers know NOT to journal a phantom
        trade). Adds a `_rounded_sz` and `_filled_status` key to the returned
        dict for caller convenience.
        """
        price = self.get_mid_price(symbol)
        raw_sz = usd_size / price
        sz = self.round_size_for_symbol(symbol, raw_sz)
        if sz <= 0:
            raise ValueError(
                f"[{symbol}] size {sz} non-positive after szDecimals rounding "
                f"(raw={raw_sz}, price={price}, usd={usd_size})"
            )
        decimals = self._ensure_sz_decimals().get(symbol, "?")
        log.info(
            f"Submitting MARKET {'BUY' if is_buy else 'SELL'} {sz} {symbol} "
            f"(raw {raw_sz:.8f}, szDecimals={decimals}, ~${sz * price:.2f} @ ~${price:.4f})"
        )
        result = self.exchange.market_open(symbol, is_buy, sz)
        log.info(f"Order result: {result}")

        # SDK returns success even on inner rejection — verify explicitly.
        filled = self._check_order_response(symbol, result)
        log.info(f"[{symbol}] order ACCEPTED oid={filled.get('oid')} sz={sz}")

        # Surface the rounded size + filled status to callers without changing
        # the SDK response shape they already log.
        if isinstance(result, dict):
            result["_rounded_sz"] = sz
            result["_filled_status"] = filled
        return result

    def update_leverage(self, symbol: str, leverage: int,
                        is_cross: bool = True) -> dict:
        """Push a leverage change for `symbol` to Hyperliquid.

        Hyperliquid stores leverage server-side per-symbol-per-account, so this
        is a one-shot call rather than a per-order parameter. Idempotent: if
        the value already matches, the SDK still POSTs successfully.
        """
        log.info(f"[{symbol}] update_leverage -> {leverage}x ({'cross' if is_cross else 'isolated'})")
        result = self.exchange.update_leverage(int(leverage), symbol, is_cross)
        log.info(f"[{symbol}] update_leverage result: {result}")
        return result

    def market_close(self, symbol: str) -> Optional[dict]:
        """Close any open position in symbol. Returns None if nothing to close."""
        pos = self.get_open_position(symbol)
        if pos is None:
            log.info(f"No open {symbol} position to close")
            return None
        log.warning(f"Closing {symbol} position size={pos.get('szi')}")
        result = self.exchange.market_close(symbol)
        log.info(f"Close result: {result}")
        return result

