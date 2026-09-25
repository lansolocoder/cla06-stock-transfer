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
    TRANSFER_STATUS_RECEIVED,
    TRANSFER_STATUS_RECEIVED_WITH_DIFF,
    TRANSFER_STATUS_SHIPPED,
    BatchInput,
    Ledger,
    ReceiptLine,
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


def _parse_quantity_lines(
    raw_lines: Sequence[str], label: str
) -> tuple[list[tuple[int, str, int]], list[str]]:
    """Validate ``批次号,数量`` lines; return (line, lot, quantity) and errors.

    *label* is the row kind used in error messages (调拨行 / 实收行).
    Field-level errors are collected per line; callers must reject the whole
    submission whenever the error list is non-empty.
    """
    parsed: list[tuple[int, str, int]] = []
    errors: list[str] = []
    seen_lots: dict[str, int] = {}

    for index, raw in enumerate(raw_lines, start=1):
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) != 2:
            errors.append(
                f"{label} {index}: 行格式错误，应为“批次号,数量”"
            )
            continue

        lot, quantity_raw = parts
        line_ok = True

        if not lot:
            errors.append(f"{label} {index}: 批次号不能为空")
            line_ok = False

        if not quantity_raw.isdigit() or int(quantity_raw) <= 0:
            errors.append(f"{label} {index}: 数量 {quantity_raw!r} 必须为正整数")
            line_ok = False

        if not line_ok:
            continue

        quantity = int(quantity_raw)
        parsed.append((index, lot, quantity))
        first_line = seen_lots.get(lot)
        if first_line is None:
            seen_lots[lot] = index
        else:
            errors.append(
                f"{label} {index}: 批次号 {lot} 与{label} {first_line} 重复"
            )

    return parsed, errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-transfer",
        description="Local 多仓库存台账：库存登记、批次查询、调拨提交与收货确认。",
        epilog=(
            "示例：\n"
            "  python3 -m stock_transfer register --warehouse WH-A "
            "--product SKU-1001 \\\n"
            "      --batch LOT-2024-001,2024-03-01,2025-03-01,18 \\\n"
            "      --batch LOT-2024-002,2024-04-02,2025-04-02,12\n"
            "  python3 -m stock_transfer query --warehouse WH-A "
            "--product SKU-1001\n"
            "  python3 -m stock_transfer transfer --transfer-no TR-2024-001 \\\n"
            "      --from-warehouse WH-A --to-warehouse WH-B "
            "--product SKU-1001 \\\n"
            "      --line LOT-2024-001,6 --line LOT-2024-002,4\n"
            "  python3 -m stock_transfer receive "
            "--transfer-no TR-2024-001 --line LOT-2024-001,6"
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
        help="提交调拨单：发出仓批次立即扣减，接收仓同批次立即入账",
        description=(
            "整单原子落账；任一调拨行不合法则整单拒绝。"
            "调拨行格式：批次号,数量（正整数），可重复提供。"
            "提交成功后调拨单状态为 shipped。"
        ),
    )
    transfer.add_argument(
        "--transfer-no", required=True, help="调拨单号（台账内全局唯一）"
    )
    transfer.add_argument("--from-warehouse", required=True, help="发出仓代码")
    transfer.add_argument("--to-warehouse", required=True, help="接收仓代码")
    transfer.add_argument("--product", required=True, help="商品代码")
    transfer.add_argument(
        "--line",
        required=True,
        action="append",
        metavar="批次号,数量",
        help="调拨行，可重复提供以在一单中调拨多个批次",
    )
    transfer.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )

    receive = subparsers.add_parser(
        "receive",
        help="确认收货：核销一张已发运（shipped）的调拨单",
        description=(
            "仅状态为 shipped 的调拨单可确认。不提供实收行时按发运数量全部"
            "收下，状态变为 received；提供实收行（批次号,数量，可重复）时"
            "按行核对，发运与实收之差记为差异数量留作后续处理，状态变为 "
            "received-with-diff。"
        ),
    )
    receive.add_argument("--transfer-no", required=True, help="调拨单号")
    receive.add_argument(
        "--line",
        action="append",
        default=None,
        metavar="批次号,数量",
        help="实收行，可重复提供；不提供则按发运数量全部收下",
    )
    receive.add_argument(
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
    transfer_no = args.transfer_no.strip()
    from_warehouse = args.from_warehouse.strip()
    to_warehouse = args.to_warehouse.strip()
    product = args.product.strip()
    if not transfer_no:
        print("调拨单号去首尾空白后不能为空", file=sys.stderr)
        return 1
    if not from_warehouse:
        print("发出仓代码去首尾空白后不能为空", file=sys.stderr)
        return 1
    if not to_warehouse:
        print("接收仓代码去首尾空白后不能为空", file=sys.stderr)
        return 1
    if from_warehouse == to_warehouse:
        print("发出仓与接收仓不能相同", file=sys.stderr)
        return 1
    if not product:
        print("商品代码去首尾空白后不能为空", file=sys.stderr)
        return 1

    parsed, errors = _parse_quantity_lines(args.line, "调拨行")
    db_path = _db_path(args)

    with Ledger.open(db_path) as ledger:
        if parsed:
            if ledger.get_transfer(transfer_no) is not None:
                errors.append(f"调拨单号 {transfer_no} 已存在")
            for line_number, lot, quantity in parsed:
                batch = ledger.get_batch(from_warehouse, product, lot)
                if batch is None:
                    errors.append(
                        f"调拨行 {line_number}: 批次号 {lot} 不在仓库 "
                        f"{from_warehouse} 商品 {product} 的台账中"
                    )
                elif quantity > batch.quantity:
                    errors.append(
                        f"调拨行 {line_number}: 批次 {lot} 现存数量 "
                        f"{batch.quantity}，不足调出 {quantity}"
                    )

        if errors:
            for message in errors:
                print(message, file=sys.stderr)
            return 1

        lines = [
            TransferLineInput(lot, quantity) for _, lot, quantity in parsed
        ]
        try:
            ledger.create_transfer(
                transfer_no, from_warehouse, to_warehouse, product, lines
            )
        except sqlite3.IntegrityError:
            print(
                f"调拨提交失败：调拨单号 {transfer_no} 与台账中已有记录冲突",
                file=sys.stderr,
            )
            return 1

    total = sum(line.quantity for line in lines)
    print(
        f"调拨提交成功：调拨单号={transfer_no} 发出仓={from_warehouse} "
        f"接收仓={to_warehouse} 商品={product} 总数量={total} "
        f"调拨行数={len(lines)}"
    )
    return 0


def _run_receive(args: argparse.Namespace) -> int:
    transfer_no = args.transfer_no.strip()
    if not transfer_no:
        print("调拨单号去首尾空白后不能为空", file=sys.stderr)
        return 1

    db_path = _db_path(args)
    if not db_path.exists():
        print(f"调拨单 {transfer_no} 不存在", file=sys.stderr)
        return 1

    with Ledger.open(db_path) as ledger:
        transfer = ledger.get_transfer(transfer_no)
        if transfer is None:
            print(f"调拨单 {transfer_no} 不存在", file=sys.stderr)
            return 1
        if transfer.status != TRANSFER_STATUS_SHIPPED:
            print(
                f"调拨单 {transfer_no} 状态为 {transfer.status}，不能确认收货",
                file=sys.stderr,
            )
            return 1

        shipped = {
            line.lot: line.shipped_quantity
            for line in ledger.list_transfer_lines(transfer_no)
        }

        if args.line is None:
            receipts = [
                ReceiptLine(lot, quantity, 0)
                for lot, quantity in shipped.items()
            ]
            status = TRANSFER_STATUS_RECEIVED
        else:
            parsed, errors = _parse_quantity_lines(args.line, "实收行")
            for line_number, lot, quantity in parsed:
                if lot not in shipped:
                    errors.append(
                        f"实收行 {line_number}: 批次号 {lot} "
                        f"不在调拨单 {transfer_no} 中"
                    )
                elif quantity > shipped[lot]:
                    errors.append(
                        f"实收行 {line_number}: 批次 {lot} 实收数量 "
                        f"{quantity} 超过发运数量 {shipped[lot]}"
                    )
            if errors:
                for message in errors:
                    print(message, file=sys.stderr)
                return 1
            received = {lot: quantity for _, lot, quantity in parsed}
            receipts = [
                ReceiptLine(
                    lot,
                    received.get(lot, 0),
                    quantity - received.get(lot, 0),
                )
                for lot, quantity in shipped.items()
            ]
            status = TRANSFER_STATUS_RECEIVED_WITH_DIFF

        ledger.confirm_transfer(transfer_no, status, receipts)

    if status == TRANSFER_STATUS_RECEIVED:
        print(f"收货确认成功：调拨单号={transfer_no} 状态={status}")
    else:
        total_diff = sum(line.diff_quantity for line in receipts)
        print(
            f"收货确认成功：调拨单号={transfer_no} 状态={status} "
            f"差异总数={total_diff}"
        )
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
    if args.command == "receive":
        return _run_receive(args)
    parser.print_help()
    return 0
