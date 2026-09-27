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
    TRANSFER_IN_TRANSIT,
    BatchInput,
    Ledger,
    ReceivedItemInput,
    TransferError,
    TransferItemInput,
    TransferRecord,
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


def _validate_transfer_items(
    raw_lines: Sequence[str],
) -> tuple[list[tuple[int, TransferItemInput]], list[str]]:
    """Validate every ``批次号,调出数量`` allocation line.

    Returns (line number, item) pairs and per-line errors; callers must
    reject the whole order whenever the error list is non-empty.
    """
    parsed: list[tuple[int, TransferItemInput]] = []
    errors: list[str] = []
    seen_lots: dict[str, int] = {}

    for index, raw in enumerate(raw_lines, start=1):
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) != 2:
            errors.append(
                f"批次分配行 {index}: 批次分配行格式错误，应为"
                "“批次号,调出数量”，每行只接受本行调出数量，不接受汇总数量"
            )
            continue

        lot, quantity_raw = parts
        line_ok = True

        if not lot:
            errors.append(f"批次分配行 {index}: 批次号不能为空")
            line_ok = False

        if not quantity_raw.isdigit() or int(quantity_raw) <= 0:
            errors.append(
                f"批次分配行 {index}: 调出数量 {quantity_raw!r} 必须为正整数"
            )
            line_ok = False

        if not line_ok:
            continue

        item = TransferItemInput(lot, int(quantity_raw))
        parsed.append((index, item))
        first_line = seen_lots.get(lot)
        if first_line is None:
            seen_lots[lot] = index
        else:
            errors.append(
                f"批次分配行 {index}: 批次号 {lot} 与批次分配行 "
                f"{first_line} 重复"
            )

    return parsed, errors


