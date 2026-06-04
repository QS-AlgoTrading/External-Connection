"""Live mark price source for external paper sessions.

Wraps `kucoin_fetcher`-style ccxt access behind a small in-memory
cache so repeated calls within a short window don't hit KuCoin's
rate limit. v1 default: 60-second TTL — fresh enough for paper
trading, cheap enough to handle many Hermes API hits.

Single shared cache across all strategy sessions (BTCUSDT's mark is
the same regardless of which strategy is asking).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Protocol

import ccxt

logger = logging.getLogger(__name__)


# ─── Protocol the application layer holds a reference to ────────────────────


class PriceSource(Protocol):
    def get_mark(self, symbol: str) -> float | None:
        """Return the latest mark price for `symbol`, or None on failure."""


# ─── KuCoin live ticker source ──────────────────────────────────────────────


@dataclass
class _CachedTicker:
    price: float
    fetched_at: float  # monotonic seconds


class KuCoinTickerSource:
    """ccxt-backed last-price source with a short TTL cache.

    Symbol format matches the rest of the codebase: parquet-style
    "BTCUSDT" is translated to ccxt-style "BTC/USDT:USDT" (perp). The
    translation is hardcoded for v1's BTC-only universe; extending it
    is a config change.
    """

    _SYMBOL_TRANSLATION = {
        "BTCUSDT": "BTC/USDT:USDT",
    }

    def __init__(self, *, cache_ttl_sec: float = 60.0):
        self.cache_ttl_sec = cache_ttl_sec
        self._cache: dict[str, _CachedTicker] = {}
        # KuCoin futures (perp) exchange instance — public endpoints only,
        # no credentials needed for ticker fetch.
        self._exchange = ccxt.kucoinfutures({"enableRateLimit": True})

    def get_mark(self, symbol: str) -> float | None:
        now = time.monotonic()
        cached = self._cache.get(symbol)
        if cached is not None and (now - cached.fetched_at) < self.cache_ttl_sec:
            return cached.price

        ccxt_symbol = self._SYMBOL_TRANSLATION.get(symbol)
        if ccxt_symbol is None:
            logger.warning("KuCoinTickerSource: no translation for %s", symbol)
            return None

        try:
            ticker = self._exchange.fetch_ticker(ccxt_symbol)
        except ccxt.BaseError as exc:
            logger.warning(
                "KuCoinTickerSource: fetch_ticker(%s) failed: %s", ccxt_symbol, exc
            )
            # Return stale value if we have one; better than nothing.
            return cached.price if cached is not None else None

        # ccxt returns 'last' as the last trade price.
        last = ticker.get("last")
        if last is None:
            logger.warning("KuCoinTickerSource: ticker has no 'last' for %s", ccxt_symbol)
            return cached.price if cached is not None else None

        price = float(last)
        self._cache[symbol] = _CachedTicker(price=price, fetched_at=now)
        return price

    def invalidate(self, symbol: str | None = None) -> None:
        """Drop cached entries (all if `symbol` is None)."""
        if symbol is None:
            self._cache.clear()
        else:
            self._cache.pop(symbol, None)


# ─── In-memory source for tests ─────────────────────────────────────────────


class StaticPriceSource:
    """Test double — returns whatever prices it's been told.

    Pass `prices={'BTCUSDT': 60_000.0}` at construction; modify the
    dict at any time and `get_mark` reflects the change immediately.
    """

    def __init__(self, prices: Optional[dict[str, float]] = None):
        self.prices: dict[str, float] = dict(prices or {})

    def get_mark(self, symbol: str) -> float | None:
        return self.prices.get(symbol)

    def set(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price
