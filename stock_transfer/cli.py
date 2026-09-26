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
    STATUS_RECEIVED,
    STATUS_RECEIVED_WITH_DIFF,
    AdjustLine,
    BatchInput,
    Ledger,
    LedgerError,
    ResolveLine,
    ShipLine,
)
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DELTA_RE = re.compile(r"^[+-]?\d+$")


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
) -> tuple[list[tuple[int, ShipLine]], list[str]]:
    """Validate lines of the form ``批次号,数量``.

    Return (line number, ship line) pairs and errors; callers reject the
    whole command whenever the error list is non-empty.
    """
    parsed: list[tuple[int, ShipLine]] = []
    errors: list[str] = []
    seen_lots: dict[str, int] = {}

    for index, raw in enumerate(raw_lines, start=1):
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) != 2:
            errors.append(
                f"调拨行 {index}: 调拨行格式错误，应为“批次号,数量”"
            )
            continue

        lot, quantity_raw = parts
        line_ok = True

        if not lot:
            errors.append(f"调拨行 {index}: 批次号不能为空")
            line_ok = False

        if not quantity_raw.isdigit() or int(quantity_raw) <= 0:
            errors.append(f"调拨行 {index}: 数量 {quantity_raw!r} 必须为正整数")
            line_ok = False

        if not line_ok:
            continue

        ship_line = ShipLine(lot, int(quantity_raw))
        parsed.append((index, ship_line))
        first_line = seen_lots.get(lot)
        if first_line is None:
            seen_lots[lot] = index
        else:
            errors.append(
                f"调拨行 {index}: 批次号 {lot} 与调拨行 {first_line} 重复"
            )

    return parsed, errors


def _validate_resolve_lines(
    raw_lines: Sequence[str],
) -> tuple[list[tuple[int, ResolveLine]], list[str]]:
    """Validate lines of the form ``批次号,处理数量[,原因]``.

    Return (line number, resolve line) pairs and errors; callers reject the
    whole command whenever the error list is non-empty. A missing or blank
    reason is recorded as 未说明; more than three fields is a format error.
    """
    parsed: list[tuple[int, ResolveLine]] = []
    errors: list[str] = []
    seen_lots: dict[str, int] = {}

    for index, raw in enumerate(raw_lines, start=1):
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) < 2 or len(parts) > 3:
            errors.append(
                f"结清行 {index}: 结清行格式错误，应为“批次号,处理数量[,原因]”"
            )
            continue

        lot, quantity_raw = parts[0], parts[1]
        reason = parts[2] if len(parts) == 3 and parts[2] else "未说明"
        line_ok = True

        if not lot:
            errors.append(f"结清行 {index}: 批次号不能为空")
            line_ok = False

        if not quantity_raw.isdigit() or int(quantity_raw) <= 0:
            errors.append(f"结清行 {index}: 处理数量 {quantity_raw!r} 必须为正整数")
            line_ok = False

        if not line_ok:
            continue

        resolve_line = ResolveLine(lot, int(quantity_raw), reason)
        parsed.append((index, resolve_line))
        first_line = seen_lots.get(lot)
        if first_line is None:
            seen_lots[lot] = index
        else:
            errors.append(
                f"结清行 {index}: 批次号 {lot} 与结清行 {first_line} 重复"
            )

    return parsed, errors


