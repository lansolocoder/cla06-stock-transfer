"""SQLite-backed local ledger for stock batches (stdlib only)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_FILENAME = "stock_ledger.db"

TRANSFER_IN_TRANSIT = "in_transit"
TRANSFER_RECEIVED = "received"
TRANSFER_CANCELLED = "cancelled"
TRANSFER_STATUSES = (
    TRANSFER_IN_TRANSIT,
    TRANSFER_RECEIVED,
    TRANSFER_CANCELLED,
)


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
class TransferItemInput:
    """One ``批次号,调出数量`` allocation line on a transfer order."""

    lot: str
    quantity: int


@dataclass(frozen=True)
class TransferItemRecord:
    """One persisted batch allocation row on a transfer order."""

    lot: str
    quantity: int


@dataclass(frozen=True)
class TransferRecord:
    """One persisted transfer order with its batch allocation lines."""

    order_no: str
    source_warehouse: str
    target_warehouse: str
    product: str
    status: str
    received_quantity: int
    items: tuple[TransferItemRecord, ...]


class TransferError(Exception):
    """Raised when a transfer submission violates a ledger rule."""


_BATCHES_TABLE = """
CREATE TABLE IF NOT EXISTS stock_batches (
    id INTEGER PRIMARY KEY,
    warehouse TEXT NOT NULL,
    product TEXT NOT NULL,
    lot TEXT NOT NULL,
    production_date TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    UNIQUE (warehouse, product, lot)
);
"""

_TRANSFERS_TABLE = """
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY,
    order_no TEXT NOT NULL UNIQUE,
    source_warehouse TEXT NOT NULL,
    target_warehouse TEXT NOT NULL,
    product TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('in_transit', 'received', 'cancelled')),
    received_quantity INTEGER NOT NULL DEFAULT 0
        CHECK (received_quantity >= 0)
);
"""

_TRANSFER_ITEMS_TABLE = """
CREATE TABLE IF NOT EXISTS transfer_items (
    id INTEGER PRIMARY KEY,
    transfer_id INTEGER NOT NULL REFERENCES transfers(id),
    lot TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0)
);
"""

_SCHEMA = "\n".join(
    [_BATCHES_TABLE, _TRANSFERS_TABLE, _TRANSFER_ITEMS_TABLE]
)


def _migrate_quantity_check(conn: sqlite3.Connection) -> None:
    """Rebuild stock_batches when an old schema forbids zero quantities.

    A transfer may draw down a batch to zero, so the original
    ``CHECK (quantity > 0)`` constraint has to become ``>= 0``.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'stock_batches'"
    ).fetchone()
    if row is None or row[0] is None or "quantity > 0" not in row[0]:
        return
    with conn:
        conn.execute("ALTER TABLE stock_batches RENAME TO stock_batches_old")
        conn.execute(_BATCHES_TABLE)
        conn.execute(
            "INSERT INTO stock_batches "
            "(id, warehouse, product, lot, production_date, expiry_date, "
            "quantity) "
            "SELECT id, warehouse, product, lot, production_date, "
            "expiry_date, quantity FROM stock_batches_old"
        )
        conn.execute("DROP TABLE stock_batches_old")


class Ledger:
    """Thin data-access wrapper around a sqlite3 connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, path: str | Path) -> "Ledger":
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        _migrate_quantity_check(conn)
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

    def transfer_exists(self, order_no: str) -> bool:
        """Return whether a transfer order with *order_no* already exists."""
        row = self._conn.execute(
            "SELECT 1 FROM transfers WHERE order_no = ?", (order_no,)
        ).fetchone()
        return row is not None

    def lot_quantities(
        self, warehouse: str, product: str, lots: Sequence[str]
    ) -> dict[str, int]:
        """Return current on-hand quantities for the given booked lots."""
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
        items: Sequence[TransferItemInput],
    ) -> None:
        """Book one transfer order and draw down source lots atomically.

        The target warehouse is not credited: the order stays
        ``in_transit`` with received quantity 0 until a later receipt.
        Raises ``TransferError`` on a missing/insufficient lot and
        ``sqlite3.IntegrityError`` on a duplicate order number; either
        way the whole transaction is rolled back.
        """
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO transfers "
                "(order_no, source_warehouse, target_warehouse, product, "
                "status, received_quantity) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (order_no, source, target, product, TRANSFER_IN_TRANSIT),
            )
            transfer_id = cursor.lastrowid
            self._conn.executemany(
                "INSERT INTO transfer_items (transfer_id, lot, quantity) "
                "VALUES (?, ?, ?)",
                [(transfer_id, item.lot, item.quantity) for item in items],
            )
            for item in items:
                cursor = self._conn.execute(
                    "UPDATE stock_batches SET quantity = quantity - ? "
                    "WHERE warehouse = ? AND product = ? AND lot = ? "
                    "AND quantity >= ?",
                    (item.quantity, source, product, item.lot, item.quantity),
                )
                if cursor.rowcount != 1:
                    row = self._conn.execute(
                        "SELECT quantity FROM stock_batches "
                        "WHERE warehouse = ? AND product = ? AND lot = ?",
                        (source, product, item.lot),
                    ).fetchone()
                    if row is None:
                        raise TransferError(
                            f"批次号 {item.lot} 未在来源仓 {source} "
                            f"商品 {product} 下落账"
                        )
                    raise TransferError(
                        f"批次号 {item.lot} 调出数量 {item.quantity} "
                        f"超过现存数量 {row[0]}"
                    )

    def get_transfer(self, order_no: str) -> TransferRecord | None:
        """Return the order and its allocation lines, or None if absent."""
        row = self._conn.execute(
            "SELECT id, order_no, source_warehouse, target_warehouse, "
            "product, status, received_quantity "
            "FROM transfers WHERE order_no = ?",
            (order_no,),
        ).fetchone()
        if row is None:
            return None
        transfer_id, order_no, source, target, product, status, received = row
        item_rows = self._conn.execute(
            "SELECT lot, quantity FROM transfer_items "
            "WHERE transfer_id = ? ORDER BY id",
            (transfer_id,),
        )
        items = tuple(TransferItemRecord(*item_row) for item_row in item_rows)
        return TransferRecord(
            order_no, source, target, product, status, received, items
        )
