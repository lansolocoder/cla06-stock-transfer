"""SQLite-backed local ledger for stock batches (stdlib only)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_FILENAME = "stock_ledger.db"


@dataclass(frozen=True)
class BatchInput:
    """One batch line supplied by a registration command."""

    lot: str
    production_date: str  # YYYY-MM-DD
    expiry_date: str  # YYYY-MM-DD
    quantity: int


@dataclass(frozen=True)
class BatchRecord(BatchInput):
    """One persisted batch row."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_batches (
    id INTEGER PRIMARY KEY,
    warehouse TEXT NOT NULL,
    product TEXT NOT NULL,
    lot TEXT NOT NULL,
    production_date TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    UNIQUE (warehouse, product, lot)
);
"""


class Ledger:
    """Thin data-access wrapper around a sqlite3 connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, path: str | Path) -> "Ledger":
        conn = sqlite3.connect(str(path))
        conn.execute(_SCHEMA)
        return cls(conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def existing_lots(
        self, warehouse: str, product: str, lots: Sequence[str]
    ) -> set[str]:
        """Return the subset of *lots* already booked for warehouse+product."""
        if not lots:
            return set()
        placeholders = ",".join("?" for _ in lots)
        rows = self._conn.execute(
            f"SELECT lot FROM stock_batches "
            f"WHERE warehouse = ? AND product = ? AND lot IN ({placeholders})",
            (warehouse, product, *lots),
        )
        return {row[0] for row in rows}

    def add_batches(
        self, warehouse: str, product: str, batches: Iterable[BatchInput]
    ) -> None:
        """Insert all batches in a single transaction (all or nothing)."""
        rows = [
            (warehouse, product, b.lot, b.production_date, b.expiry_date, b.quantity)
            for b in batches
        ]
        with self._conn:
            self._conn.executemany(
                "INSERT INTO stock_batches "
                "(warehouse, product, lot, production_date, expiry_date, quantity) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def list_batches(self, warehouse: str, product: str) -> list[BatchRecord]:
        rows = self._conn.execute(
            "SELECT lot, production_date, expiry_date, quantity "
            "FROM stock_batches WHERE warehouse = ? AND product = ? "
            "ORDER BY id",
            (warehouse, product),
        )
        return [BatchRecord(*row) for row in rows]
