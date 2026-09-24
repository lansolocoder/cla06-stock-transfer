"""Command-line entry point."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from . import __version__
from .ledger import (
    DEFAULT_DB_FILENAME,
    BatchInput,
    Ledger,
    TransferLineInput,
)

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _parse_iso_date(value: str) -> date | None:
    """Parse a strict YYYY-MM-DD date, or return None if invalid."""
    match = _DATE_RE.match(value)
    if match is None:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def _validate_batches(
    raw_lines: Sequence[str],
) -> tuple[list[tuple[int, BatchInput]], list[str]]:
    """Validate every batch line; return (line number, batch) pairs and errors.

    A line has the form ``批次号,生产日期,有效期至,数量``. Field-level errors
    are collected per line; callers must reject the whole submission whenever
    the error list is non-empty.
    """
    parsed: list[tuple[int, BatchInput]] = []
    errors: list[str] = []
    seen_lots: dict[str, int] = {}

    for index, raw in enumerate(raw_lines, start=1):
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) != 4:
            errors.append(
                f"批次行 {index}: 批次行格式错误，应为"
                "“批次号,生产日期,有效期至,数量”"
            )
            continue

        lot, production_raw, expiry_raw, quantity_raw = parts
        line_ok = True

        if not lot:
            errors.append(f"批次行 {index}: 批次号不能为空")
            line_ok = False

        production_date = _parse_iso_date(production_raw)
        if production_date is None:
            errors.append(
                f"批次行 {index}: 生产日期 {production_raw!r} 格式必须为 YYYY-MM-DD"
            )
            line_ok = False

        expiry_date = _parse_iso_date(expiry_raw)
        if expiry_date is None:
            errors.append(
                f"批次行 {index}: 有效期至 {expiry_raw!r} 格式必须为 YYYY-MM-DD"
            )
            line_ok = False
        elif production_date is not None and expiry_date <= production_date:
            errors.append(
                f"批次行 {index}: 有效期至 {expiry_raw} 必须晚于生产日期 "
                f"{production_raw}"
            )
            line_ok = False

        if not quantity_raw.isdigit() or int(quantity_raw) <= 0:
            errors.append(f"批次行 {index}: 数量 {quantity_raw!r} 必须为正整数")
            line_ok = False

        if not line_ok:
            continue

        batch = BatchInput(lot, production_raw, expiry_raw, int(quantity_raw))
        parsed.append((index, batch))
        first_line = seen_lots.get(lot)
        if first_line is None:
            seen_lots[lot] = index
        else:
            errors.append(
                f"批次行 {index}: 批次号 {lot} 与批次行 {first_line} 重复"
            )

    return parsed, errors


def _validate_transfer_lines(
    raw_lines: Sequence[str],
) -> tuple[list[tuple[int, TransferLineInput]], list[str]]:
    """Validate every allocation line; return (line number, line) pairs and errors.

    A line has the form ``批次号,调出数量`` — the quantity is the only
    quantity on the line; no summary/total quantity is accepted. Field-level
    errors are collected per line; callers must reject the whole submission
    whenever the error list is non-empty.
    """
    parsed: list[tuple[int, TransferLineInput]] = []
    errors: list[str] = []
    seen_lots: dict[str, int] = {}

    for index, raw in enumerate(raw_lines, start=1):
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) != 2:
            errors.append(
                f"调拨行 {index}: 调拨行格式错误，应为“批次号,调出数量”"
            )
            continue

        lot, quantity_raw = parts
        line_ok = True

        if not lot:
            errors.append(f"调拨行 {index}: 批次号不能为空")
            line_ok = False

        if not quantity_raw.isdigit() or int(quantity_raw) <= 0:
            errors.append(
                f"调拨行 {index}: 调出数量 {quantity_raw!r} 必须为正整数"
            )
            line_ok = False

        if not line_ok:
            continue

        parsed.append((index, TransferLineInput(lot, int(quantity_raw))))
        first_line = seen_lots.get(lot)
        if first_line is None:
            seen_lots[lot] = index
        else:
            errors.append(
                f"调拨行 {index}: 批次号 {lot} 与调拨行 {first_line} 重复"
            )

    return parsed, errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-transfer",
        description="Local 多仓库存台账：库存登记、批次查询与调拨在途跟踪。",
        epilog=(
            "示例：\n"
            "  python3 -m stock_transfer register --warehouse WH-A "
            "--product SKU-1001 \\\n"
            "      --batch LOT-2024-001,2024-03-01,2025-03-01,18 \\\n"
            "      --batch LOT-2024-002,2024-04-02,2025-04-02,12\n"
            "  python3 -m stock_transfer query --warehouse WH-A "
            "--product SKU-1001\n"
            "  python3 -m stock_transfer transfer --order TR-0001 "
            "--source WH-A --target WH-B --product SKU-1001 \\\n"
            "      --line LOT-2024-001,5\n"
            "  python3 -m stock_transfer transfer-query --order TR-0001"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    register = subparsers.add_parser(
        "register",
        help="把一次入库的一个或多个批次登记到指定仓库的指定商品下",
        description=(
            "整次提交一次落账；任一批次行不合法则全部拒绝。"
            "批次行格式：批次号,生产日期,有效期至,数量（YYYY-MM-DD，正整数）。"
        ),
    )
    register.add_argument("--warehouse", required=True, help="仓库代码")
    register.add_argument("--product", required=True, help="商品代码")
    register.add_argument(
        "--batch",
        required=True,
        action="append",
        metavar="批次号,生产日期,有效期至,数量",
        help="批次行，可重复提供以在一次登记中拆成多个批次",
    )
    register.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )

    query = subparsers.add_parser(
        "query",
        help="查询指定仓库、商品下每个批次的现存数量",
    )
    query.add_argument("--warehouse", required=True, help="仓库代码")
    query.add_argument("--product", required=True, help="商品代码")
    query.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )

    transfer = subparsers.add_parser(
        "transfer",
        help="把指定商品按批次从来源仓调出到目标仓，生成在途调拨单",
        description=(
            "整单一次落账：来源仓各批次现存数量立即扣减，目标仓不立即收货，"
            "单据状态为 in_transit、实收数量为 0；任一行不合法则整单拒绝。"
            "调拨行格式：批次号,调出数量（正整数，为本行唯一数量含义）。"
        ),
    )
    transfer.add_argument("--order", required=True, help="调拨单号（全局唯一）")
    transfer.add_argument("--source", required=True, help="来源仓代码")
    transfer.add_argument("--target", required=True, help="目标仓代码")
    transfer.add_argument("--product", required=True, help="商品代码")
    transfer.add_argument(
        "--line",
        required=True,
        action="append",
        metavar="批次号,调出数量",
        help="批次调拨行，可重复提供以在一单内调出多个批次",
    )
    transfer.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )

    transfer_query = subparsers.add_parser(
        "transfer-query",
        help="按调拨单号查询调拨单状态、实收数量与各批次调出数量",
    )
    transfer_query.add_argument("--order", required=True, help="调拨单号")
    transfer_query.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )
    return parser


def _db_path(args: argparse.Namespace) -> Path:
    return Path(args.db) if args.db else Path.cwd() / DEFAULT_DB_FILENAME


def _run_register(args: argparse.Namespace) -> int:
    warehouse = args.warehouse.strip()
    product = args.product.strip()
    if not warehouse:
        print("仓库代码去首尾空白后不能为空", file=sys.stderr)
        return 1
    if not product:
        print("商品代码去首尾空白后不能为空", file=sys.stderr)
        return 1

    parsed, errors = _validate_batches(args.batch)
    db_path = _db_path(args)

    if parsed:
        with Ledger.open(db_path) as ledger:
            conflicts = ledger.existing_lots(
                warehouse, product, [batch.lot for _, batch in parsed]
            )
        for line_number, batch in parsed:
            if batch.lot in conflicts:
                errors.append(
                    f"批次行 {line_number}: 批次号 {batch.lot} "
                    f"已在仓库 {warehouse} 商品 {product} 下落账"
                )

    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return 1

    try:
        with Ledger.open(db_path) as ledger:
            ledger.add_batches(warehouse, product, [batch for _, batch in parsed])
    except sqlite3.IntegrityError:
        print(
            "登记失败：批次号与已落账批次冲突（仓库 "
            f"{warehouse} 商品 {product}）",
            file=sys.stderr,
        )
        return 1

    total = sum(batch.quantity for _, batch in parsed)
    print(
        f"登记成功：仓库={warehouse} 商品={product} "
        f"总数量={total} 批次数={len(parsed)}"
    )
    return 0


def _run_query(args: argparse.Namespace) -> int:
    warehouse = args.warehouse.strip()
    product = args.product.strip()
    if not warehouse:
        print("仓库代码去首尾空白后不能为空", file=sys.stderr)
        return 1
    if not product:
        print("商品代码去首尾空白后不能为空", file=sys.stderr)
        return 1

    db_path = _db_path(args)
    if not db_path.exists():
        records = []
    else:
        with Ledger.open(db_path) as ledger:
            records = ledger.list_batches(warehouse, product)

    print(f"仓库={warehouse} 商品={product} 批次总数={len(records)}")
    for record in records:
        print(
            f"批次号={record.lot} 生产日期={record.production_date} "
            f"有效期至={record.expiry_date} 数量={record.quantity}"
        )
    return 0


def _run_transfer(args: argparse.Namespace) -> int:
    order_no = args.order.strip()
    source = args.source.strip()
    target = args.target.strip()
    product = args.product.strip()

    errors: list[str] = []
    if not order_no:
        errors.append("调拨单号去首尾空白后不能为空")
    if not source:
        errors.append("来源仓代码去首尾空白后不能为空")
    if not target:
        errors.append("目标仓代码去首尾空白后不能为空")
    if not product:
        errors.append("商品代码去首尾空白后不能为空")
    if source and target and source == target:
        errors.append("目标仓不得与来源仓相同")

    parsed, line_errors = _validate_transfer_lines(args.line)
    errors.extend(line_errors)

    db_path = _db_path(args)
    if not errors:
        with Ledger.open(db_path) as ledger:
            if ledger.transfer_order_exists(order_no):
                errors.append(f"调拨单号 {order_no} 已存在")
            quantities = ledger.batch_quantities(
                source, product, [line.lot for _, line in parsed]
            )
        for line_number, line in parsed:
            current = quantities.get(line.lot)
            if current is None:
                errors.append(
                    f"调拨行 {line_number}: 批次号 {line.lot} 未在来源仓 "
                    f"{source} 商品 {product} 下落账"
                )
            elif line.quantity > current:
                errors.append(
                    f"调拨行 {line_number}: 调出数量 {line.quantity} 超出来源仓 "
                    f"{source} 批次 {line.lot} 现存数量 {current}"
                )

    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return 1

    try:
        with Ledger.open(db_path) as ledger:
            ledger.create_transfer(
                order_no, source, target, product,
                [line for _, line in parsed],
            )
    except sqlite3.IntegrityError as exc:
        print(f"调拨失败：{exc}", file=sys.stderr)
        return 1

    total = sum(line.quantity for _, line in parsed)
    print(
        f"调拨成功：单号={order_no} 来源仓={source} 目标仓={target} "
        f"商品={product} 状态=in_transit 实收数量=0 "
        f"调出总数量={total} 批次数={len(parsed)}"
    )
    return 0


def _run_transfer_query(args: argparse.Namespace) -> int:
    order_no = args.order.strip()
    if not order_no:
        print("调拨单号去首尾空白后不能为空", file=sys.stderr)
        return 1

    db_path = _db_path(args)
    if not db_path.exists():
        record = None
    else:
        with Ledger.open(db_path) as ledger:
            record = ledger.get_transfer(order_no)

    if record is None:
        print(f"调拨单号={order_no} 无此调拨单")
        return 0

    print(
        f"调拨单号={record.order_no} 来源仓={record.source_warehouse} "
        f"目标仓={record.target_warehouse} 商品={record.product} "
        f"状态={record.status} 实收数量={record.received_quantity} "
        f"批次数={len(record.lines)}"
    )
    for line in record.lines:
        print(f"批次号={line.lot} 调出数量={line.quantity}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) == 0:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)

    if args.command == "register":
        return _run_register(args)
    if args.command == "query":
        return _run_query(args)
    if args.command == "transfer":
        return _run_transfer(args)
    if args.command == "transfer-query":
        return _run_transfer_query(args)
    parser.print_help()
    return 0
