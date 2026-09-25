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
    """One transfer line supplied by a transfer command."""

    lot: str
    quantity: int


@dataclass(frozen=True)
class TransferLineRecord:
    """One persisted transfer line."""

    lot: str
    shipped_quantity: int
    received_quantity: int | None
    diff_quantity: int | None


@dataclass(frozen=True)
class TransferRecord:
    """One persisted transfer order."""

    transfer_no: str
    from_warehouse: str
    to_warehouse: str
    product: str
    status: str


@dataclass(frozen=True)
class ReceiptLine:
    """One confirmed receipt line used to close a transfer."""

    lot: str
    received_quantity: int
    diff_quantity: int


TRANSFER_STATUS_SHIPPED = "shipped"
TRANSFER_STATUS_RECEIVED = "received"
TRANSFER_STATUS_RECEIVED_WITH_DIFF = "received-with-diff"

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
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY,
    transfer_no TEXT NOT NULL UNIQUE,
    from_warehouse TEXT NOT NULL,
    to_warehouse TEXT NOT NULL,
    product TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transfer_lines (
    id INTEGER PRIMARY KEY,
    transfer_no TEXT NOT NULL REFERENCES transfers (transfer_no),
    lot TEXT NOT NULL,
    shipped_quantity INTEGER NOT NULL CHECK (shipped_quantity > 0),
    received_quantity INTEGER CHECK (received_quantity >= 0),
    diff_quantity INTEGER CHECK (diff_quantity >= 0),
    UNIQUE (transfer_no, lot)
);
"""


class Ledger:
    """Thin data-access wrapper around a sqlite3 connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, path: str | Path) -> "Ledger":
        conn = sqlite3.connect(str(path))
        conn.executescript(_SCHEMA)
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

    def get_batch(
        self, warehouse: str, product: str, lot: str
    ) -> BatchRecord | None:
        row = self._conn.execute(
            "SELECT lot, production_date, expiry_date, quantity "
            "FROM stock_batches WHERE warehouse = ? AND product = ? AND lot = ?",
            (warehouse, product, lot),
        ).fetchone()
        return BatchRecord(*row) if row is not None else None

    def get_transfer(self, transfer_no: str) -> TransferRecord | None:
        row = self._conn.execute(
            "SELECT transfer_no, from_warehouse, to_warehouse, product, status "
            "FROM transfers WHERE transfer_no = ?",
            (transfer_no,),
        ).fetchone()
        return TransferRecord(*row) if row is not None else None

    def list_transfer_lines(self, transfer_no: str) -> list[TransferLineRecord]:
        rows = self._conn.execute(
            "SELECT lot, shipped_quantity, received_quantity, diff_quantity "
            "FROM transfer_lines WHERE transfer_no = ? ORDER BY id",
            (transfer_no,),
        )
        return [TransferLineRecord(*row) for row in rows]

    def _adjust_batch(
        self,
        warehouse: str,
        product: str,
        lot: str,
        delta: int,
        *,
        production_date: str | None = None,
        expiry_date: str | None = None,
    ) -> None:
        """Apply *delta* to one batch row, creating or deleting it as needed."""
        row = self._conn.execute(
            "SELECT quantity FROM stock_batches "
            "WHERE warehouse = ? AND product = ? AND lot = ?",
            (warehouse, product, lot),
        ).fetchone()
        new_quantity = (row[0] if row is not None else 0) + delta
        if row is not None and new_quantity <= 0:
            self._conn.execute(
                "DELETE FROM stock_batches "
                "WHERE warehouse = ? AND product = ? AND lot = ?",
                (warehouse, product, lot),
            )
        elif row is not None:
            self._conn.execute(
                "UPDATE stock_batches SET quantity = ? "
                "WHERE warehouse = ? AND product = ? AND lot = ?",
                (new_quantity, warehouse, product, lot),
            )
        elif new_quantity > 0:
            self._conn.execute(
                "INSERT INTO stock_batches "
                "(warehouse, product, lot, production_date, expiry_date, quantity) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (warehouse, product, lot, production_date, expiry_date,
                 new_quantity),
            )

    def create_transfer(
        self,
        transfer_no: str,
        from_warehouse: str,
        to_warehouse: str,
        product: str,
        lines: Iterable[TransferLineInput],
    ) -> None:
        """Book a whole transfer atomically (status ``shipped``).

        The source batches are deducted and the destination batches credited
        immediately; destination rows keep the source batch dates.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO transfers "
                "(transfer_no, from_warehouse, to_warehouse, product, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (transfer_no, from_warehouse, to_warehouse, product,
                 TRANSFER_STATUS_SHIPPED),
            )
            for line in lines:
                self._conn.execute(
                    "INSERT INTO transfer_lines "
                    "(transfer_no, lot, shipped_quantity) VALUES (?, ?, ?)",
                    (transfer_no, line.lot, line.quantity),
                )
                source = self.get_batch(from_warehouse, product, line.lot)
                self._adjust_batch(
                    from_warehouse, product, line.lot, -line.quantity
                )
                self._adjust_batch(
                    to_warehouse,
                    product,
                    line.lot,
                    line.quantity,
                    production_date=source.production_date,
                    expiry_date=source.expiry_date,
                )

    def confirm_transfer(
        self,
        transfer_no: str,
        status: str,
        receipts: Iterable[ReceiptLine],
    ) -> None:
        """Close a transfer with its receipt result, atomically.

        Destination stock was credited with the shipped quantities at submit
        time, so any difference between shipped and received is deducted from
        the destination again; the difference itself stays recorded on the
        transfer lines for later handling.
        """
        transfer = self.get_transfer(transfer_no)
        with self._conn:
            self._conn.execute(
                "UPDATE transfers SET status = ? WHERE transfer_no = ?",
                (status, transfer_no),
            )
            for line in receipts:
                self._conn.execute(
                    "UPDATE transfer_lines "
                    "SET received_quantity = ?, diff_quantity = ? "
                    "WHERE transfer_no = ? AND lot = ?",
                    (line.received_quantity, line.diff_quantity,
                     transfer_no, line.lot),
                )
                if line.diff_quantity:
                    self._adjust_batch(
                        transfer.to_warehouse,
                        transfer.product,
                        line.lot,
                        -line.diff_quantity,
                    )
