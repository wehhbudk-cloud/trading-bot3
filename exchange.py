"""Read-only client for Binance's public Spot market-data API."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BINANCE_SPOT_BASE_URL = "https://data-api.binance.vision"
DEFAULT_INTERVAL = "15m"
SIGNAL_INTERVAL_MS = 15 * 60 * 1000


class ExchangeError(RuntimeError):
    """Sanitized market-data error without request URLs or credentials."""


@dataclass(frozen=True)
class Kline:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


class BinanceMarketDataClient:
    """Fetch public prices and candles; this client cannot submit orders."""

    def _request(self, path: str, params: dict[str, str]) -> Any:
        query = urlencode(params)
        request = Request(
            f"{BINANCE_SPOT_BASE_URL}{path}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "binance-spot-paper-bot",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=10) as response:
                body = response.read()
        except HTTPError as exc:
            raise ExchangeError(f"Binance Spot API returned HTTP {exc.code}.") from None
        except (URLError, TimeoutError, OSError):
            raise ExchangeError("Could not reach Binance Spot API.") from None

        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ExchangeError("Binance Spot API returned an unreadable response.") from None

    def get_symbol_info(self, symbol: str) -> dict[str, Any]:
        response = self._request(
            "/api/v3/exchangeInfo",
            {"symbol": symbol},
        )
        try:
            symbols = response["symbols"]
            if not symbols:
                raise ExchangeError(
                    f"{symbol} is not listed on Binance Spot."
                )
            info = symbols[0]
            price_filter = next(
                item
                for item in info["filters"]
                if item.get("filterType") == "PRICE_FILTER"
            )
            tick_size = float(price_filter["tickSize"])
            if not math.isfinite(tick_size) or tick_size <= 0:
                raise ValueError("Invalid tick size.")
            return {
                "status": str(info["status"]),
                "quote_asset": str(info["quoteAsset"]),
                "tick_size": tick_size,
            }
        except ExchangeError:
            raise
        except (KeyError, IndexError, StopIteration, TypeError, ValueError):
            raise ExchangeError(
                f"Binance returned invalid exchange information for {symbol}."
            ) from None

    def get_klines(
        self,
        symbol: str,
        limit: int = 500,
        interval: str = DEFAULT_INTERVAL,
    ) -> list[Kline]:
        if not 1 <= limit <= 1000:
            raise ValueError("Kline limit must be between 1 and 1000.")
        if not interval.strip():
            raise ValueError("Kline interval cannot be empty.")
        response = self._request(
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": str(limit)},
        )
        if not isinstance(response, list):
            raise ExchangeError("Binance returned an invalid candle response.")

        candles: list[Kline] = []
        try:
            for row in response:
                if not isinstance(row, list) or len(row) < 7:
                    raise ValueError("Incomplete candle.")
                values = [float(row[index]) for index in (1, 2, 3, 4, 5)]
                if not all(math.isfinite(value) for value in values):
                    raise ValueError("Non-finite candle value.")
                open_price, high, low, close, volume = values
                if min(open_price, high, low, close) <= 0 or volume < 0:
                    raise ValueError("Invalid candle value.")
                candles.append(
                    Kline(
                        open_time=int(row[0]),
                        open=open_price,
                        high=high,
                        low=low,
                        close=close,
                        volume=volume,
                        close_time=int(row[6]),
                    )
                )
        except (IndexError, TypeError, ValueError, OverflowError):
            raise ExchangeError("Binance returned malformed candle data.") from None

        return sorted(candles, key=lambda candle: candle.open_time)

    def get_price(self, symbol: str) -> float:
        response = self._request("/api/v3/ticker/price", {"symbol": symbol})
        try:
            price = float(response["price"])
        except (KeyError, TypeError, ValueError):
            raise ExchangeError("Could not read the Binance market price.") from None
        if not math.isfinite(price) or price <= 0:
            raise ExchangeError("Binance returned an invalid market price.")
        return price
