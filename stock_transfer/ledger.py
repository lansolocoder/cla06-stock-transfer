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


@dataclass(frozen=True)
class TransferLineInput:
    """One allocation line supplied by a transfer command."""

    lot: str
    quantity: int


@dataclass(frozen=True)
class TransferLineRecord(TransferLineInput):
    """One persisted transfer allocation line."""


@dataclass(frozen=True)
class TransferRecord:
    """One persisted transfer order with its allocation lines."""

    order_no: str
    source_warehouse: str
    target_warehouse: str
    product: str
    status: str
    received_quantity: int
    lines: tuple[TransferLineRecord, ...]


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

_TRANSFER_SCHEMA = """
CREATE TABLE IF NOT EXISTS transfer_orders (
    id INTEGER PRIMARY KEY,
    order_no TEXT NOT NULL UNIQUE,
    source_warehouse TEXT NOT NULL,
    target_warehouse TEXT NOT NULL,
    product TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('in_transit', 'received', 'cancelled')),
    received_quantity INTEGER NOT NULL CHECK (received_quantity >= 0)
);
CREATE TABLE IF NOT EXISTS transfer_lines (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES transfer_orders (id),
    lot TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    UNIQUE (order_id, lot)
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
        conn.executescript(_TRANSFER_SCHEMA)
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

    def transfer_order_exists(self, order_no: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM transfer_orders WHERE order_no = ?", (order_no,)
        ).fetchone()
        return row is not None

    def batch_quantities(
        self, warehouse: str, product: str, lots: Sequence[str]
    ) -> dict[str, int]:
        """Return current quantity per lot; lots not booked are absent."""
        if not lots:
            return {}
        placeholders = ",".join("?" for _ in lots)
        rows = self._conn.execute(
            f"SELECT lot, quantity FROM stock_batches "
            f"WHERE warehouse = ? AND product = ? AND lot IN ({placeholders})",
            (warehouse, product, *lots),
        )
        return {lot: quantity for lot, quantity in rows}

    def create_transfer(
        self,
        order_no: str,
        source: str,
        target: str,
        product: str,
        lines: Iterable[TransferLineInput],
    ) -> None:
        """Book a transfer order and deduct source stock, all or nothing.

        The target warehouse is not credited; the order starts out as
        ``in_transit`` with received quantity 0. A source batch drawn down
        to zero is removed so the ``quantity > 0`` invariant holds.
        """
        lines = list(lines)
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO transfer_orders "
                "(order_no, source_warehouse, target_warehouse, product, "
                "status, received_quantity) "
                "VALUES (?, ?, ?, ?, 'in_transit', 0)",
                (order_no, source, target, product),
            )
            order_id = cursor.lastrowid
            self._conn.executemany(
                "INSERT INTO transfer_lines (order_id, lot, quantity) "
                "VALUES (?, ?, ?)",
                [(order_id, line.lot, line.quantity) for line in lines],
            )
            for line in lines:
                row = self._conn.execute(
                    "SELECT id, quantity FROM stock_batches "
                    "WHERE warehouse = ? AND product = ? AND lot = ?",
                    (source, product, line.lot),
                ).fetchone()
                if row is None or row[1] < line.quantity:
                    raise sqlite3.IntegrityError(
                        f"批次 {line.lot} 现存数量不足以调出 {line.quantity}"
                    )
                batch_id, remaining = row[0], row[1] - line.quantity
                if remaining == 0:
                    self._conn.execute(
                        "DELETE FROM stock_batches WHERE id = ?", (batch_id,)
                    )
                else:
                    self._conn.execute(
                        "UPDATE stock_batches SET quantity = ? WHERE id = ?",
                        (remaining, batch_id),
                    )

    def get_transfer(self, order_no: str) -> TransferRecord | None:
        row = self._conn.execute(
            "SELECT id, order_no, source_warehouse, target_warehouse, "
            "product, status, received_quantity "
            "FROM transfer_orders WHERE order_no = ?",
            (order_no,),
        ).fetchone()
        if row is None:
            return None
        lines = self._conn.execute(
            "SELECT lot, quantity FROM transfer_lines "
            "WHERE order_id = ? ORDER BY id",
            (row[0],),
        )
        return TransferRecord(
            order_no=row[1],
            source_warehouse=row[2],
            target_warehouse=row[3],
            product=row[4],
            status=row[5],
            received_quantity=row[6],
            lines=tuple(TransferLineRecord(*line) for line in lines),
        )
