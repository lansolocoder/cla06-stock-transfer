"""本地批次台账：sqlite3 持久化与入库校验（仅用标准库）。"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

LEDGER_FILENAME = "stock_ledger.db"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INT_RE = re.compile(r"^\d+$")


class LedgerError(ValueError):
    """登记输入不合法。

    errors 为 ``(行号, 字段, 原因)`` 三元组：批次行号从 1 开始，
    0 表示仓库/商品这一层的整单问题。
    """

    def __init__(self, errors: list[tuple[int, str, str]]):
        self.errors = errors
        super().__init__(
            "; ".join(
                (f"第 {row} 行 " if row else "") + f"{field}: {reason}"
                for row, field, reason in errors
            )
        )


@dataclass(frozen=True)
class BatchRecord:
    """一个已落账批次行。"""

    batch_no: str
    production_date: str
    expiry_date: str
    quantity: int


@dataclass(frozen=True)
class _ParsedBatch:
    batch_no: str
    production_date: date
    expiry_date: date
    quantity: int


def _parse_date(value: str, field: str, row: int, errors: list) -> date | None:
    if not _DATE_RE.match(value):
        errors.append((row, field, "日期格式必须为 YYYY-MM-DD"))
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append((row, field, "日期不存在"))
        return None


def validate_submission(
    warehouse: str,
    product: str,
    raw_lines: list[str],
    existing_batch_nos: set[str],
) -> tuple[str, str, list[_ParsedBatch]]:
    """校验一次入库登记；全部合法才返回解析结果，否则抛 :class:`LedgerError`。

    每个 raw_line 形如 ``批次号,生产日期,有效期至,数量``，行号即其在列表中的
    位置（从 1 开始）。
    """

    errors: list[tuple[int, str, str]] = []

    warehouse_code = warehouse.strip()
    if not warehouse_code:
        errors.append((0, "仓库代码", "去首尾空白后不能为空"))

    product_code = product.strip()
    if not product_code:
        errors.append((0, "商品代码", "去首尾空白后不能为空"))

    seen_in_submission: dict[str, int] = {}
    parsed: list[_ParsedBatch] = []

    for index, line in enumerate(raw_lines, start=1):
        parts = line.split(",")
        if len(parts) != 4:
            errors.append(
                (
                    index,
                    "批次行格式",
                    "应为 批次号,生产日期,有效期至,数量 四个逗号分隔字段",
                )
            )
            continue

        batch_no_raw, production_raw, expiry_raw, quantity_raw = (
            part.strip() for part in parts
        )

        batch_no = batch_no_raw
        if not batch_no:
            errors.append((index, "批次号", "去首尾空白后不能为空"))
        elif batch_no in existing_batch_nos:
            errors.append((index, "批次号", f"与已落账批次 {batch_no} 重复"))
        elif batch_no in seen_in_submission:
            errors.append(
                (
                    index,
                    "批次号",
                    f"与本次登记第 {seen_in_submission[batch_no]} 行重复",
                )
            )
        else:
            seen_in_submission[batch_no] = index

        production_date = _parse_date(production_raw, "生产日期", index, errors)
        expiry_date = _parse_date(expiry_raw, "有效期至", index, errors)
        if (
            production_date is not None
            and expiry_date is not None
            and expiry_date <= production_date
        ):
            errors.append((index, "有效期至", "必须晚于生产日期"))

        if not _INT_RE.match(quantity_raw) or int(quantity_raw) <= 0:
            errors.append((index, "数量", "必须为正整数"))

        parsed.append(
            _ParsedBatch(
                batch_no,
                production_date or date.min,
                expiry_date or date.min,
                int(quantity_raw) if _INT_RE.match(quantity_raw) else 0,
            )
        )

    if errors:
        raise LedgerError(errors)

    return warehouse_code, product_code, parsed


class Ledger:
    """当前工作目录下的 sqlite3 台账文件。"""

    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_batches (
                id INTEGER PRIMARY KEY,
                warehouse TEXT NOT NULL,
                product TEXT NOT NULL,
                batch_no TEXT NOT NULL,
                production_date TEXT NOT NULL,
                expiry_date TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                UNIQUE(warehouse, product, batch_no)
            )
            """
        )
        return conn

    def register(
        self,
        warehouse: str,
        product: str,
        raw_lines: list[str],
    ) -> tuple[str, str, list[_ParsedBatch]]:
        """整次提交一次落账；任一行不合法则不写入任何记录。"""

        conn = self._connect()
        try:
            with conn:
                existing = {
                    row[0]
                    for row in conn.execute(
                        "SELECT batch_no FROM stock_batches WHERE warehouse = ? AND product = ?",
                        (warehouse.strip(), product.strip()),
                    )
                }
                warehouse_code, product_code, batches = validate_submission(
                    warehouse, product, raw_lines, existing
                )
                conn.executemany(
                    """
                    INSERT INTO stock_batches
                        (warehouse, product, batch_no, production_date, expiry_date, quantity)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            warehouse_code,
                            product_code,
                            batch.batch_no,
                            batch.production_date.isoformat(),
                            batch.expiry_date.isoformat(),
                            batch.quantity,
                        )
                        for batch in batches
                    ],
                )
        finally:
            conn.close()

        return warehouse_code, product_code, batches

    def query_batches(self, warehouse: str, product: str) -> list[BatchRecord]:
        """返回指定仓库商品下的全部批次；台账不存在或无数据时返回空列表。"""

        if not self.path.exists():
            return []

        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT batch_no, production_date, expiry_date, quantity
                FROM stock_batches
                WHERE warehouse = ? AND product = ?
                ORDER BY id
                """,
                (warehouse.strip(), product.strip()),
            ).fetchall()
        finally:
            conn.close()

        return [BatchRecord(*row) for row in rows]
