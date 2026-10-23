"""CSV trade history with a PostgreSQL-backed durable source for deployments."""

from __future__ import annotations

import csv
import os
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any


HISTORY_COLUMNS = (
    "trade_id",
    "symbol",
    "opened_at_utc",
    "entry_candle_time_ms",
    "entry_price",
    "quantity",
    "quote_amount",
    "target_price",
    "stop_price",
    "status",
    "closed_at_utc",
    "exit_candle_time_ms",
    "exit_price",
    "close_reason",
    "gross_pnl_usd",
    "binance_fees_usd",
    "net_pnl_usd",
    "net_pnl_percent",
)

_SELECT_SQL = (
    "SELECT " + ", ".join(HISTORY_COLUMNS) +
    " FROM paper_trades ORDER BY opened_at_utc, trade_id"
)


def _normalize_row(row: dict[str, Any]) -> dict[str, str]:
    return {
        column: "" if row.get(column) is None else str(row.get(column))
        for column in HISTORY_COLUMNS
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(path.name + ".tmp")
    with temporary_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_COLUMNS)
        writer.writeheader()
        writer.writerows(_normalize_row(row) for row in rows)
        handle.flush()
        os.fsync(handle.fileno())
    temporary_path.replace(path)


class CsvHistoryStore:
    """Local CSV implementation used by tests and standalone development runs."""

    def __init__(self, csv_path: Path) -> None:
        self.csv_path = csv_path
        self._lock = threading.RLock()
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.csv_path.exists() or self.csv_path.stat().st_size == 0:
            _write_csv(self.csv_path, [])
        else:
            self.all_trades()

    def all_trades(self) -> list[dict[str, str]]:
        with self._lock:
            with self.csv_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if tuple(reader.fieldnames or ()) != HISTORY_COLUMNS:
                    raise RuntimeError(
                        "trading_history.csv has an unexpected header; "
                        "preserve a copy and inspect it before continuing."
                    )
                return [_normalize_row(row) for row in reader]

    def add_trade(self, trade: dict[str, Any]) -> None:
        with self._lock:
            rows = self.all_trades()
            if any(row["trade_id"] == str(trade["trade_id"]) for row in rows):
                raise RuntimeError("A trade with this ID already exists in history.")
            rows.append(trade)
            _write_csv(self.csv_path, rows)

    def close_trade(self, trade_id: str, updates: dict[str, Any]) -> bool:
        with self._lock:
            rows = self.all_trades()
            for row in rows:
                if row["trade_id"] == trade_id and row["status"] == "ACTIVE":
                    row.update(_normalize_row({**row, **updates}))
                    _write_csv(self.csv_path, rows)
                    return True
            return False


class PostgresHistoryStore:
    """PostgreSQL is authoritative; trading_history.csv is its durable snapshot."""

    def __init__(self, database_url: str | None, csv_path: Path) -> None:
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL is required for durable trade history on Replit."
            )
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "The PostgreSQL driver is missing. Install the bot requirements "
                "before starting."
            ) from exc
        self._psycopg = psycopg
        self._database_url = database_url
        self.csv_path = csv_path
        self._lock = threading.RLock()
        self.write_csv_snapshot()

    def _query_rows(self) -> list[dict[str, str]]:
        try:
            with self._psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(_SELECT_SQL)
                    results = cursor.fetchall()
        except Exception as exc:
            raise RuntimeError(
                "Could not read durable trade history. Confirm the paper_trades "
                "database table exists and the database is available."
            ) from exc
        return [
            _normalize_row(dict(zip(HISTORY_COLUMNS, result)))
            for result in results
        ]

    def all_trades(self) -> list[dict[str, str]]:
        return self._query_rows()

    def add_trade(self, trade: dict[str, Any]) -> None:
        placeholders = ", ".join(["%s"] * len(HISTORY_COLUMNS))
        columns = ", ".join(HISTORY_COLUMNS)
        numeric_columns = {
            "entry_price",
            "quantity",
            "quote_amount",
            "target_price",
            "stop_price",
        }
        integer_columns = {"entry_candle_time_ms", "exit_candle_time_ms"}
        values = tuple(
            (
                None
                if trade.get(column) in (None, "")
                else Decimal(str(trade[column]))
                if column in numeric_columns
                else int(trade[column])
                if column in integer_columns
                else trade[column]
            )
            for column in HISTORY_COLUMNS
        )
        try:
            with self._psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"INSERT INTO paper_trades ({columns}) "
                        f"VALUES ({placeholders})",
                        values,
                    )
        except Exception as exc:
            raise RuntimeError("Could not persist the new paper trade.") from exc
        self.write_csv_snapshot()

    def close_trade(self, trade_id: str, updates: dict[str, Any]) -> bool:
        columns = (
            "status",
            "closed_at_utc",
            "exit_candle_time_ms",
            "exit_price",
            "close_reason",
            "gross_pnl_usd",
            "binance_fees_usd",
            "net_pnl_usd",
            "net_pnl_percent",
        )
        assignments = ", ".join(f"{column} = %s" for column in columns)
        numeric_columns = {
            "exit_price",
            "gross_pnl_usd",
            "binance_fees_usd",
            "net_pnl_usd",
            "net_pnl_percent",
        }
        values = tuple(
            (
                None
                if updates.get(column) in (None, "")
                else Decimal(str(updates[column]))
                if column in numeric_columns
                else int(updates[column])
                if column == "exit_candle_time_ms"
                else updates[column]
            )
            for column in columns
        )
        try:
            with self._psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"UPDATE paper_trades SET {assignments} "
                        "WHERE trade_id = %s AND status = 'ACTIVE'",
                        (*values, trade_id),
                    )
                    updated = cursor.rowcount > 0
        except Exception as exc:
            raise RuntimeError("Could not persist the closed paper trade.") from exc
        if updated:
            self.write_csv_snapshot()
        return updated

    def write_csv_snapshot(self) -> None:
        with self._lock:
            _write_csv(self.csv_path, self._query_rows())