def _validate_adjust_lines(
    raw_lines: Sequence[str],
) -> tuple[list[tuple[int, AdjustLine]], list[str]]:
    """Validate lines of the form ``批次号,数量变化[,原因]``.

    Return (line number, adjust line) pairs and errors; callers reject the
    whole command whenever the error list is non-empty. The quantity delta
    must be a non-zero integer; a missing or blank reason is recorded as
    未说明; more than three fields is a format error.
    """
    parsed: list[tuple[int, AdjustLine]] = []
    errors: list[str] = []
    seen_lots: dict[str, int] = {}

    for index, raw in enumerate(raw_lines, start=1):
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) < 2 or len(parts) > 3:
            errors.append(
                f"调整行 {index}: 调整行格式错误，应为“批次号,数量变化[,原因]”"
            )
            continue

        lot, delta_raw = parts[0], parts[1]
        reason = parts[2] if len(parts) == 3 and parts[2] else "未说明"
        line_ok = True

        if not lot:
            errors.append(f"调整行 {index}: 批次号不能为空")
            line_ok = False

        if not _DELTA_RE.match(delta_raw) or int(delta_raw) == 0:
            errors.append(
                f"调整行 {index}: 数量变化 {delta_raw!r} 必须为非零整数"
            )
            line_ok = False

        if not line_ok:
            continue

        adjust_line = AdjustLine(lot, int(delta_raw), reason)
        parsed.append((index, adjust_line))
        first_line = seen_lots.get(lot)
        if first_line is None:
            seen_lots[lot] = index
        else:
            errors.append(
                f"调整行 {index}: 批次号 {lot} 与调整行 {first_line} 重复"
            )

    return parsed, errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-transfer",
        description="Local 多仓库存台账：库存登记、批次查询、调拨提交、收货确认、调拨取消、差异结清、差异查询与盘点调整。",
        epilog=(
            "示例：\n"
            "  python3 -m stock_transfer register --warehouse WH-A "
            "--product SKU-1001 \\\n"
            "      --batch LOT-2024-001,2024-03-01,2025-03-01,18 \\\n"
            "      --batch LOT-2024-002,2024-04-02,2025-04-02,12\n"
            "  python3 -m stock_transfer query --warehouse WH-A "
            "--product SKU-1001\n"
            "  python3 -m stock_transfer ship --transfer TR-001 \\\n"
            "      --from WH-A --to WH-B --product SKU-1001 \\\n"
            "      --line LOT-2024-001,10\n"
            "  python3 -m stock_transfer receive --transfer TR-001 \\\n"
            "      --line LOT-2024-001,8\n"
            "  python3 -m stock_transfer cancel --transfer TR-001 "
            "--reason 客户撤单\n"
            "  python3 -m stock_transfer resolve --transfer TR-001 \\\n"
            "      --line LOT-2024-001,2,运输损耗\n"
            "  python3 -m stock_transfer diff --transfer TR-001\n"
            "  python3 -m stock_transfer adjust --warehouse WH-A \\\n"
            "      --product SKU-1001 \\\n"
            "      --line LOT-2024-001,-2,盘点损耗 \\\n"
            "      --line LOT-2024-002,3\n"
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

    ship = subparsers.add_parser(
        "ship",
        help="提交一张多仓调拨单（整单原子落账）",
        description=(
            "整单一次落账；任一批次不存在、数量超现存、批次号重复或调拨单号"
            "已存在则全部拒绝。调拨行格式：批次号,数量（正整数）。"
        ),
    )
    ship.add_argument(
        "--transfer", required=True, metavar="调拨单号", help="全局唯一调拨单号"
    )
    ship.add_argument(
        "--from",
        required=True,
        dest="source",
        metavar="发出仓",
        help="发出仓代码（去空白后非空，且不能与接收仓相同）",
    )
    ship.add_argument(
        "--to",
        required=True,
        dest="dest",
        metavar="接收仓",
        help="接收仓代码（去空白后非空，且不能与发出仓相同）",
    )
    ship.add_argument("--product", required=True, help="商品代码")
    ship.add_argument(
        "--line",
        required=True,
        action="append",
        metavar="批次号,数量",
        help="调拨行，可重复提供；批次号定位唯一存量批次，数量为正整数",
    )
    ship.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )

    receive = subparsers.add_parser(
        "receive",
        help="对状态为 shipped 的调拨单做收货确认",
        description=(
            "未给实收行时按发运数量全部收下（状态 received）；给了实收行则"
            "逐行核对，差异挂账并转为 received-with-diff。实收行格式："
            "批次号,数量（正整数）。"
        ),
    )
    receive.add_argument(
        "--transfer", required=True, metavar="调拨单号", help="待收货的调拨单号"
    )
    receive.add_argument(
        "--line",
        action="append",
        default=None,
        metavar="批次号,数量",
        help="可选实收行，可重复提供；缺省表示按发运数量全部实收",
    )
    receive.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )

    cancel = subparsers.add_parser(
        "cancel",
        help="取消状态为 shipped 的在途调拨单",
        description=(
            "仅状态为 shipped 的调拨单可取消。逐行把发运数量全额退回发出仓"
            "对应批次（接收仓数量保持不动），状态精确变为 canceled，"
            "各调拨行实收数量记为 0，差异不再挂账；任一行退回不合法则整次拒绝。"
        ),
    )
    cancel.add_argument(
        "--transfer", required=True, metavar="调拨单号", help="待取消的调拨单号"
    )
    cancel.add_argument(
        "--reason",
        default=None,
        metavar="原因",
        help="取消原因，去空白后不能为空（仅校验，不参与输出）",
    )
    cancel.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )

    resolve = subparsers.add_parser(
        "resolve",
        help="结清状态为 received-with-diff 的调拨单的差异",
        description=(
            "仅状态精确为 received-with-diff 的调拨单可结清差异。逐行核对："
            "批次号须在该单差异批次内且同一命令内不重复，处理数量为正整数且"
            "不超过该批次未结差异数量，否则整次拒绝、台账不变。全部结清后"
            "状态精确变为 resolved，部分结清仍为 received-with-diff。"
            "结清行格式：批次号,处理数量[,原因]（正整数；原因缺省记为 未说明）。"
        ),
    )
    resolve.add_argument(
        "--transfer", required=True, metavar="调拨单号", help="待结清差异的调拨单号"
    )
    resolve.add_argument(
        "--line",
        required=True,
        action="append",
        metavar="批次号,处理数量[,原因]",
        help="结清行，可重复提供；原因缺省或去空白后为空记为 未说明",
    )
    resolve.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )

    diff = subparsers.add_parser(
        "diff",
        help="查询调拨单按批次的差异明细（任意状态可查）",
        description=(
            "首行输出调拨单号、状态与未结差异总数；随后按调拨单内原行序"
            "输出每个未结差异不为 0 的批次：发运数量、实收数量、未结差异"
            "数量、已结清数量与最近一次结清原因（未结清过为 未说明）。"
            "canceled 调拨单的差异不再挂账，未结差异与已结清数量均按 0 输出。"
        ),
    )
    diff.add_argument(
        "--transfer", required=True, metavar="调拨单号", help="待查询的调拨单号"
    )
    diff.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )

    adjust = subparsers.add_parser(
        "adjust",
        help="盘点后直接修正指定仓库、商品下批次的现存数量（整次原子落账）",
        description=(
            "整次提交一次落账；任一行不合法则全部拒绝、台账不变。调整行格式："
            "批次号,数量变化[,原因]（数量变化为非零整数，正数增加、负数减少；"
            "减少后数量不能为负，减到 0 时该批次行从台账删除；原因缺省或去空白"
            "后为空记为 未说明；字段超过 3 个视为格式错误）。只改动批次现存"
            "数量，不影响任何调拨单的状态或差异数据。"
        ),
    )
    adjust.add_argument("--warehouse", required=True, help="仓库代码")
    adjust.add_argument("--product", required=True, help="商品代码")
    adjust.add_argument(
        "--line",
        required=True,
        action="append",
        metavar="批次号,数量变化[,原因]",
        help="调整行，可重复提供；批次号定位该仓库该商品下的现存批次行",
    )
    adjust.add_argument(
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


def _run_ship(args: argparse.Namespace) -> int:
    transfer_no = args.transfer.strip()
    source = args.source.strip()
    dest = args.dest.strip()
    product = args.product.strip()

    errors: list[str] = []
    if not transfer_no:
        errors.append("调拨单号去首尾空白后不能为空")
    if not source:
        errors.append("发出仓代码去首尾空白后不能为空")
    if not dest:
        errors.append("接收仓代码去首尾空白后不能为空")
    if source and dest and source == dest:
        errors.append(f"发出仓与接收仓不能相同（均为 {source}）")
    if not product:
        errors.append("商品代码去首尾空白后不能为空")

    parsed, line_errors = _validate_transfer_lines(args.line)
    errors.extend(line_errors)

    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return 1

    db_path = _db_path(args)
    try:
        with Ledger.open(db_path) as ledger:
            total = ledger.ship_transfer(
                transfer_no,
                source,
                dest,
                product,
                [line for _, line in parsed],
            )
    except LedgerError as exc:
        print(f"调拨提交失败：{exc}", file=sys.stderr)
        return 1

    print(f"调拨提交成功：调拨单号={transfer_no} 总数量={total}")
    return 0


def _run_receive(args: argparse.Namespace) -> int:
    transfer_no = args.transfer.strip()
    if not transfer_no:
        print("调拨单号去首尾空白后不能为空", file=sys.stderr)
        return 1

    received: dict[str, int] | None = None
    if args.line:
        parsed, errors = _validate_transfer_lines(args.line)
        if errors:
            for message in errors:
                print(message, file=sys.stderr)
            return 1
        received = {line.lot: line.quantity for _, line in parsed}

    db_path = _db_path(args)
    try:
        with Ledger.open(db_path) as ledger:
            status, diff_total = ledger.confirm_receipt(transfer_no, received)
    except LedgerError as exc:
        print(f"收货确认失败：{exc}", file=sys.stderr)
        return 1

    if status == STATUS_RECEIVED:
        print(f"收货确认成功：调拨单号={transfer_no} 状态={status}")
    else:
        print(
            f"收货确认成功：调拨单号={transfer_no} 状态={status} "
            f"差异总数={diff_total}"
        )
    return 0


def _run_cancel(args: argparse.Namespace) -> int:
    transfer_no = args.transfer.strip()
    if not transfer_no:
        print("调拨单号去首尾空白后不能为空", file=sys.stderr)
        return 1

    if args.reason is not None and not args.reason.strip():
        print("取消原因去首尾空白后不能为空", file=sys.stderr)
        return 1

    db_path = _db_path(args)
    try:
        with Ledger.open(db_path) as ledger:
            total = ledger.cancel_transfer(transfer_no)
    except LedgerError as exc:
        print(f"调拨取消失败：{exc}", file=sys.stderr)
        return 1

    print(f"取消成功：调拨单号={transfer_no} 退回总数={total}")
    return 0


def _run_resolve(args: argparse.Namespace) -> int:
    transfer_no = args.transfer.strip()
    if not transfer_no:
        print("调拨单号去首尾空白后不能为空", file=sys.stderr)
        return 1

    parsed, errors = _validate_resolve_lines(args.line)
    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return 1

    db_path = _db_path(args)
    try:
        with Ledger.open(db_path) as ledger:
            total, status = ledger.resolve_diff(
                transfer_no, [line for _, line in parsed]
            )
    except LedgerError as exc:
        print(f"差异结清失败：{exc}", file=sys.stderr)
        return 1

    print(f"差异结清成功：调拨单号={transfer_no} 结清数量={total} 状态={status}")
    return 0


def _run_diff(args: argparse.Namespace) -> int:
    transfer_no = args.transfer.strip()
    if not transfer_no:
        print("调拨单号去首尾空白后不能为空", file=sys.stderr)
        return 1

    db_path = _db_path(args)
    if not db_path.exists():
        print(f"差异查询失败：调拨单 {transfer_no} 不存在", file=sys.stderr)
        return 1
    try:
        with Ledger.open(db_path) as ledger:
            status, lines = ledger.diff_report(transfer_no)
    except LedgerError as exc:
        print(f"差异查询失败：{exc}", file=sys.stderr)
        return 1

    pending_total = sum(line.pending_quantity for line in lines)
    print(f"调拨单号={transfer_no} 状态={status} 未结差异总数={pending_total}")
    for line in lines:
        if line.pending_quantity == 0:
            continue
        print(
            f"批次号={line.lot} 发运数量={line.shipped_quantity} "
            f"实收数量={line.received_quantity} "
            f"未结差异数量={line.pending_quantity} "
            f"已结清数量={line.resolved_quantity} 原因={line.reason}"
        )
    return 0


def _run_adjust(args: argparse.Namespace) -> int:
    warehouse = args.warehouse.strip()
    product = args.product.strip()

    errors: list[str] = []
    if not warehouse:
        errors.append("仓库代码去首尾空白后不能为空")
    if not product:
        errors.append("商品代码去首尾空白后不能为空")

    parsed, line_errors = _validate_adjust_lines(args.line)
    errors.extend(line_errors)

    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return 1

    db_path = _db_path(args)
    try:
        with Ledger.open(db_path) as ledger:
            net = ledger.adjust_batches(
                warehouse, product, [line for _, line in parsed]
            )
    except LedgerError as exc:
        print(f"盘点调整失败：{exc}", file=sys.stderr)
        return 1

    print(
        f"盘点调整成功：仓库={warehouse} 商品={product} "
        f"调整批次={len(parsed)} 净变化={net}"
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
    if args.command == "ship":
        return _run_ship(args)
    if args.command == "receive":
        return _run_receive(args)
    if args.command == "cancel":
        return _run_cancel(args)
    if args.command == "resolve":
        return _run_resolve(args)
    if args.command == "diff":
        return _run_diff(args)
    if args.command == "adjust":
        return _run_adjust(args)
    parser.print_help()
    return 0