def _validate_received_items(
    raw_lines: Sequence[str],
) -> tuple[list[tuple[int, ReceivedItemInput]], list[str]]:
    """Validate every ``批次号,实收数量`` receipt line.

    Returns (line number, item) pairs and per-line errors; callers must
    reject the whole receipt whenever the error list is non-empty.
    A received quantity of 0 means the lot never arrived.
    """
    parsed: list[tuple[int, ReceivedItemInput]] = []
    errors: list[str] = []
    seen_lots: dict[str, int] = {}

    for index, raw in enumerate(raw_lines, start=1):
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) != 2:
            errors.append(
                f"实收行 {index}: 实收行格式错误，应为"
                "“批次号,实收数量”，每行只接受本行实收数量，不接受汇总数量"
            )
            continue

        lot, quantity_raw = parts
        line_ok = True

        if not lot:
            errors.append(f"实收行 {index}: 批次号不能为空")
            line_ok = False

        if not quantity_raw.isdigit():
            errors.append(
                f"实收行 {index}: 实收数量 {quantity_raw!r} 必须为非负整数"
            )
            line_ok = False

        if not line_ok:
            continue

        item = ReceivedItemInput(lot, int(quantity_raw))
        parsed.append((index, item))
        first_line = seen_lots.get(lot)
        if first_line is None:
            seen_lots[lot] = index
        else:
            errors.append(
                f"实收行 {index}: 批次号 {lot} 与实收行 {first_line} 重复"
            )

    return parsed, errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-transfer",
        description=(
            "Local 多仓库存台账：库存登记、批次查询、调拨在途跟踪与收货确认。"
        ),
        epilog=(
            "示例：\n"
            "  python3 -m stock_transfer register --warehouse WH-A "
            "--product SKU-1001 \\\n"
            "      --batch LOT-2024-001,2024-03-01,2025-03-01,18 \\\n"
            "      --batch LOT-2024-002,2024-04-02,2025-04-02,12\n"
            "  python3 -m stock_transfer query --warehouse WH-A "
            "--product SKU-1001\n"
            "  python3 -m stock_transfer transfer --order TR-001 \\\n"
            "      --from WH-A --to WH-B --product SKU-1001 \\\n"
            "      --item LOT-2024-001,10\n"
            "  python3 -m stock_transfer receive --order TR-001 \\\n"
            "      --received LOT-2024-001,10\n"
            "  python3 -m stock_transfer transfer-query --order TR-001"
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
        help="提交调拨单：按批次把商品从来源仓调出到目标仓（在途）",
        description=(
            "整单一次落账；任一批次分配行不合法则整单拒绝、来源仓数量不变。"
            "批次分配行格式：批次号,调出数量（正整数，每行只写本行数量）。"
            "提交成功后单据状态为 in_transit、实收数量为 0，目标仓不立即收货。"
        ),
    )
    transfer.add_argument("--order", required=True, help="调拨单号（全部调拨单中唯一）")
    transfer.add_argument("--from", dest="source", required=True, help="来源仓代码")
    transfer.add_argument("--to", dest="target", required=True, help="目标仓代码")
    transfer.add_argument("--product", required=True, help="商品代码")
    transfer.add_argument(
        "--item",
        required=True,
        action="append",
        metavar="批次号,调出数量",
        help="批次分配行，可重复提供以在一单中调出多个批次",
    )
    transfer.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )

    transfer_query = subparsers.add_parser(
        "transfer-query",
        help="按调拨单号查询单据字段、各批次调出数量、状态与实收数量",
    )
    transfer_query.add_argument("--order", required=True, help="调拨单号")
    transfer_query.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )

    receive = subparsers.add_parser(
        "receive",
        help="收货确认：登记在途调拨单的实收情况并把商品记入目标仓",
        description=(
            "整次收货一次落账；任一实收行不合法则整单拒绝、各仓数量不变。"
            "实收行格式：批次号,实收数量（非负整数，0 表示该批次未收到货），"
            "必须逐一覆盖调拨单上的全部批次，不得遗漏或多出。"
            "确认成功后单据状态为 received，实收数量为本次实收总数，"
            "目标仓按批次入账实收数量。"
        ),
    )
    receive.add_argument(
        "--order", required=True, help="调拨单号（须为在途单据）"
    )
    receive.add_argument(
        "--received",
        required=True,
        action="append",
        metavar="批次号,实收数量",
        help="实收行，可重复提供；须逐一覆盖调拨单上的全部批次",
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
    order_no = args.order.strip()
    source = args.source.strip()
    target = args.target.strip()
    product = args.product.strip()
    if not order_no:
        print("调拨单号去首尾空白后不能为空", file=sys.stderr)
        return 1
    if not source:
        print("来源仓代码去首尾空白后不能为空", file=sys.stderr)
        return 1
    if not target:
        print("目标仓代码去首尾空白后不能为空", file=sys.stderr)
        return 1
    if not product:
        print("商品代码去首尾空白后不能为空", file=sys.stderr)
        return 1
    if source == target:
        print(
            f"目标仓 {target} 不得与来源仓 {source} 相同",
            file=sys.stderr,
        )
        return 1

    parsed, errors = _validate_transfer_items(args.item)
    db_path = _db_path(args)

    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return 1

    with Ledger.open(db_path) as ledger:
        if ledger.transfer_exists(order_no):
            errors.append(f"调拨单号 {order_no} 已存在，单号必须唯一")
        quantities = ledger.lot_quantities(
            source, product, [item.lot for _, item in parsed]
        )
        for line_number, item in parsed:
            available = quantities.get(item.lot)
            if available is None:
                errors.append(
                    f"批次分配行 {line_number}: 批次号 {item.lot} "
                    f"未在来源仓 {source} 商品 {product} 下落账"
                )
            elif item.quantity > available:
                errors.append(
                    f"批次分配行 {line_number}: 批次号 {item.lot} "
                    f"调出数量 {item.quantity} 超过现存数量 {available}"
                )

        if errors:
            for message in errors:
                print(message, file=sys.stderr)
            return 1

        try:
            ledger.create_transfer(
                order_no,
                source,
                target,
                product,
                [item for _, item in parsed],
            )
        except (TransferError, sqlite3.IntegrityError) as exc:
            print(f"调拨失败：{exc}", file=sys.stderr)
            return 1

    total = sum(item.quantity for _, item in parsed)
    print(
        f"调拨单提交成功：调拨单号={order_no} 来源仓={source} "
        f"目标仓={target} 商品={product} 总调出数量={total} "
        f"批次数={len(parsed)} 状态=in_transit 实收数量=0"
    )
    return 0


def _run_receive(args: argparse.Namespace) -> int:
    order_no = args.order.strip()
    if not order_no:
        print("调拨单号去首尾空白后不能为空", file=sys.stderr)
        return 1

    parsed, errors = _validate_received_items(args.received)
    db_path = _db_path(args)

    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return 1

    with Ledger.open(db_path) as ledger:
        record = ledger.get_transfer(order_no)
        if record is None:
            errors.append(f"调拨单号 {order_no} 不存在")
        elif record.status != TRANSFER_IN_TRANSIT:
            errors.append(
                f"调拨单号 {order_no} 当前状态为 {record.status}，"
                "只有在途单据可以收货确认"
            )
        else:
            shipped = {item.lot: item.quantity for item in record.items}
            received_lots = {item.lot for _, item in parsed}
            for lot in sorted(shipped.keys() - received_lots):
                errors.append(
                    f"批次号 {lot} 缺少对应实收行，"
                    "实收行必须逐一覆盖调拨单全部批次"
                )
            for line_number, item in parsed:
                if item.lot not in shipped:
                    errors.append(
                        f"实收行 {line_number}: 批次号 {item.lot} "
                        f"不在调拨单 {order_no} 的批次分配行中"
                    )
                elif item.quantity > shipped[item.lot]:
                    errors.append(
                        f"实收行 {line_number}: 批次号 {item.lot} "
                        f"实收数量 {item.quantity} "
                        f"大于调出数量 {shipped[item.lot]}"
                    )

        if errors:
            for message in errors:
                print(message, file=sys.stderr)
            return 1

        try:
            total = ledger.receive_transfer(
                order_no, [item for _, item in parsed]
            )
        except TransferError as exc:
            print(f"收货确认失败：{exc}", file=sys.stderr)
            return 1

    print(
        f"收货确认成功：调拨单号={order_no} 状态=received "
        f"实收数量={total} 批次数={len(parsed)}"
    )
    return 0


def _print_transfer(record: TransferRecord) -> None:
    print(
        f"调拨单号={record.order_no} 来源仓={record.source_warehouse} "
        f"目标仓={record.target_warehouse} 商品={record.product} "
        f"状态={record.status} 实收数量={record.received_quantity} "
        f"批次数={len(record.items)}"
    )
    for item in record.items:
        print(
            f"批次号={item.lot} 调出数量={item.quantity} "
            f"实收数量={item.received_quantity}"
        )


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
        # Empty result, still a successful lookup.
        print(f"调拨单号={order_no} 批次数=0")
        return 0

    _print_transfer(record)
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
    if args.command == "transfer-query":
        return _run_transfer_query(args)
    parser.print_help()
    return 0
