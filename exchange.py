"""Hyperliquid testnet client wrapper.

Isolates SDK calls so the rest of the bot speaks plain Python.
"""
import time
from typing import Optional

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
        log.info(f"Connected to Hyperliquid {'testnet' if use_testnet else 'mainnet'} as {address}")

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
        # Hyperliquid unified accounts pool spot + perp into one margin set.
        # marginSummary.accountValue alone reflects only the perp leg, so add
        # spot USDC to get the true equity used by the unified margin engine.
        perp_state = self.info.user_state(self.address)
        perp_value = float(perp_state["marginSummary"]["accountValue"])
        spot_state = self.info.spot_user_state(self.address)
        spot_usdc = next(
            (float(b["total"]) for b in spot_state.get("balances", []) if b.get("coin") == "USDC"),
            0.0,
        )
        return perp_value + spot_usdc

    def get_open_position(self, symbol: str) -> Optional[dict]:
        """Return position dict if user has a non-zero position in symbol, else None."""
        state = self.info.user_state(self.address)
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == symbol and float(pos.get("szi", 0)) != 0.0:
                return pos
        return None

    def market_open(self, symbol: str, is_buy: bool, usd_size: float) -> dict:
        """Open a market position sized in USD."""
        price = self.get_mid_price(symbol)
        sz = self._round_size(usd_size / price)
        if sz <= 0:
            raise ValueError(f"Computed size {sz} is non-positive (price={price}, usd={usd_size})")
        log.info(f"Submitting MARKET {'BUY' if is_buy else 'SELL'} {sz} {symbol} (~${usd_size:.2f} @ ~${price:.4f})")
        result = self.exchange.market_open(symbol, is_buy, sz)
        log.info(f"Order result: {result}")
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

    @staticmethod
    def _round_size(sz: float) -> float:
        # SOL perp on Hyperliquid supports 2 decimal sizes; round down to be safe.
        return float(int(sz * 100)) / 100.0
