"""Run the paper-only Binance Spot market-data scanner."""

from __future__ import annotations

import logging
import os
import threading
import time

from config import Config
from engine import BotEngine
from exchange import BinanceMarketDataClient
from history import PostgresHistoryStore
from scanner import SignalScanner
from server import serve
from telegram_bot import TelegramBot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = Config.from_environment()
        history_file = config.history_file or (
            config.state_file.parent / "trading_history.csv"
        )
        history = PostgresHistoryStore(
            os.environ.get("DATABASE_URL"),
            history_file,
        )
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None

    market_data = BinanceMarketDataClient()
    engine = BotEngine(config, market_data, history)
    scanner = SignalScanner(config, market_data, engine)
    telegram = TelegramBot.from_environment(
        engine,
        config.telegram_offset_file
        or (config.state_file.parent / "telegram_offset.txt"),
    )
    if telegram is not None:
        engine.set_event_handler(telegram.notify_trade)
        telegram_worker = threading.Thread(
            target=telegram.run_forever,
            name="telegram-bot-polling",
            daemon=True,
        )
        telegram_worker.start()

    logging.info(
        "Paper-only bot started. Binance public Spot market data; "
        "%s Confirm-only WAIT-flow signals with fixed 15m HTF confirmation; "
        "fixed +3%% target / -2%% stop; no exchange orders are submitted.",
        config.timeframe,
    )
    logging.info(
        "Watchlist: %s | position cap: %s | budget: %s USDT total, %s USDT each.",
        ", ".join(config.watchlist),
        config.max_positions,
        config.total_budget,
        config.quote_amount,
    )
    logging.info(
        "Health status: http://%s:%s/healthz",
        config.host,
        config.port,
    )
    engine.print_report()

    def run_worker() -> None:
        while True:
            if engine.is_enabled():
                scanner.scan_once()
            engine.check_positions_once()
            time.sleep(config.poll_seconds)

    worker = threading.Thread(
        target=run_worker,
        name="testnet-configured-timeframe-scanner",
        daemon=True,
    )
    worker.start()

    try:
        serve(engine, config.host, config.port)
    except OSError as exc:
        raise SystemExit(
            f"Could not start the status server ({type(exc).__name__}). "
            "Check that the configured port is available."
        ) from None


if __name__ == "__main__":
    main()