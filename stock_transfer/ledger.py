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
class ResolveLine:
    """One difference-resolution line: which lot to clear, how much, and why."""

    lot: str
    quantity: int
    reason: str


@dataclass(frozen=True)
class DiffLine:
    """One transfer line's difference breakdown for the diff query."""

    lot: str
    shipped_quantity: int
    received_quantity: int
    pending_quantity: int
    resolved_quantity: int
    reason: str


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
    resolved_reason TEXT,
    UNIQUE (transfer_no, lot)
);
"""

# Columns added after the initial schema; backfilled into existing databases.
_TRANSFER_LINE_MIGRATIONS = (
    ("resolved_quantity", "resolved_quantity INTEGER NOT NULL DEFAULT 0"),
    ("resolved_reason", "resolved_reason TEXT"),
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
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(transfer_lines)")
        }
        for name, definition in _TRANSFER_LINE_MIGRATIONS:
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE transfer_lines ADD COLUMN {definition}"
                )
        conn.commit()
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

    def resolve_diff(
        self, transfer_no: str, resolutions: Sequence[ResolveLine]
    ) -> tuple[int, str]:
        """Clear pending differences of a received-with-diff transfer.

        Return ``(resolved_total, new_status)``. Every resolution line is
        checked against the order first: the lot must be one of the order's
        difference batches, must not repeat within one command, and the
        quantity must be a positive integer no larger than that lot's
        pending difference (shipped minus received minus already resolved).
        Any violation raises LedgerError and leaves the ledger and the order
        untouched. When every difference is cleared the order becomes
        ``resolved``; otherwise it stays ``received-with-diff`` with the
        remaining difference still on the books.
        """
        try:
            with self._conn:
                row = self._conn.execute(
                    "SELECT status FROM transfers WHERE transfer_no = ?",
                    (transfer_no,),
                ).fetchone()
                if row is None:
                    raise LedgerError(f"调拨单 {transfer_no} 不存在")
                status = row[0]
                if status != STATUS_RECEIVED_WITH_DIFF:
                    raise LedgerError(
                        f"调拨单 {transfer_no} 状态为 {status}，不能结清差异"
                    )

                lines = self._conn.execute(
                    "SELECT lot, shipped_quantity, received_quantity, "
                    "resolved_quantity FROM transfer_lines "
                    "WHERE transfer_no = ? ORDER BY seq",
                    (transfer_no,),
                ).fetchall()
                pending_by_lot = {
                    lot: shipped - received - resolved
                    for lot, shipped, received, resolved in lines
                }

                seen_lots: set[str] = set()
                for line in resolutions:
                    if line.lot in seen_lots:
                        raise LedgerError(
                            f"批次 {line.lot} 在同一结清命令内重复"
                        )
                    seen_lots.add(line.lot)
                    pending = pending_by_lot.get(line.lot)
                    if pending is None or pending <= 0:
                        raise LedgerError(
                            f"批次 {line.lot} 不在调拨单 {transfer_no} 的"
                            "差异批次中"
                        )
                    if line.quantity > pending:
                        raise LedgerError(
                            f"批次 {line.lot} 结清数量 {line.quantity} "
                            f"超过未结差异数量 {pending}"
                        )

                total = 0
                for line in resolutions:
                    self._conn.execute(
                        "UPDATE transfer_lines "
                        "SET resolved_quantity = resolved_quantity + ?, "
                        "    resolved_reason = ? "
                        "WHERE transfer_no = ? AND lot = ?",
                        (line.quantity, line.reason, transfer_no, line.lot),
                    )
                    total += line.quantity

                remaining = sum(pending_by_lot.values()) - total
                new_status = (
                    STATUS_RESOLVED if remaining == 0 else STATUS_RECEIVED_WITH_DIFF
                )
                self._conn.execute(
                    "UPDATE transfers SET status = ?, diff_total = ? "
                    "WHERE transfer_no = ?",
                    (new_status, remaining, transfer_no),
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"差异结清失败：{exc}") from exc
        return total, new_status

    def diff_report(self, transfer_no: str) -> tuple[str, list[DiffLine]]:
        """Return (status, per-lot difference lines) for any transfer order.

        Lines come back in the order's original line order. A ``canceled``
        order (and a ``shipped`` one, where no receipt was ever booked)
        carries no pending or resolved difference: both are reported as 0
        with the reason 未说明.
        """
        row = self._conn.execute(
            "SELECT status FROM transfers WHERE transfer_no = ?",
            (transfer_no,),
        ).fetchone()
        if row is None:
            raise LedgerError(f"调拨单 {transfer_no} 不存在")
        status = row[0]

        rows = self._conn.execute(
            "SELECT lot, shipped_quantity, received_quantity, "
            "resolved_quantity, resolved_reason FROM transfer_lines "
            "WHERE transfer_no = ? ORDER BY seq",
            (transfer_no,),
        ).fetchall()

        lines: list[DiffLine] = []
        for lot, shipped, received, resolved, reason in rows:
            if status == STATUS_CANCELED or received is None:
                pending = 0
                resolved = 0
                reason = None
            else:
                pending = shipped - received - resolved
            lines.append(
                DiffLine(
                    lot=lot,
                    shipped_quantity=shipped,
                    received_quantity=received if received is not None else 0,
                    pending_quantity=pending,
                    resolved_quantity=resolved,
                    reason=reason if reason else "未说明",
                )
            )
        return status, lines
