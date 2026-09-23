"""Command-line entry point."""

import argparse
from collections.abc import Sequence

from . import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stock-transfer",
        description="Local 多仓库存调拨与批次追溯 ledger.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0
