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
class ReceiptItemInput:
    """One ``批次号,实收数量`` receipt line (quantity may be zero)."""

    lot: str
    quantity: int


@dataclass(frozen=True)
class SplitPieceInput:
    """One ``新批次号,数量`` split line produced by a split command."""

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


@dataclass(frozen=True)
class AdjustmentRecord:
    """One persisted stock-taking adjustment row."""

    lot: str
    reason: str
    before_quantity: int
    after_quantity: int
    delta: int


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

_ADJUSTMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS stock_adjustments (
    id INTEGER PRIMARY KEY,
    warehouse TEXT NOT NULL,
    product TEXT NOT NULL,
    lot TEXT NOT NULL,
    reason TEXT NOT NULL,
    before_quantity INTEGER NOT NULL,
    after_quantity INTEGER NOT NULL CHECK (after_quantity >= 0),
    delta INTEGER NOT NULL
);
"""

_SCHEMA = "\n".join(
    [
        _BATCHES_TABLE,
        _TRANSFERS_TABLE,
        _TRANSFER_ITEMS_TABLE,
        _ADJUSTMENTS_TABLE,
    ]
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


def _migrate_transfer_items_received(conn: sqlite3.Connection) -> None:
    """Add the per-lot ``received_quantity`` column to an old transfer_items."""
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(transfer_items)").fetchall()
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
        _migrate_transfer_items_received(conn)
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

    def confirm_receipt(
        self,
        order_no: str,
        items: Sequence[ReceiptItemInput],
    ) -> TransferRecord:
        """Confirm one in-transit order's receipt and credit the target.

        Every allocation lot must be covered exactly once (the CLI validates
        line formatting and coverage); a received quantity of zero books
        nothing for that lot. Target lots already booked accumulate; new
        target lots inherit their production/expiry dates from the source
        warehouse's registration. Status flips to ``received`` with the
        received total, and per-lot received quantities are persisted.
        Everything happens in one transaction and rolls back whole on any
        rule violation (``TransferError``).
        """
        with self._conn:
            row = self._conn.execute(
                "SELECT id, source_warehouse, target_warehouse, product, status "
                "FROM transfers WHERE order_no = ?",
                (order_no,),
            ).fetchone()
            if row is None:
                raise TransferError(f"调拨单号 {order_no} 不存在")
            transfer_id, source, target, product, status = row
            if status == TRANSFER_RECEIVED:
                raise TransferError(
                    f"调拨单号 {order_no} 已是 received 状态，不得重复确认收货"
                )
            if status == TRANSFER_CANCELLED:
                raise TransferError(
                    f"调拨单号 {order_no} 已是 cancelled 状态，拒绝收货确认"
                )

            expected: dict[str, int] = dict(
                self._conn.execute(
                    "SELECT lot, quantity FROM transfer_items "
                    "WHERE transfer_id = ?",
                    (transfer_id,),
                ).fetchall()
            )
            received = {item.lot: item.quantity for item in items}
            coverage_errors: list[str] = []
            missing = [lot for lot in expected if lot not in received]
            if missing:
                coverage_errors.append(
                    "实收行未覆盖调拨单上的全部批次，缺少：" + "、".join(missing)
                )
            extra = [lot for lot in received if lot not in expected]
            if extra:
                coverage_errors.append(
                    "实收行出现调拨单上不存在的批次号：" + "、".join(extra)
                )
            if coverage_errors:
                raise TransferError("；".join(coverage_errors))
            for lot, quantity in received.items():
                if quantity < 0:
                    raise TransferError(
                        f"批次号 {lot} 实收数量 {quantity} 不能为负数"
                    )
                if quantity > expected[lot]:
                    raise TransferError(
                        f"批次号 {lot} 实收数量 {quantity} 超过调出数量 "
                        f"{expected[lot]}"
                    )

            lots = [lot for lot, quantity in received.items() if quantity > 0]
            dates: dict[str, tuple[str, str]] = {}
            if lots:
                placeholders = ",".join("?" for _ in lots)
                date_rows = self._conn.execute(
                    f"SELECT lot, production_date, expiry_date "
                    f"FROM stock_batches "
                    f"WHERE warehouse = ? AND product = ? "
                    f"AND lot IN ({placeholders})",
                    (source, product, *lots),
                )
                dates = {
                    lot: (production_date, expiry_date)
                    for lot, production_date, expiry_date in date_rows
                }

            for lot, quantity in received.items():
                if quantity == 0:
                    # Nothing arrived: no quantity movement and no new lot row.
                    continue
                existing = self._conn.execute(
                    "SELECT quantity FROM stock_batches "
                    "WHERE warehouse = ? AND product = ? AND lot = ?",
                    (target, product, lot),
                ).fetchone()
                if existing is not None:
                    self._conn.execute(
                        "UPDATE stock_batches SET quantity = quantity + ? "
                        "WHERE warehouse = ? AND product = ? AND lot = ?",
                        (quantity, target, product, lot),
                    )
                else:
                    production_date, expiry_date = dates[lot]
                    self._conn.execute(
                        "INSERT INTO stock_batches "
                        "(warehouse, product, lot, production_date, "
                        "expiry_date, quantity) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
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
                    for lot, quantity in received.items()
                ],
            )
            total = sum(received.values())
            self._conn.execute(
                "UPDATE transfers SET status = ?, received_quantity = ? "
                "WHERE id = ?",
                (TRANSFER_RECEIVED, total, transfer_id),
            )

        record = self.get_transfer(order_no)
        assert record is not None
        return record

    def split_batch(
        self,
        warehouse: str,
        product: str,
        source_lot: str,
        pieces: Sequence[SplitPieceInput],
    ) -> None:
        """Replace one booked batch with new lots in a single transaction.

        The source row is removed and each piece becomes its own batch
        row inheriting the source production/expiry dates; the piece
        quantities must sum exactly to the source on-hand quantity.
        Raises ``TransferError`` on a missing source lot, a quantity
        mismatch, or a new lot number already booked; the whole
        transaction rolls back on any violation.
        """
        with self._conn:
            row = self._conn.execute(
                "SELECT production_date, expiry_date, quantity "
                "FROM stock_batches "
                "WHERE warehouse = ? AND product = ? AND lot = ?",
                (warehouse, product, source_lot),
            ).fetchone()
            if row is None:
                raise TransferError(
                    f"批次号 {source_lot} 未在仓库 {warehouse} "
                    f"商品 {product} 下落账"
                )
            production_date, expiry_date, quantity = row
            total = sum(piece.quantity for piece in pieces)
            if total != quantity:
                raise TransferError(
                    f"拆分数量之和 {total} 必须等于来源批次 {source_lot} "
                    f"现存数量 {quantity}"
                )
            conflicts = self.existing_lots(
                warehouse, product, [piece.lot for piece in pieces]
            )
            if conflicts:
                raise TransferError(
                    "新批次号 " + "、".join(sorted(conflicts))
                    + f" 已在仓库 {warehouse} 商品 {product} 下落账"
                )
            self._conn.execute(
                "DELETE FROM stock_batches "
                "WHERE warehouse = ? AND product = ? AND lot = ?",
                (warehouse, product, source_lot),
            )
            self._conn.executemany(
                "INSERT INTO stock_batches "
                "(warehouse, product, lot, production_date, expiry_date, "
                "quantity) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        warehouse,
                        product,
                        piece.lot,
                        production_date,
                        expiry_date,
                        piece.quantity,
                    )
                    for piece in pieces
                ],
            )

    def merge_batches(
        self,
        warehouse: str,
        product: str,
        source_lots: Sequence[str],
        target_lot: str,
    ) -> None:
        """Merge two booked batches into one target lot atomically.

        Both source rows are removed and the target lot is booked with
        the summed quantity, the earlier production date and the later
        expiry date of the two sources. Raises ``TransferError`` when a
        source lot is missing or holds no positive quantity, when the
        target equals a source, or when the target lot is already
        booked; the whole transaction rolls back on any violation.
        """
        first, second = source_lots
        with self._conn:
            records: dict[str, tuple[str, str, int]] = {}
            for lot in (first, second):
                row = self._conn.execute(
                    "SELECT production_date, expiry_date, quantity "
                    "FROM stock_batches "
                    "WHERE warehouse = ? AND product = ? AND lot = ?",
                    (warehouse, product, lot),
                ).fetchone()
                if row is None:
                    raise TransferError(
                        f"批次号 {lot} 未在仓库 {warehouse} "
                        f"商品 {product} 下落账"
                    )
                records[lot] = (row[0], row[1], row[2])
            for lot in (first, second):
                if records[lot][2] <= 0:
                    raise TransferError(
                        f"批次号 {lot} 现存数量为 0，不得参与合并"
                    )
            if target_lot in (first, second):
                raise TransferError(
                    f"目标批次号 {target_lot} 不得等于任一来源批次号"
                )
            if self.existing_lots(warehouse, product, [target_lot]):
                raise TransferError(
                    f"目标批次号 {target_lot} 已在仓库 {warehouse} "
                    f"商品 {product} 下落账"
                )
            production_date = min(records[first][0], records[second][0])
            expiry_date = max(records[first][1], records[second][1])
            quantity = records[first][2] + records[second][2]
            self._conn.executemany(
                "DELETE FROM stock_batches "
                "WHERE warehouse = ? AND product = ? AND lot = ?",
                [(warehouse, product, lot) for lot in (first, second)],
            )
            self._conn.execute(
                "INSERT INTO stock_batches "
                "(warehouse, product, lot, production_date, expiry_date, "
                "quantity) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    warehouse,
                    product,
                    target_lot,
                    production_date,
                    expiry_date,
                    quantity,
                ),
            )

    def adjust_batch(
        self,
        warehouse: str,
        product: str,
        lot: str,
        counted_quantity: int,
        reason: str,
    ) -> AdjustmentRecord:
        """Book one stock-taking adjustment in a single transaction.

        The lot's on-hand quantity is set to the counted physical
        quantity (zero allowed, meaning the lot is physically gone);
        the difference is positive for a surplus （盘盈） and negative
        for a shortage （盘亏）. One adjustment row recording the reason
        and the before/after quantities is written alongside. Raises
        ``TransferError`` when the lot is not booked under this
        warehouse and product; the whole transaction rolls back on any
        violation. Transfer orders and their lines are never touched.
        """
        with self._conn:
            row = self._conn.execute(
                "SELECT quantity FROM stock_batches "
                "WHERE warehouse = ? AND product = ? AND lot = ?",
                (warehouse, product, lot),
            ).fetchone()
            if row is None:
                raise TransferError(
                    f"批次号 {lot} 未在仓库 {warehouse} "
                    f"商品 {product} 下落账"
                )
            before = row[0]
            delta = counted_quantity - before
            self._conn.execute(
                "UPDATE stock_batches SET quantity = ? "
                "WHERE warehouse = ? AND product = ? AND lot = ?",
                (counted_quantity, warehouse, product, lot),
            )
            self._conn.execute(
                "INSERT INTO stock_adjustments "
                "(warehouse, product, lot, reason, before_quantity, "
                "after_quantity, delta) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    warehouse,
                    product,
                    lot,
                    reason,
                    before,
                    counted_quantity,
                    delta,
                ),
            )
        return AdjustmentRecord(lot, reason, before, counted_quantity, delta)

    def list_adjustments(
        self, warehouse: str, product: str
    ) -> list[AdjustmentRecord]:
        """Return all adjustment rows for warehouse+product, oldest first."""
        rows = self._conn.execute(
            "SELECT lot, reason, before_quantity, after_quantity, delta "
            "FROM stock_adjustments WHERE warehouse = ? AND product = ? "
            "ORDER BY id",
            (warehouse, product),
        )
        return [AdjustmentRecord(*row) for row in rows]
