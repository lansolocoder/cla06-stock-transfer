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
class ReceivedItemInput:
    """One ``批次号,实收数量`` receipt line on a receive command."""

    lot: str
    quantity: int


@dataclass(frozen=True)
class TransferItemRecord:
    """One persisted batch allocation row on a transfer order."""

    lot: str
    quantity: int
    received_quantity: int = 0


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
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    received_quantity INTEGER NOT NULL DEFAULT 0
        CHECK (received_quantity >= 0)
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


def _migrate_received_quantity_column(conn: sqlite3.Connection) -> None:
    """Add per-lot received quantities to a transfer_items table lacking it."""
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(transfer_items)")
    }
    if not columns or "received_quantity" in columns:
        return
    with conn:
        conn.execute(
            "ALTER TABLE transfer_items "
            "ADD COLUMN received_quantity INTEGER NOT NULL DEFAULT 0 "
            "CHECK (received_quantity >= 0)"
        )


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
        _migrate_received_quantity_column(conn)
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
            "SELECT lot, quantity, received_quantity FROM transfer_items "
            "WHERE transfer_id = ? ORDER BY id",
            (transfer_id,),
        )
        items = tuple(TransferItemRecord(*item_row) for item_row in item_rows)
        return TransferRecord(
            order_no, source, target, product, status, received, items
        )

    def receive_transfer(
        self,
        order_no: str,
        received_items: Sequence[ReceivedItemInput],
    ) -> int:
        """Confirm receipt of one in-transit order and credit the target.

        All lot receipts are applied in a single transaction: the target
        warehouse is credited per lot (lots received with quantity 0
        create no row; an existing same lot accumulates), batch
        production/expiry dates are copied from the source warehouse
        rows, each allocation line records its received quantity, and
        the order becomes ``received`` with the received total.

        Source-warehouse quantities are never touched. Raises
        ``TransferError`` when the order is absent, not ``in_transit``,
        the receipt lines do not cover exactly the order's lots, or a
        received quantity exceeds the shipped quantity; in any such
        case the whole transaction is rolled back.
        """
        with self._conn:
            order_row = self._conn.execute(
                "SELECT id, source_warehouse, target_warehouse, product, "
                "status FROM transfers WHERE order_no = ?",
                (order_no,),
            ).fetchone()
            if order_row is None:
                raise TransferError(f"调拨单号 {order_no} 不存在")
            transfer_id, source, target, product, status = order_row
            if status != TRANSFER_IN_TRANSIT:
                raise TransferError(
                    f"调拨单号 {order_no} 当前状态为 {status}，"
                    "只有在途单据可以收货确认"
                )

            item_rows = self._conn.execute(
                "SELECT lot, quantity FROM transfer_items "
                "WHERE transfer_id = ?",
                (transfer_id,),
            ).fetchall()
            shipped: dict[str, int] = {lot: quantity for lot, quantity in item_rows}
            receipts: dict[str, int] = {}
            for item in received_items:
                if item.lot in receipts:
                    raise TransferError(
                        f"批次号 {item.lot} 在同一次收货内重复"
                    )
                receipts[item.lot] = item.quantity

            if set(receipts) != set(shipped):
                missing = sorted(set(shipped) - set(receipts))
                unknown = sorted(set(receipts) - set(shipped))
                details = []
                if missing:
                    details.append("遗漏批次 " + "、".join(missing))
                if unknown:
                    details.append("单上不存在的批次 " + "、".join(unknown))
                raise TransferError(
                    "实收行必须逐一覆盖调拨单全部批次：" + "；".join(details)
                )
            for lot, quantity in receipts.items():
                if quantity < 0:
                    raise TransferError(
                        f"批次号 {lot} 实收数量 {quantity} 不能为负数"
                    )
                if quantity > shipped[lot]:
                    raise TransferError(
                        f"批次号 {lot} 实收数量 {quantity} "
                        f"大于调出数量 {shipped[lot]}"
                    )

            credited = [
                (lot, quantity)
                for lot, quantity in receipts.items()
                if quantity > 0
            ]
            if credited:
                placeholders = ",".join("?" for _ in credited)
                date_rows = self._conn.execute(
                    f"SELECT lot, production_date, expiry_date "
                    f"FROM stock_batches "
                    f"WHERE warehouse = ? AND product = ? "
                    f"AND lot IN ({placeholders})",
                    (source, product, *[lot for lot, _ in credited]),
                ).fetchall()
                dates = {
                    lot: (production_date, expiry_date)
                    for lot, production_date, expiry_date in date_rows
                }
                for lot, quantity in credited:
                    lot_dates = dates.get(lot)
                    if lot_dates is None:
                        raise TransferError(
                            f"批次号 {lot} 未在来源仓 {source} "
                            f"商品 {product} 下落账"
                        )
                    production_date, expiry_date = lot_dates
                    self._conn.execute(
                        "INSERT INTO stock_batches "
                        "(warehouse, product, lot, production_date, "
                        "expiry_date, quantity) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(warehouse, product, lot) DO UPDATE SET "
                        "quantity = quantity + excluded.quantity",
                        (
                            target,
                            product,
                            lot,
                            production_date,
                            expiry_date,
                            quantity,
                        ),
                    )

            self._conn.executemany(
                "UPDATE transfer_items SET received_quantity = ? "
                "WHERE transfer_id = ? AND lot = ?",
                [
                    (quantity, transfer_id, lot)
                    for lot, quantity in receipts.items()
                ],
            )
            total_received = sum(receipts.values())
            self._conn.execute(
                "UPDATE transfers SET status = ?, received_quantity = ? "
                "WHERE id = ?",
                (TRANSFER_RECEIVED, total_received, transfer_id),
            )
            return total_received
