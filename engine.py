"""Paper position handling, budget enforcement, and status reporting."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
import unicodedata

from config import Config
from exchange import ExchangeError
from history import CsvHistoryStore


TAKE_PROFIT_RATE = Decimal("0.03")
STOP_LOSS_RATE = Decimal("0.02")
BINANCE_FEE_RATE = Decimal("0.00075")


def current_candle_open_time_ms(
    timeframe: str, now_ms: int | None = None
) -> int:
    """Return the UTC-aligned TradingView candle open time for a Binance interval."""
    units = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
    if len(timeframe) < 2 or timeframe[-1] not in units:
        raise ValueError(f"Unsupported candle timeframe: {timeframe}")
    try:
        interval_ms = int(timeframe[:-1]) * units[timeframe[-1]]
    except ValueError as exc:
        raise ValueError(f"Unsupported candle timeframe: {timeframe}") from exc
    if interval_ms <= 0:
        raise ValueError(f"Unsupported candle timeframe: {timeframe}")
    timestamp = now_ms if now_ms is not None else int(time.time() * 1000)
    return timestamp // interval_ms * interval_ms


def candle_time_utc(timestamp_ms: int) -> str:
    """Format Binance/TradingView epoch milliseconds as an exact UTC timestamp."""
    return (
        datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class BotEngine:
    def __init__(
        self,
        config: Config,
        market_data: Any,
        history_store: Any | None = None,
    ) -> None:
        self.config = config
        self.market_data = market_data
        self._lock = threading.RLock()
        history_path = config.history_file or (
            config.state_file.parent / "trading_history.csv"
        )
        self.history = history_store or CsvHistoryStore(history_path)
        self._positions: dict[str, dict[str, str]] = {}
        self._indicator_states: dict[str, dict[str, Any]] = {}
        self._current_candle_times: dict[str, int] = {}
        self._enabled = True
        self._event_handler: Callable[[dict[str, Any]], None] | None = None
        self._scan_status: dict[str, dict[str, Any]] = {
            symbol: {
                "state": "starting",
                "last_closed_candle": None,
                "last_error": None,
                "last_signal": None,
            }
            for symbol in config.watchlist
        }
        self._price_errors: dict[str, str] = {}
        self._load_state()
        self._restore_positions_from_history()

    @staticmethod
    def candle_time_utc(timestamp_ms: int) -> str:
        return candle_time_utc(timestamp_ms)

    def set_event_handler(
        self, handler: Callable[[dict[str, Any]], None] | None
    ) -> None:
        self._event_handler = handler

    def _emit_event(self, event: dict[str, Any]) -> None:
        if self._event_handler is None:
            return
        try:
            self._event_handler(event)
        except Exception as exc:
            logging.warning(
                "Trade alert could not be queued (%s).",
                type(exc).__name__,
            )

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._save_state()

    def set_current_candle_time(self, symbol: str, open_time_ms: int) -> None:
        with self._lock:
            self._current_candle_times[symbol] = int(open_time_ms)

    def handle_signal(
        self,
        symbol: str,
        action: str,
        signal_price: float,
        *,
        candle_open_time_ms: int | None = None,
    ) -> dict[str, Any]:
        if action not in {"buy", "sell"}:
            raise ValueError("Only buy and sell signals are supported.")
        try:
            price = Decimal(str(signal_price))
        except InvalidOperation as exc:
            raise ValueError("Signal price must be numeric.") from exc
        if not price.is_finite() or price <= 0:
            raise ValueError("Signal price must be greater than zero.")
        candle_time_ms = (
            candle_open_time_ms
            if candle_open_time_ms is not None
            else current_candle_open_time_ms(self.config.timeframe)
        )

        with self._lock:
            # تجاهل إشارة البيع الصادرة من انعكاس المؤشر فقط؛ الخروج يتم بالهدق أو الستوب حصراً
            if action == "sell":
                return {"status": "ignored", "reason": "tp_sl_exit_only"}

            if symbol in self._positions:
                return {"status": "ignored", "reason": "position_already_open"}
            if len(self._positions) >= self.config.max_positions:
                return {"status": "ignored", "reason": "position_limit_reached"}

            exposure = self._current_exposure()
            if exposure + self.config.quote_amount > self.config.total_budget:
                return {"status": "ignored", "reason": "total_budget_reached"}

            quantity = self.config.quote_amount / price
            trade_id = uuid.uuid4().hex
            opened_at_utc = candle_time_utc(candle_time_ms)
            target_price = price * (Decimal("1") + TAKE_PROFIT_RATE)
            stop_price = price * (Decimal("1") - STOP_LOSS_RATE)
            position = {
                "trade_id": trade_id,
                "entry_price": str(price),
                "quantity": str(quantity),
                "quote_amount": str(self.config.quote_amount),
                "target_price": str(target_price),
                "stop_price": str(stop_price),
                "entry_candle_time_ms": str(candle_time_ms),
                "opened_at_utc": opened_at_utc,
                "opened_at": opened_at_utc,
            }
            self.history.add_trade(
                {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "opened_at_utc": opened_at_utc,
                    "entry_candle_time_ms": candle_time_ms,
                    "entry_price": price,
                    "quantity": quantity,
                    "quote_amount": self.config.quote_amount,
                    "target_price": target_price,
                    "stop_price": stop_price,
                    "status": "ACTIVE",
                }
            )
            self._positions[symbol] = position
            self._save_state()
            logging.info(
                "Paper entry %s at %s | target %s | stop %s | candle %s.",
                symbol,
                price,
                target_price,
                stop_price,
                opened_at_utc,
            )
            self._emit_event(
                {
                    "type": "entry",
                    "symbol": symbol,
                    "entry_price": str(price),
                    "target_price": str(target_price),
                    "stop_price": str(stop_price),
                    "quantity": str(quantity),
                    "candle_time_utc": opened_at_utc,
                    "trade_id": trade_id,
                }
            )
            self.print_report()
            return {
                "status": "opened",
                "symbol": symbol,
                "entry_price": str(price),
                "target_price": str(target_price),
                "stop_price": str(stop_price),
                "quote_amount": str(self.config.quote_amount),
                "quantity": str(quantity),
                "trade_id": trade_id,
                "candle_time_utc": opened_at_utc,
            }

    def check_positions_once(
        self, *, candle_open_time_ms: int | None = None
    ) -> None:
        with self._lock:
            positions = {
                symbol: dict(position)
                for symbol, position in self._positions.items()
            }

        for symbol, position in positions.items():
            try:
                with self._lock:
                    candle_time_ms = (
                        candle_open_time_ms
                        if candle_open_time_ms is not None
                        else self._current_candle_times.get(symbol)
                    )
                if candle_time_ms is None:
                    candle_time_ms = current_candle_open_time_ms(
                        self.config.timeframe
                    )
                price = Decimal(str(self.market_data.get_price(symbol)))
                if not price.is_finite() or price <= 0:
                    raise ExchangeError("Binance returned an invalid market price.")
                target = Decimal(position["target_price"])
                stop = Decimal(position["stop_price"])
                if price >= target:
                    self._close_by_risk_rule(
                        symbol, "take_profit", target, candle_time_ms
                    )
                elif price <= stop:
                    self._close_by_risk_rule(
                        symbol, "stop_loss", stop, candle_time_ms
                    )
                if symbol in self._price_errors:
                    logging.info("Market price feed recovered for %s.", symbol)
                    self._price_errors.pop(symbol, None)
            except Exception as exc:
                error = (
                    str(exc)
                    if isinstance(exc, ExchangeError)
                    else f"Price check failed ({type(exc).__name__})."
                )
                if self._price_errors.get(symbol) != error:
                    logging.warning("Could not check %s paper position: %s", symbol, error)
                    self._price_errors[symbol] = error

    def _close_by_risk_rule(
        self,
        symbol: str,
        reason: str,
        exit_price: Decimal,
        candle_time_ms: int,
    ) -> None:
        self._close_position(symbol, reason, exit_price, candle_time_ms)

    def _close_position(
        self,
        symbol: str,
        reason: str,
        exit_price: Decimal,
        candle_time_ms: int,
    ) -> dict[str, Any]:
        with self._lock:
            position = self._positions.get(symbol)
            if position is None:
                return {"status": "ignored", "reason": "no_open_position"}
            entry = Decimal(position["entry_price"])
            quantity = Decimal(position["quantity"])
            entry_notional = entry * quantity
            exit_notional = exit_price * quantity
            gross_pnl = (exit_price - entry) * quantity
            fees = (entry_notional + exit_notional) * BINANCE_FEE_RATE
            net_pnl = gross_pnl - fees
            net_pnl_percent = (
                net_pnl / entry_notional * Decimal("100")
                if entry_notional
                else Decimal("0")
            )
            closed_at_utc = candle_time_utc(candle_time_ms)
            updates = {
                "status": "END",
                "closed_at_utc": closed_at_utc,
                "exit_candle_time_ms": candle_time_ms,
                "exit_price": str(exit_price),
                "close_reason": reason,
                "gross_pnl_usd": str(gross_pnl),
                "binance_fees_usd": str(fees),
                "net_pnl_usd": str(net_pnl),
                "net_pnl_percent": str(net_pnl_percent),
            }
            updated = self.history.close_trade(position["trade_id"], updates)
            if not updated:
                stored_trade = next(
                    (
                        row
                        for row in self.history.all_trades()
                        if row["trade_id"] == position["trade_id"]
                        and row["status"] == "END"
                    ),
                    None,
                )
                if stored_trade is None:
                    raise RuntimeError(
                        "The open trade was missing from persistent history."
                    )
                updates = stored_trade
                exit_price = Decimal(stored_trade["exit_price"])
                gross_pnl = Decimal(stored_trade["gross_pnl_usd"])
                fees = Decimal(stored_trade["binance_fees_usd"])
                net_pnl = Decimal(stored_trade["net_pnl_usd"])
                net_pnl_percent = Decimal(stored_trade["net_pnl_percent"])
                closed_at_utc = stored_trade["closed_at_utc"]
            self._positions.pop(symbol, None)
            self._save_state()
        if reason == "take_profit":
            logging.info(
                "[🎯] TARGET HUNTER! %s reached its fixed +3%% target.",
                symbol,
            )
        elif reason == "stop_loss":
            logging.warning(
                "[⚠️] STOP LOSS HUNTED! %s reached its fixed -2%% stop.",
                symbol,
            )
        logging.info(
            "Closed paper trade %s | Entry Price: %s | Exit Price: %s | "
            "Gross Profit/Loss: %s USDT | Binance Trading Fees (0.075%% each "
            "side): %s USDT | Net Profit/Loss: %s USDT (%s%%) | "
            "TradingView candle UTC: %s.",
            symbol,
            entry,
            exit_price,
            gross_pnl,
            fees,
            net_pnl,
            net_pnl_percent,
            closed_at_utc,
        )
        event_type = {
            "take_profit": "target_hunter",
            "stop_loss": "stop_loss_hunted",
        }.get(reason, "trade_closed")
        self._emit_event(
            {
                "type": event_type,
                "symbol": symbol,
                "entry_price": str(entry),
                "exit_price": str(exit_price),
                "target_price": position["target_price"],
                "stop_price": position["stop_price"],
                "gross_pnl_usd": str(gross_pnl),
                "binance_fees_usd": str(fees),
                "net_pnl_usd": str(net_pnl),
                "net_pnl_percent": str(net_pnl_percent),
                "reason": reason,
                "candle_time_utc": closed_at_utc,
            }
        )
        self.print_report()
        return {
            "status": "closed",
            "symbol": symbol,
            "reason": reason,
            "exit_price": str(exit_price),
            "gross_pnl_usd": str(gross_pnl),
            "binance_fees_usd": str(fees),
            "net_pnl_usd": str(net_pnl),
            "net_pnl_percent": str(net_pnl_percent),
            "candle_time_utc": closed_at_utc,
        }

    def get_indicator_state(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._indicator_states.get(symbol)
            return json.loads(json.dumps(value)) if value is not None else None

    def save_indicator_state(
        self, symbol: str, state: dict[str, Any]
    ) -> None:
        with self._lock:
            self._indicator_states[symbol] = state
            self._save_state()

    def record_scan(
        self,
        symbol: str,
        *,
        state: str | None = None,
        last_closed_candle: int | None = None,
        error: str | None = None,
        last_signal: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            current = self._scan_status.setdefault(
                symbol,
                {
                    "state": "starting",
                    "last_closed_candle": None,
                    "last_error": None,
                    "last_signal": None,
                },
            )
            if state is not None:
                current["state"] = state
            if last_closed_candle is not None:
                current["last_closed_candle"] = last_closed_candle
            current["last_error"] = error
            if last_signal is not None:
                current["last_signal"] = last_signal

    def status(self) -> dict[str, Any]:
        with self._lock:
            exposure = self._current_exposure()
            trades = self.history.all_trades()
            summary = self.history_summary(trades)
            return {
                "mode": "paper",
                "enabled": self._enabled,
                "testnet_market_data": True,
                "order_submission_enabled": False,
                "timeframe": self.config.timeframe,
                "htf_timeframe": self.config.htf_timeframe,
                "watchlist": list(self.config.watchlist),
                "open_positions": len(self._positions),
                "max_open_positions": self.config.max_positions,
                "position_budget_usdt": str(self.config.quote_amount),
                "total_budget_usdt": str(self.config.total_budget),
                "current_exposure_usdt": str(exposure),
                "take_profit_percent": 3,
                "stop_loss_percent": 2,
                **summary,
                "positions": [
                    {
                        "symbol": symbol,
                        "entry_price": position["entry_price"],
                        "target_price": position["target_price"],
                        "stop_price": position["stop_price"],
                        "quantity": position["quantity"],
                        "quote_amount": position["quote_amount"],
                        "opened_at_utc": position["opened_at_utc"],
                        "entry_candle_time_ms": position[
                            "entry_candle_time_ms"
                        ],
                    }
                    for symbol, position in sorted(self._positions.items())
                ],
                "scans": {
                    symbol: {
                        **scan,
                        "last_closed_candle_utc": (
                            candle_time_utc(scan["last_closed_candle"])
                            if scan.get("last_closed_candle") is not None
                            else None
                        ),
                    }
                    for symbol, scan in sorted(self._scan_status.items())
                },
            }

    def history_summary(
        self, trades: list[dict[str, str]] | None = None
    ) -> dict[str, str | int]:
        rows = trades if trades is not None else self.history.all_trades()
        closed = [row for row in rows if row["status"] == "END"]
        wins = sum(
            Decimal(row["net_pnl_usd"]) > 0 for row in closed
        )
        losses = sum(
            Decimal(row["net_pnl_usd"]) < 0 for row in closed
        )
        net_total = sum(
            (Decimal(row["net_pnl_usd"]) for row in closed),
            start=Decimal("0"),
        )
        closed_notional = sum(
            (Decimal(row["quote_amount"]) for row in closed),
            start=Decimal("0"),
        )
        cumulative_percent = (
            net_total / closed_notional * Decimal("100")
            if closed_notional
            else Decimal("0")
        )
        return {
            "total_trades_opened": len(rows),
            "successful_trades": wins,
            "failed_trades": losses,
            "net_cumulative_profit_usd": str(net_total),
            "net_cumulative_profit_percent": str(cumulative_percent),
        }

    @staticmethod
    def _display_width(value: str) -> int:
        return sum(
            2
            if unicodedata.east_asian_width(character) in {"W", "F"}
            else 1
            for character in value
        )

    @staticmethod
    def _display_price(value: str | Decimal) -> str:
        number = Decimal(str(value))
        return f"{number:,.8f}".rstrip("0").rstrip(".")

    @staticmethod
    def _display_pnl(value: str, percent: str) -> str:
        amount = Decimal(value)
        sign = "+" if amount >= 0 else "-"
        return f"${sign}{abs(amount):.6f} ({Decimal(percent):+.2f}%)"

    def format_status_table(self) -> str:
        headers = (
            "Symbol",
            "Entry Price",
            "Target (+3%)",
            "Stop Loss (-2%)",
            "Status",
            "Net PnL",
        )
        rows: list[tuple[str, ...]] = []
        for trade in self.history.all_trades():
            active = trade["status"] == "ACTIVE"
            rows.append(
                (
                    trade["symbol"],
                    self._display_price(trade["entry_price"]),
                    self._display_price(trade["target_price"]),
                    self._display_price(trade["stop_price"]),
                    "🟢 ACTIVE" if active else "🔴 END",
                    (
                        "—"
                        if active
                        else self._display_pnl(
                            trade["net_pnl_usd"], trade["net_pnl_percent"]
                        )
                    ),
                )
            )
        widths = [
            max(
                [self._display_width(headers[index])]
                + [self._display_width(row[index]) for row in rows]
            )
            for index in range(len(headers))
        ]

        def format_row(values: tuple[str, ...]) -> str:
            cells = [
                value + " " * (widths[index] - self._display_width(value))
                for index, value in enumerate(values)
            ]
            return "| " + " | ".join(cells) + " |"

        border = "+-" + "-+-".join("-" * width for width in widths) + "-+"
        return "\n".join(
            [border, format_row(headers), border]
            + [format_row(row) for row in rows]
            + [border]
        )

    def format_history_summary(self) -> str:
        summary = self.history_summary()
        return (
            "Trade history summary | "
            f"Total Trades Opened: {summary['total_trades_opened']} | "
            f"Successful Trades (Wins): {summary['successful_trades']} | "
            f"Failed Trades (Losses): {summary['failed_trades']} | "
            "Net Cumulative Profit/Loss: "
            f"${Decimal(str(summary['net_cumulative_profit_usd'])):.6f} "
            f"({Decimal(str(summary['net_cumulative_profit_percent'])):+.2f}%)"
        )

    def print_report(self) -> None:
        logging.info("\n%s", self.format_status_table())
        logging.info("%s", self.format_history_summary())

    def _current_exposure(self) -> Decimal:
        return sum(
            (Decimal(position["quote_amount"]) for position in self._positions.values()),
            start=Decimal("0"),
        )

    def _restore_positions_from_history(self) -> None:
        trades = self.history.all_trades()
        active: dict[str, dict[str, str]] = {}
        for trade in trades:
            if trade["status"] != "ACTIVE":
                continue
            symbol = trade["symbol"]
            if symbol in active:
                raise RuntimeError(
                    f"Persistent history contains multiple open trades for {symbol}."
                )
            active[symbol] = trade

        for symbol, legacy in list(self._positions.items()):
            if symbol in active:
                continue
            entry = Decimal(legacy["entry_price"])
            quantity = Decimal(legacy["quantity"])
            quote_amount = Decimal(legacy["quote_amount"])
            opened_at = legacy.get("opened_at", "")
            try:
                parsed = datetime.fromisoformat(
                    opened_at.replace("Z", "+00:00")
                )
                candle_ms = int(parsed.timestamp() * 1000)
            except (ValueError, TypeError, OverflowError):
                candle_ms = current_candle_open_time_ms(self.config.timeframe)
            trade_id = uuid.uuid4().hex
            target = entry * (Decimal("1") + TAKE_PROFIT_RATE)
            stop = entry * (Decimal("1") - STOP_LOSS_RATE)
            trade = {
                "trade_id": trade_id,
                "symbol": symbol,
                "opened_at_utc": candle_time_utc(candle_ms),
                "entry_candle_time_ms": candle_ms,
                "entry_price": entry,
                "quantity": quantity,
                "quote_amount": quote_amount,
                "target_price": target,
                "stop_price": stop,
                "status": "ACTIVE",
            }
            self.history.add_trade(trade)
            active[symbol] = {
                key: "" if value is None else str(value)
                for key, value in trade.items()
            }

        restored: dict[str, dict[str, str]] = {}
        for symbol, trade in active.items():
            try:
                entry = Decimal(trade["entry_price"])
                quantity = Decimal(trade["quantity"])
                quote_amount = Decimal(trade["quote_amount"])
                target = Decimal(trade["target_price"])
                stop = Decimal(trade["stop_price"])
                candle_ms = int(trade["entry_candle_time_ms"])
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise RuntimeError(
                    f"Persistent history contains an invalid active trade for {symbol}."
                ) from exc
            if any(
                not value.is_finite() or value <= 0
                for value in (entry, quantity, quote_amount, target, stop)
            ):
                raise RuntimeError(
                    f"Persistent history contains invalid prices for {symbol}."
                )
            restored[symbol] = {
                "trade_id": trade["trade_id"],
                "entry_price": str(entry),
                "quantity": str(quantity),
                "quote_amount": str(quote_amount),
                "target_price": str(target),
                "stop_price": str(stop),
                "entry_candle_time_ms": str(candle_ms),
                "opened_at_utc": trade["opened_at_utc"],
                "opened_at": trade["opened_at_utc"],
            }
        if len(restored) > self.config.max_positions:
            raise RuntimeError("Persistent trades exceed the configured position limit.")
        self._positions = restored
        if self._current_exposure() > self.config.total_budget:
            raise RuntimeError("Persistent trades exceed the configured total budget.")
        self._save_state()

    def _load_state(self) -> None:
        path = self.config.state_file
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            enabled = state.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ValueError("Invalid saved run state.")
            self._enabled = enabled
            positions = state.get("positions", {})
            if not isinstance(positions, dict) or len(positions) > 3:
                raise ValueError("Invalid saved positions.")
            restored: dict[str, dict[str, str]] = {}
            for symbol, raw_position in positions.items():
                if not isinstance(symbol, str) or not isinstance(raw_position, dict):
                    raise ValueError("Invalid saved position entry.")
                entry = Decimal(str(raw_position["entry_price"]))
                quantity = Decimal(str(raw_position["quantity"]))
                quote_amount = Decimal(
                    str(raw_position.get("quote_amount", entry * quantity))
                )
                if (
                    not entry.is_finite()
                    or not quantity.is_finite()
                    or not quote_amount.is_finite()
                    or entry <= 0
                    or quantity <= 0
                    or quote_amount <= 0
                ):
                    raise ValueError("Invalid saved position values.")
                restored[symbol] = {
                    "entry_price": str(entry),
                    "quantity": str(quantity),
                    "quote_amount": str(quote_amount),
                    "opened_at": str(raw_position.get("opened_at", "unknown")),
                }
            self._positions = restored
            if self._current_exposure() > self.config.total_budget:
                raise ValueError("Saved positions exceed the current total budget.")

            indicator_states = state.get("indicator_states", {})
            if not isinstance(indicator_states, dict):
                raise ValueError("Invalid saved indicator states.")
            self._indicator_states = {
                str(symbol): value
                for symbol, value in indicator_states.items()
                if isinstance(value, dict)
            }
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "Saved paper state could not be read safely. Stop the bot and "
                "inspect its data file before continuing."
            ) from exc

    def _save_state(self) -> None:
        path: Path = self.config.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(".tmp")
        state = {
            "positions": self._positions,
            "indicator_states": self._indicator_states,
            "enabled": self._enabled,
        }
        temporary_path.write_text(
            json.dumps(state, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary_path.replace(path)