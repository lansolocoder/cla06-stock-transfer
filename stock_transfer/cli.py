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
    STATUS_CANCELED,
    STATUS_RECEIVED,
    STATUS_SHIPPED,
    BatchInput,
    Ledger,
    LedgerError,
    ShipLine,
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
    raw_lines: Sequence[str], line_label: str = "调拨行"
) -> tuple[list[tuple[int, ShipLine]], list[str]]:
    """Validate lines of the form ``批次号,数量``.

    Return (line number, ship line) pairs and errors; callers reject the
    whole command whenever the error list is non-empty. *line_label* names
    the line kind in error messages (调拨行 / 处理行).
    """
    parsed: list[tuple[int, ShipLine]] = []
    errors: list[str] = []
    seen_lots: dict[str, int] = {}

    for index, raw in enumerate(raw_lines, start=1):
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) != 2:
            errors.append(
                f"{line_label} {index}: {line_label}格式错误，应为“批次号,数量”"
            )
            continue

        lot, quantity_raw = parts
        line_ok = True

        if not lot:
            errors.append(f"{line_label} {index}: 批次号不能为空")
            line_ok = False

        if not quantity_raw.isdigit() or int(quantity_raw) <= 0:
            errors.append(f"{line_label} {index}: 数量 {quantity_raw!r} 必须为正整数")
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
                f"{line_label} {index}: 批次号 {lot} 与{line_label} "
                f"{first_line} 重复"
            )

    return parsed, errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-transfer",
        description="Local 多仓库存台账：库存登记、批次查询、调拨提交、收货确认、调拨取消与差异处理。",
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
            "  python3 -m stock_transfer resolve --transfer TR-001 "
            "--line LOT-2024-001,2\n"
            "  python3 -m stock_transfer detail --transfer TR-001\n"
            "  python3 -m stock_transfer merge-batch --warehouse WH-A \\\n"
            "      --product SKU-1001 --target LOT-2024-001 --source LOT-2024-002\n"
            "  python3 -m stock_transfer split-batch --warehouse WH-A \\\n"
            "      --product SKU-1001 --batch LOT-2024-001 \\\n"
            "      --new-batch LOT-2024-003 --qty 5"
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
        help="对 received/received-with-diff 调拨单做差异处理结案",
        description=(
            "对状态为 received 或 received-with-diff 的调拨单按批次逐行给出"
            "差异的最终处置。处理行格式：批次号,数量（正整数，不超过该批次当前"
            "挂账差异量），批次号必须在调拨单内且不重复；不给 --line 时按各"
            "批次当前全部挂账差异结案。挂账清零后状态精确变为 resolved；尚有"
            "批次未结案则状态仍为 received-with-diff。各仓现存数量与各调拨行"
            "实收数量均不改变。"
        ),
    )
    resolve.add_argument(
        "--transfer",
        required=True,
        metavar="调拨单号",
        help="待差异处理的调拨单号（状态须为 received 或 received-with-diff）",
    )
    resolve.add_argument(
        "--line",
        action="append",
        default=None,
        metavar="批次号,数量",
        help="差异处理行，可重复提供；缺省表示各批次当前全部挂账差异结案",
    )
    resolve.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )

    detail = subparsers.add_parser(
        "detail",
        help="逐行查询某调拨单的发运、实收、挂账差异与结案情况",
        description=(
            "只读查询，不修改任何台账数据。按调拨行原始顺序逐行输出"
            "状态、批次号、发运数量、实收数量、挂账差异与已结案数量；"
            "未确认收货的调拨行实收数量显示为“未收货”。"
        ),
    )
    detail.add_argument(
        "--transfer", required=True, metavar="调拨单号", help="待查询的调拨单号"
    )
    detail.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )

    merge_batch = subparsers.add_parser(
        "merge-batch",
        help="把同仓同商品下的来源批次并入目标批次",
        description=(
            "两个批次必须都存在于该仓该商品下且批次号去空白后非空、不相同。"
            "合并后来源批次行删除，数量并入目标批次行，生产日期与有效期至均"
            "取两者较早日期；整次原子落账，任一校验失败则台账不变。"
        ),
    )
    merge_batch.add_argument("--warehouse", required=True, help="仓库代码")
    merge_batch.add_argument("--product", required=True, help="商品代码")
    merge_batch.add_argument(
        "--target", required=True, metavar="目标批次号", help="并入数量的目标批次号"
    )
    merge_batch.add_argument(
        "--source", required=True, metavar="来源批次号", help="被合并删除的来源批次号"
    )
    merge_batch.add_argument(
        "--db",
        default=None,
        help="台账数据文件路径（默认当前工作目录下的 stock_ledger.db）",
    )

    split_batch = subparsers.add_parser(
        "split-batch",
        help="把同仓同商品下的某批次按数量拆出一个新批次",
        description=(
            "数量必须为正整数且小于该批次现存数量；新批次号去空白后非空、"
            "不与该仓该商品下任何现有批次号相同。拆分后原批次行数量减少，"
            "追加新批次行（生产日期与有效期至沿用原批次）；整次原子落账，"
            "任一校验失败则台账不变。"
        ),
    )
    split_batch.add_argument("--warehouse", required=True, help="仓库代码")
    split_batch.add_argument("--product", required=True, help="商品代码")
    split_batch.add_argument(
        "--batch", required=True, metavar="批次号", help="待拆分的原批次号"
    )
    split_batch.add_argument(
        "--new-batch",
        required=True,
        dest="new_batch",
        metavar="新批次号",
        help="拆分产生的新批次号（该仓该商品下不得已存在）",
    )
    split_batch.add_argument(
        "--qty",
        required=True,
        metavar="数量",
        help="拆分数量（正整数，且小于原批次现存数量）",
    )
    split_batch.add_argument(
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

    resolutions: dict[str, int] | None = None
    if args.line:
        parsed, errors = _validate_transfer_lines(args.line, line_label="处理行")
        if errors:
            for message in errors:
                print(message, file=sys.stderr)
            return 1
        resolutions = {line.lot: line.quantity for _, line in parsed}

    db_path = _db_path(args)
    try:
        with Ledger.open(db_path) as ledger:
            status, resolved_total = ledger.resolve_differences(
                transfer_no, resolutions
            )
    except LedgerError as exc:
        print(f"差异处理失败：{exc}", file=sys.stderr)
        return 1

    print(
        f"差异处理成功：调拨单号={transfer_no} 结案总数={resolved_total}"
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


def _run_detail(args: argparse.Namespace) -> int:
    transfer_no = args.transfer.strip()
    if not transfer_no:
        print("调拨单号去首尾空白后不能为空", file=sys.stderr)
        return 1

    db_path = _db_path(args)
    if not db_path.exists():
        print(f"调拨单 {transfer_no} 不存在", file=sys.stderr)
        return 1

    with Ledger.open(db_path) as ledger:
        detail = ledger.get_transfer_detail(transfer_no)
    if detail is None:
        print(f"调拨单 {transfer_no} 不存在", file=sys.stderr)
        return 1

    status, lines = detail
    for line in lines:
        # shipped 与 canceled 的调拨行从未确认收货（取消时实收数量记 0
        # 只是账面回退，并不构成收货）：显示“未收货”，差异与结案均为 0。
        if status in (STATUS_SHIPPED, STATUS_CANCELED):
            received_text = "未收货"
            pending_diff = 0
            resolved = 0
        else:
            received_text = str(line.received_quantity)
            resolved = line.resolved_quantity
            pending_diff = (
                line.shipped_quantity - line.received_quantity - resolved
            )
        print(
            f"调拨单号={transfer_no} 状态={status} 批次号={line.lot} "
            f"发运数量={line.shipped_quantity} 实收数量={received_text} "
            f"挂账差异={pending_diff} 已结案={resolved}"
        )
    return 0


def _run_merge_batch(args: argparse.Namespace) -> int:
    warehouse = args.warehouse.strip()
    product = args.product.strip()
    target = args.target.strip()
    source = args.source.strip()

    errors: list[str] = []
    if not warehouse:
        errors.append("仓库代码去首尾空白后不能为空")
    if not product:
        errors.append("商品代码去首尾空白后不能为空")
    if not target:
        errors.append("目标批次号去首尾空白后不能为空")
    if not source:
        errors.append("来源批次号去首尾空白后不能为空")
    if target and source and target == source:
        errors.append(f"目标批次号与来源批次号不能相同（均为 {target}）")
    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return 1

    db_path = _db_path(args)
    try:
        with Ledger.open(db_path) as ledger:
            merged = ledger.merge_batches(warehouse, product, target, source)
    except LedgerError as exc:
        print(f"批次合并失败：{exc}", file=sys.stderr)
        return 1

    print(f"合并成功：目标批次={target} 来源批次={source} 合并数量={merged}")
    return 0


def _run_split_batch(args: argparse.Namespace) -> int:
    warehouse = args.warehouse.strip()
    product = args.product.strip()
    lot = args.batch.strip()
    new_lot = args.new_batch.strip()
    quantity_raw = args.qty.strip()

    errors: list[str] = []
    if not warehouse:
        errors.append("仓库代码去首尾空白后不能为空")
    if not product:
        errors.append("商品代码去首尾空白后不能为空")
    if not lot:
        errors.append("批次号去首尾空白后不能为空")
    if not new_lot:
        errors.append("新批次号去首尾空白后不能为空")
    if not quantity_raw.isdigit() or int(quantity_raw) <= 0:
        errors.append(f"拆分数量 {args.qty!r} 必须为正整数")
    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return 1

    db_path = _db_path(args)
    try:
        with Ledger.open(db_path) as ledger:
            ledger.split_batch(warehouse, product, lot, new_lot, int(quantity_raw))
    except LedgerError as exc:
        print(f"批次拆分失败：{exc}", file=sys.stderr)
        return 1

    print(f"拆分成功：原批次={lot} 新批次={new_lot} 拆分数量={int(quantity_raw)}")
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
    if args.command == "detail":
        return _run_detail(args)
    if args.command == "merge-batch":
        return _run_merge_batch(args)
    if args.command == "split-batch":
        return _run_split_batch(args)
    parser.print_help()
    return 0
