"""SQLite-backed local ledger for stock batches and transfers (stdlib only)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_FILENAME = "stock_ledger.db"

# Transfer order status literals; matched exactly, no case/spelling variants.
STATUS_SHIPPED = "shipped"
STATUS_RECEIVED = "received"
STATUS_RECEIVED_WITH_DIFF = "received-with-diff"
STATUS_CANCELED = "canceled"
STATUS_RESOLVED = "resolved"


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
class TransferDetailLine:
    """One persisted transfer line with its reconciliation figures."""

    lot: str
    shipped_quantity: int
    received_quantity: int | None  # None while receipt is unconfirmed
    resolved_quantity: int


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
    resolved_quantity INTEGER NOT NULL DEFAULT 0,
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
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        cls._migrate(conn)
        return cls(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Apply small additive migrations to ledgers created by older versions."""
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(transfer_lines)")
        }
        if "resolved_quantity" not in columns:
            conn.execute(
                "ALTER TABLE transfer_lines "
                "ADD COLUMN resolved_quantity INTEGER NOT NULL DEFAULT 0"
            )
            conn.commit()

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

    def merge_batches(
        self, warehouse: str, product: str, target_lot: str, source_lot: str
    ) -> int:
        """Merge *source_lot* into *target_lot*; return the merged quantity.

        Both lots must already exist under warehouse+product. The source row
        is deleted, its quantity is added to the target row, and the target
        row keeps the earlier production date and the earlier expiry date of
        the two. Transfer orders and lines are untouched. Any violation
        raises LedgerError and leaves the ledger untouched.
        """
        with self._conn:
            row = self._conn.execute(
                "SELECT lot, production_date, expiry_date, quantity "
                "FROM stock_batches "
                "WHERE warehouse = ? AND product = ? AND lot IN (?, ?)",
                (warehouse, product, target_lot, source_lot),
            ).fetchall()
            by_lot = {lot: (production, expiry, qty) for lot, production, expiry, qty in row}
            if target_lot not in by_lot:
                raise LedgerError(
                    f"目标批次 {target_lot} 在仓库 {warehouse} 商品 {product} 下不存在"
                )
            if source_lot not in by_lot:
                raise LedgerError(
                    f"来源批次 {source_lot} 在仓库 {warehouse} 商品 {product} 下不存在"
                )
            target_production, target_expiry, target_qty = by_lot[target_lot]
            source_production, source_expiry, source_qty = by_lot[source_lot]

            self._conn.execute(
                "UPDATE stock_batches SET quantity = ?, "
                "production_date = ?, expiry_date = ? "
                "WHERE warehouse = ? AND product = ? AND lot = ?",
                (
                    target_qty + source_qty,
                    min(target_production, source_production),
                    min(target_expiry, source_expiry),
                    warehouse,
                    product,
                    target_lot,
                ),
            )
            self._conn.execute(
                "DELETE FROM stock_batches "
                "WHERE warehouse = ? AND product = ? AND lot = ?",
                (warehouse, product, source_lot),
            )
        return source_qty

    def split_batch(
        self,
        warehouse: str,
        product: str,
        lot: str,
        new_lot: str,
        quantity: int,
    ) -> None:
        """Split *quantity* off *lot* into a new batch row *new_lot*.

        The quantity must be a positive integer smaller than the lot's
        on-hand quantity, and *new_lot* must not already exist under
        warehouse+product. The original row's quantity is reduced and a new
        row is appended with the original row's production and expiry dates.
        Transfer orders and lines are untouched. Any violation raises
        LedgerError and leaves the ledger untouched.
        """
        try:
            with self._conn:
                row = self._conn.execute(
                    "SELECT production_date, expiry_date, quantity "
                    "FROM stock_batches "
                    "WHERE warehouse = ? AND product = ? AND lot = ?",
                    (warehouse, product, lot),
                ).fetchone()
                if row is None:
                    raise LedgerError(
                        f"批次 {lot} 在仓库 {warehouse} 商品 {product} 下不存在"
                    )
                production_date, expiry_date, on_hand = row
                if quantity >= on_hand:
                    raise LedgerError(
                        f"拆分数量 {quantity} 必须小于批次 {lot} 现存数量 {on_hand}"
                    )
                existing = self._conn.execute(
                    "SELECT 1 FROM stock_batches "
                    "WHERE warehouse = ? AND product = ? AND lot = ?",
                    (warehouse, product, new_lot),
                ).fetchone()
                if existing is not None:
                    raise LedgerError(
                        f"新批次号 {new_lot} 已在仓库 {warehouse} 商品 {product} 下落账"
                    )

                self._conn.execute(
                    "UPDATE stock_batches SET quantity = quantity - ? "
                    "WHERE warehouse = ? AND product = ? AND lot = ?",
                    (quantity, warehouse, product, lot),
                )
                self._conn.execute(
                    "INSERT INTO stock_batches "
                    "(warehouse, product, lot, production_date, expiry_date, "
                    " quantity) VALUES (?, ?, ?, ?, ?, ?)",
                    (warehouse, product, new_lot, production_date, expiry_date,
                     quantity),
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"批次拆分失败：{exc}") from exc

    def get_transfer_detail(
        self, transfer_no: str
    ) -> tuple[str, list[TransferDetailLine]] | None:
        """Return (status, lines) for a transfer in original line order.

        Read-only: returns None when no transfer with *transfer_no* exists.
        """
        row = self._conn.execute(
            "SELECT status FROM transfers WHERE transfer_no = ?",
            (transfer_no,),
        ).fetchone()
        if row is None:
            return None
        (status,) = row
        rows = self._conn.execute(
            "SELECT lot, shipped_quantity, received_quantity, resolved_quantity "
            "FROM transfer_lines WHERE transfer_no = ? ORDER BY seq",
            (transfer_no,),
        )
        return status, [TransferDetailLine(*line) for line in rows]

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

    def resolve_differences(
        self, transfer_no: str, resolutions: Mapping[str, int] | None
    ) -> tuple[str, int]:
        """Give pending differences their final disposition.

        Only orders in status ``received`` or ``received-with-diff`` are
        eligible. A lot's pending difference is its shipped quantity minus
        its received quantity minus what was already closed on previous
        calls. ``resolutions=None`` closes every lot's full pending
        difference; otherwise each entry (lot -> quantity) closes exactly
        that much of one lot, and lots left untouched keep their pending
        difference. Stock in neither warehouse changes and the received
        quantities stay frozen.

        Returns ``(status, resolved_total)``: ``resolved`` once the order
        has no pending difference left, otherwise ``received-with-diff``.
        Any violation raises LedgerError and leaves everything untouched.
        """
        try:
            with self._conn:
                row = self._conn.execute(
                    "SELECT status FROM transfers WHERE transfer_no = ?",
                    (transfer_no,),
                ).fetchone()
                if row is None:
                    raise LedgerError(f"调拨单 {transfer_no} 不存在")
                (status,) = row
                if status not in (STATUS_RECEIVED, STATUS_RECEIVED_WITH_DIFF):
                    raise LedgerError(
                        f"调拨单 {transfer_no} 状态为 {status}，不能差异处理"
                    )

                lines = self._conn.execute(
                    "SELECT lot, shipped_quantity, received_quantity, "
                    "resolved_quantity FROM transfer_lines "
                    "WHERE transfer_no = ? ORDER BY seq",
                    (transfer_no,),
                ).fetchall()
                pending_by_lot = {
                    lot: shipped_qty - received_qty - resolved_qty
                    for lot, shipped_qty, received_qty, resolved_qty in lines
                }

                if resolutions is None:
                    to_close = dict(pending_by_lot)
                else:
                    to_close = {}
                    for lot, quantity in resolutions.items():
                        if lot not in pending_by_lot:
                            raise LedgerError(
                                f"处理批次 {lot} 不在调拨单 {transfer_no} 中"
                            )
                        if quantity <= 0:
                            raise LedgerError(
                                f"批次 {lot} 结案数量 {quantity} 必须为正整数"
                            )
                        if quantity > pending_by_lot[lot]:
                            raise LedgerError(
                                f"批次 {lot} 结案数量 {quantity} 超过当前挂账差异量 "
                                f"{pending_by_lot[lot]}"
                            )
                        to_close[lot] = quantity

                resolved_total = 0
                for lot, quantity in to_close.items():
                    if quantity <= 0:
                        continue
                    self._conn.execute(
                        "UPDATE transfer_lines SET "
                        "resolved_quantity = resolved_quantity + ? "
                        "WHERE transfer_no = ? AND lot = ?",
                        (quantity, transfer_no, lot),
                    )
                    resolved_total += quantity

                remaining = sum(pending_by_lot.values()) - resolved_total
                new_status = (
                    STATUS_RESOLVED if remaining == 0 else STATUS_RECEIVED_WITH_DIFF
                )
                self._conn.execute(
                    "UPDATE transfers SET status = ?, diff_total = ? "
                    "WHERE transfer_no = ?",
                    (new_status, remaining, transfer_no),
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"差异处理失败：{exc}") from exc
        return new_status, resolved_total
