"""SQLite-backed local ledger for stock batches and transfers (stdlib only)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_DB_FILENAME = "stock_ledger.db"
ADJUSTMENT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Transfer order status literals; matched exactly, no case/spelling variants.
STATUS_SHIPPED = "shipped"
STATUS_RECEIVED = "received"
STATUS_RECEIVED_WITH_DIFF = "received-with-diff"
STATUS_CANCELED = "canceled"


class LedgerError(Exception):
    """A business-rule violation detected while writing to the ledger."""


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
class ShipLine:
    """One transfer line: which lot to move and how much."""

    lot: str
    quantity: int


@dataclass(frozen=True)
class AdjustmentRecord:
    """One persisted stock-count adjustment entry."""

    lot: str
    delta: int
    resulting_quantity: int
    reason: str
    occurred_at: str  # YYYY-MM-DD HH:MM:SS


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
    transfer_no TEXT PRIMARY KEY,
    source_warehouse TEXT NOT NULL,
    dest_warehouse TEXT NOT NULL,
    product TEXT NOT NULL,
    status TEXT NOT NULL,
    total_quantity INTEGER NOT NULL,
    diff_total INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transfer_lines (
    id INTEGER PRIMARY KEY,
    transfer_no TEXT NOT NULL REFERENCES transfers (transfer_no),
    seq INTEGER NOT NULL,
    lot TEXT NOT NULL,
    shipped_quantity INTEGER NOT NULL CHECK (shipped_quantity > 0),
    received_quantity INTEGER,
    UNIQUE (transfer_no, lot)
);
CREATE TABLE IF NOT EXISTS stock_adjustments (
    id INTEGER PRIMARY KEY,
    warehouse TEXT NOT NULL,
    product TEXT NOT NULL,
    lot TEXT NOT NULL,
    delta INTEGER NOT NULL CHECK (delta <> 0),
    resulting_quantity INTEGER NOT NULL CHECK (resulting_quantity >= 0),
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
"""


