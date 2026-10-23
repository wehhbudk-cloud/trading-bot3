"""Safe, paper-only settings for the Binance Spot market-data scanner."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_WATCHLIST = (
    "SOLUSDT",
    "FETUSDT",
    "MAVUSDT",
    "DASHUSDT",
    "SUIUSDT",
    "ZENUSDT",
    "ZECUSDT",
)
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{3,20}USDT$")
ALLOWED_LOCAL_SETTINGS = {
    "WATCHLIST",
    "TRADE_QUOTE_AMOUNT",
    "MAX_TOTAL_EXPOSURE",
    "MAX_OPEN_POSITIONS",
    "BOT_HOST",
    "BOT_PORT",
    "PORT",
    "SCAN_POLL_SECONDS",
    "TIMEFRAME",
    "HTF_TIMEFRAME",
}


def load_local_env() -> None:
    env_file = PROJECT_DIR / ".env"
    if not env_file.exists():
        return

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key in ALLOWED_LOCAL_SETTINGS:
            os.environ.setdefault(key, value)


def _decimal_setting(name: str, default: str) -> Decimal:
    try:
        value = Decimal(os.environ.get(name, default).strip())
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a number.") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


@dataclass(frozen=True)
class Config:
    watchlist: tuple[str, ...]
    quote_amount: Decimal
    total_budget: Decimal
    max_positions: int
    host: str
    port: int
    poll_seconds: int
    state_file: Path
    history_file: Path | None = None
    telegram_offset_file: Path | None = None
    timeframe: str = "5m"
    htf_timeframe: str = "15m"

    @classmethod
    def from_environment(cls) -> "Config":
        load_local_env()

        raw_symbols = os.environ.get(
            "WATCHLIST", ",".join(DEFAULT_WATCHLIST)
        )
        symbols = tuple(
            symbol.strip().upper()
            for symbol in raw_symbols.split(",")
            if symbol.strip()
        )
        if not symbols:
            raise ValueError("WATCHLIST must contain at least one USDT pair.")

        quote_amount = _decimal_setting("TRADE_QUOTE_AMOUNT", "10")
        total_budget = _decimal_setting("MAX_TOTAL_EXPOSURE", "30")

        try:
            max_positions = int(os.environ.get("MAX_OPEN_POSITIONS", "3"))
        except ValueError as exc:
            raise ValueError("MAX_OPEN_POSITIONS must be a whole number.") from exc

        try:
            port = int(os.environ.get("PORT", os.environ.get("BOT_PORT", "8000")))
        except ValueError as exc:
            raise ValueError("BOT_PORT must be a valid port number.") from exc

        try:
            poll_seconds = int(os.environ.get("SCAN_POLL_SECONDS", "5"))
        except ValueError as exc:
            raise ValueError("SCAN_POLL_SECONDS must be a whole number.") from exc

        timeframe = os.environ.get("TIMEFRAME", "5m").strip()
        htf_timeframe = os.environ.get("HTF_TIMEFRAME", "15m").strip()
        host = os.environ.get("BOT_HOST", "0.0.0.0").strip()

        return cls(
            watchlist=symbols,
            quote_amount=quote_amount,
            total_budget=total_budget,
            max_positions=max_positions,
            host=host,
            port=port,
            poll_seconds=poll_seconds,
            state_file=PROJECT_DIR / "data" / "paper_state.json",
            history_file=PROJECT_DIR / "trading_history.csv",
            telegram_offset_file=PROJECT_DIR / "data" / "telegram_offset.txt",
            timeframe=timeframe,
            htf_timeframe=htf_timeframe,
        )