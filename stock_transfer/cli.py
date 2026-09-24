"""Command-line entry point."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .ledger import LEDGER_FILENAME, Ledger, LedgerError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock-transfer",
        description="Local 多仓库存调拨与批次追溯 ledger.",
        epilog=(
            "入库登记:\n"
            "  python3 -m stock_transfer register --warehouse WH-A --product SKU-1001 \\\n"
            "      --batch LOT-001,2024-03-01,2025-03-01,18 \\\n"
            "      --batch LOT-002,2024-04-02,2025-04-02,12\n"
            "批次查询:\n"
            "  python3 -m stock_transfer query --warehouse WH-A --product SKU-1001"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", metavar="{register,query}")

    register = subparsers.add_parser(
        "register",
        help="把入库批次登记到指定仓库（整次提交一次落账）",
        description=(
            "登记一次入库：--batch 可重复，每行格式为 "
            "批次号,生产日期(YYYY-MM-DD),有效期至(YYYY-MM-DD),数量(正整数)。"
            "任一批次行不合法则整次登记全部拒绝。"
        ),
    )
    register.add_argument("--warehouse", required=True, help="仓库代码")
    register.add_argument("--product", required=True, help="商品代码")
    register.add_argument(
        "--batch",
        required=True,
        action="append",
        metavar="批次号,生产日期,有效期至,数量",
        help="一个批次行，可重复以把总量拆成多个批次",
    )

    query = subparsers.add_parser(
        "query",
        help="查询指定仓库商品下每个批次的现存数量",
    )
    query.add_argument("--warehouse", required=True, help="仓库代码")
    query.add_argument("--product", required=True, help="商品代码")

    return parser


def _run_register(args: argparse.Namespace) -> int:
    ledger = Ledger(Path.cwd() / LEDGER_FILENAME)
    try:
        warehouse, product, batches = ledger.register(
            args.warehouse, args.product, args.batch
        )
    except LedgerError as exc:
        for row, field, reason in exc.errors:
            location = f"批次行 {row}: " if row else ""
            print(f"错误: {location}{field}: {reason}", file=sys.stderr)
        print("登记已拒绝：未写入任何批次记录", file=sys.stderr)
        return 1

    total = sum(batch.quantity for batch in batches)
    print("登记成功")
    print(f"仓库: {warehouse}")
    print(f"商品: {product}")
    print(f"本次总数量: {total}")
    print(f"批次数: {len(batches)}")
    return 0


def _run_query(args: argparse.Namespace) -> int:
    ledger = Ledger(Path.cwd() / LEDGER_FILENAME)
    records = ledger.query_batches(args.warehouse, args.product)
    payload = {
        "warehouse": args.warehouse.strip(),
        "product": args.product.strip(),
        "batches": [
            {
                "batch_no": record.batch_no,
                "production_date": record.production_date,
                "expiry_date": record.expiry_date,
                "quantity": record.quantity,
            }
            for record in records
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "register":
        return _run_register(args)
    if args.command == "query":
        return _run_query(args)

    parser.error(f"未知命令: {args.command}")
    return 2  # pragma: no cover - parser.error 以状态 2 退出


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