class Ledger:
    """Thin data-access wrapper around a sqlite3 connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, path: str | Path) -> "Ledger":
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA foreign_keys = ON")
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

    def adjust_stock(
        self,
        warehouse: str,
        product: str,
        lot: str,
        delta: int,
        reason: str,
    ) -> int:
        """Apply one signed stock-count adjustment atomically.

        The matched lot is located by exact (warehouse, product, lot) scope,
        so same-named lots under other warehouses/products are never touched.
        Its production/expiry dates never change. A missing lot, a zero
        delta, or a result below zero raises LedgerError and leaves both the
        stock and the adjustment history untouched. A result of exactly zero
        removes the exhausted batch row (as shipment does). The adjustment is
        recorded with its reason and timestamp before returning the new
        on-hand quantity.
        """
        if delta == 0:
            raise LedgerError("调整量不能为 0")
        try:
            with self._conn:
                row = self._conn.execute(
                    "SELECT quantity FROM stock_batches "
                    "WHERE warehouse = ? AND product = ? AND lot = ?",
                    (warehouse, product, lot),
                ).fetchone()
                if row is None:
                    raise LedgerError(
                        f"批次 {lot} 在仓库 {warehouse} 商品 {product} 下不存在"
                    )
                resulting = row[0] + delta
                if resulting < 0:
                    raise LedgerError(
                        f"批次 {lot} 调整后数量为 {resulting}，不能小于 0"
                    )
                if resulting == 0:
                    self._conn.execute(
                        "DELETE FROM stock_batches "
                        "WHERE warehouse = ? AND product = ? AND lot = ?",
                        (warehouse, product, lot),
                    )
                else:
                    self._conn.execute(
                        "UPDATE stock_batches SET quantity = ? "
                        "WHERE warehouse = ? AND product = ? AND lot = ?",
                        (resulting, warehouse, product, lot),
                    )
                occurred_at = datetime.now().strftime(ADJUSTMENT_TIME_FORMAT)
                self._conn.execute(
                    "INSERT INTO stock_adjustments "
                    "(warehouse, product, lot, delta, resulting_quantity, "
                    " reason, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        warehouse,
                        product,
                        lot,
                        delta,
                        resulting,
                        reason,
                        occurred_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"盘点调整失败：{exc}") from exc
        return resulting

    def list_adjustments(
        self, warehouse: str, product: str
    ) -> list[AdjustmentRecord]:
        """Return adjustment history for warehouse+product, oldest first.

        Entries sharing one timestamp stay in insertion order.
        """
        rows = self._conn.execute(
            "SELECT lot, delta, resulting_quantity, reason, occurred_at "
            "FROM stock_adjustments WHERE warehouse = ? AND product = ? "
            "ORDER BY occurred_at, id",
            (warehouse, product),
        )
        return [AdjustmentRecord(*row) for row in rows]


    def ship_transfer(
        self,
        transfer_no: str,
        source: str,
        dest: str,
        product: str,
        lines: Sequence[ShipLine],
    ) -> int:
        """Book a whole transfer order atomically; return total shipped quantity.

        The source lots are decremented and the destination lots incremented
        (reusing the source batch dates) in one transaction. Any rule
        violation raises LedgerError and leaves the ledger untouched.
        """
        if not lines:
            raise LedgerError("调拨单至少需要一行调拨行")
        seen_lots: set[str] = set()
        for line in lines:
            if line.lot in seen_lots:
                raise LedgerError(f"批次号 {line.lot} 在同一调拨单内重复")
            seen_lots.add(line.lot)

        try:
            with self._conn:
                existing = self._conn.execute(
                    "SELECT 1 FROM transfers WHERE transfer_no = ?",
                    (transfer_no,),
                ).fetchone()
                if existing is not None:
                    raise LedgerError(f"调拨单号 {transfer_no} 已存在")

                for line in lines:
                    row = self._conn.execute(
                        "SELECT production_date, expiry_date, quantity "
                        "FROM stock_batches "
                        "WHERE warehouse = ? AND product = ? AND lot = ?",
                        (source, product, line.lot),
                    ).fetchone()
                    if row is None:
                        raise LedgerError(
                            f"批次 {line.lot} 在仓库 {source} 商品 {product} 下不存在"
                        )
                    production_date, expiry_date, on_hand = row
                    if line.quantity > on_hand:
                        raise LedgerError(
                            f"批次 {line.lot} 现存数量 {on_hand} "
                            f"不足调拨数量 {line.quantity}"
                        )
                    remaining = on_hand - line.quantity
                    if remaining == 0:
                        self._conn.execute(
                            "DELETE FROM stock_batches "
                            "WHERE warehouse = ? AND product = ? AND lot = ?",
                            (source, product, line.lot),
                        )
                    else:
                        self._conn.execute(
                            "UPDATE stock_batches SET quantity = ? "
                            "WHERE warehouse = ? AND product = ? AND lot = ?",
                            (remaining, source, product, line.lot),
                        )
                    self._conn.execute(
                        "INSERT INTO stock_batches "
                        "(warehouse, product, lot, production_date, expiry_date, "
                        " quantity) VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (warehouse, product, lot) "
                        "DO UPDATE SET quantity = quantity + excluded.quantity",
                        (
                            dest,
                            product,
                            line.lot,
                            production_date,
                            expiry_date,
                            line.quantity,
                        ),
                    )

                total = sum(line.quantity for line in lines)
                self._conn.execute(
                    "INSERT INTO transfers "
                    "(transfer_no, source_warehouse, dest_warehouse, product, "
                    " status, total_quantity, diff_total) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (transfer_no, source, dest, product, STATUS_SHIPPED, total),
                )
                self._conn.executemany(
                    "INSERT INTO transfer_lines "
                    "(transfer_no, seq, lot, shipped_quantity) VALUES (?, ?, ?, ?)",
                    [
                        (transfer_no, seq, line.lot, line.quantity)
                        for seq, line in enumerate(lines, start=1)
                    ],
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"调拨提交失败：{exc}") from exc
        return total

    def cancel_transfer(self, transfer_no: str) -> int:
        """Cancel a shipped transfer order; return total returned quantity.

        Every line's shipped quantity is returned in full to the source
        warehouse's matching lot (accumulating onto an existing row or
        appending a new row that keeps the batch's current dates). The
        destination stock stays exactly as booked at ship time. The order
        becomes ``canceled`` with every line's received quantity set to 0
        and no pending difference. Any violation raises LedgerError and
        leaves the ledger and the order untouched.
        """
        try:
            with self._conn:
                row = self._conn.execute(
                    "SELECT status, source_warehouse, product "
                    "FROM transfers WHERE transfer_no = ?",
                    (transfer_no,),
                ).fetchone()
                if row is None:
                    raise LedgerError(f"调拨单 {transfer_no} 不存在")
                status, source, product = row
                if status != STATUS_SHIPPED:
                    raise LedgerError(
                        f"调拨单 {transfer_no} 状态为 {status}，不能取消"
                    )

                lines = self._conn.execute(
                    "SELECT lot, shipped_quantity FROM transfer_lines "
                    "WHERE transfer_no = ? ORDER BY seq",
                    (transfer_no,),
                ).fetchall()

                # Validate every return before touching any row, so an
                # illegal line rejects the whole cancellation.
                returns: list[tuple[str, int, str, str]] = []
                for lot, shipped_qty in lines:
                    source_row = self._conn.execute(
                        "SELECT production_date, expiry_date "
                        "FROM stock_batches "
                        "WHERE warehouse = ? AND product = ? AND lot = ?",
                        (source, product, lot),
                    ).fetchone()
                    if source_row is not None:
                        production_date, expiry_date = source_row
                    else:
                        # The source row was fully consumed at ship time, so
                        # the returned quantity is appended as a new row and
                        # keeps the batch's current dates wherever it now
                        # lives (prefer the order's destination warehouse).
                        batch_row = self._conn.execute(
                            "SELECT sb.production_date, sb.expiry_date "
                            "FROM stock_batches sb "
                            "WHERE sb.product = ? AND sb.lot = ? "
                            "ORDER BY CASE WHEN sb.warehouse = "
                            "    (SELECT dest_warehouse FROM transfers "
                            "     WHERE transfer_no = ?) THEN 0 ELSE 1 END, "
                            "sb.id LIMIT 1",
                            (product, lot, transfer_no),
                        ).fetchone()
                        if batch_row is None:
                            raise LedgerError(
                                f"批次 {lot} 在台账中无现存批次，无法退回发出仓 "
                                f"{source}"
                            )
                        production_date, expiry_date = batch_row
                    returns.append((lot, shipped_qty, production_date, expiry_date))

                for lot, shipped_qty, production_date, expiry_date in returns:
                    self._conn.execute(
                        "INSERT INTO stock_batches "
                        "(warehouse, product, lot, production_date, expiry_date, "
                        " quantity) VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (warehouse, product, lot) "
                        "DO UPDATE SET quantity = quantity + excluded.quantity",
                        (
                            source,
                            product,
                            lot,
                            production_date,
                            expiry_date,
                            shipped_qty,
                        ),
                    )

                total = sum(shipped_qty for _, shipped_qty, _, _ in returns)
                self._conn.execute(
                    "UPDATE transfer_lines SET received_quantity = 0 "
                    "WHERE transfer_no = ?",
                    (transfer_no,),
                )
                self._conn.execute(
                    "UPDATE transfers SET status = ?, diff_total = 0 "
                    "WHERE transfer_no = ?",
                    (STATUS_CANCELED, transfer_no),
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"调拨取消失败：{exc}") from exc
        return total

    def confirm_receipt(
        self, transfer_no: str, received: Mapping[str, int] | None
    ) -> tuple[int, int]:
        """Confirm receipt of a shipped transfer; return (status, diff_total).

        ``received=None`` accepts every line at its shipped quantity
        (status ``received``). Otherwise each lot's received quantity is
        checked against the order and the destination stock is adjusted by
        the difference (status ``received-with-diff``). Any violation raises
        LedgerError and leaves the ledger and the order untouched.
        """
        try:
            with self._conn:
                row = self._conn.execute(
                    "SELECT status, dest_warehouse, product "
                    "FROM transfers WHERE transfer_no = ?",
                    (transfer_no,),
                ).fetchone()
                if row is None:
                    raise LedgerError(f"调拨单 {transfer_no} 不存在")
                status, dest, product = row
                if status != STATUS_SHIPPED:
                    raise LedgerError(
                        f"调拨单 {transfer_no} 状态为 {status}，不能确认收货"
                    )

                lines = self._conn.execute(
                    "SELECT lot, shipped_quantity FROM transfer_lines "
                    "WHERE transfer_no = ? ORDER BY seq",
                    (transfer_no,),
                ).fetchall()

                if received is None:
                    self._conn.execute(
                        "UPDATE transfer_lines "
                        "SET received_quantity = shipped_quantity "
                        "WHERE transfer_no = ?",
                        (transfer_no,),
                    )
                    self._conn.execute(
                        "UPDATE transfers SET status = ? WHERE transfer_no = ?",
                        (STATUS_RECEIVED, transfer_no),
                    )
                    return STATUS_RECEIVED, 0

                shipped_by_lot = {lot: qty for lot, qty in lines}
                for lot, qty in received.items():
                    if lot not in shipped_by_lot:
                        raise LedgerError(
                            f"实收批次 {lot} 不在调拨单 {transfer_no} 中"
                        )
                    if qty > shipped_by_lot[lot]:
                        raise LedgerError(
                            f"批次 {lot} 实收数量 {qty} 超过发运数量 "
                            f"{shipped_by_lot[lot]}"
                        )

                diff_total = 0
                for lot, shipped_qty in lines:
                    received_qty = received.get(lot, 0)
                    diff = shipped_qty - received_qty
                    diff_total += diff
                    if diff:
                        # Ship-time booked the full shipped quantity at the
                        # destination; pull the unreceived difference back out.
                        row = self._conn.execute(
                            "SELECT quantity FROM stock_batches "
                            "WHERE warehouse = ? AND product = ? AND lot = ?",
                            (dest, product, lot),
                        ).fetchone()
                        if row is None or row[0] < diff:
                            raise LedgerError(
                                f"接收仓 {dest} 批次 {lot} 现存数量不足，"
                                "无法回冲差异"
                            )
                        if row[0] == diff:
                            self._conn.execute(
                                "DELETE FROM stock_batches "
                                "WHERE warehouse = ? AND product = ? AND lot = ?",
                                (dest, product, lot),
                            )
                        else:
                            self._conn.execute(
                                "UPDATE stock_batches SET quantity = quantity - ? "
                                "WHERE warehouse = ? AND product = ? AND lot = ?",
                                (diff, dest, product, lot),
                            )
                    self._conn.execute(
                        "UPDATE transfer_lines SET received_quantity = ? "
                        "WHERE transfer_no = ? AND lot = ?",
                        (received_qty, transfer_no, lot),
                    )
                self._conn.execute(
                    "UPDATE transfers SET status = ?, diff_total = ? "
                    "WHERE transfer_no = ?",
                    (STATUS_RECEIVED_WITH_DIFF, diff_total, transfer_no),
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"收货确认失败：{exc}") from exc
        return STATUS_RECEIVED_WITH_DIFF, diff_total
