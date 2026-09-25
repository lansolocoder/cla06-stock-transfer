"""Checks for the documented command-line entry point."""

import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONPATH": str(ROOT)}


class CommandLineTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "stock_transfer", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=ENV,
        )

    def test_help_and_no_arguments(self) -> None:
        for arguments in [(), ("--help",)]:
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--help", result.stdout)
                self.assertIn("--version", result.stdout)
                self.assertEqual(result.stderr, "")

    def test_version(self) -> None:
        result = self.invoke("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "stock-transfer 0.1.0")
        self.assertEqual(result.stderr, "")

    def test_unknown_argument_is_an_error(self) -> None:
        result = self.invoke("--unknown-option")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--unknown-option", result.stderr)
        self.assertEqual(result.stdout, "")


class LedgerCommandTests(unittest.TestCase):
    def invoke_in(self, cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "stock_transfer", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=ENV,
        )

    def test_register_then_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = self.invoke_in(
                cwd,
                "register", "--warehouse", "WH-A", "--product", "SKU-1001",
                "--batch", "LOT-2024-001,2024-03-01,2025-03-01,18",
                "--batch", "LOT-2024-002,2024-04-02,2025-04-02,12",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("仓库=WH-A", result.stdout)
            self.assertIn("商品=SKU-1001", result.stdout)
            self.assertIn("总数量=30", result.stdout)
            self.assertIn("批次数=2", result.stdout)

            # Data persists in a separate process invocation.
            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A", "--product", "SKU-1001"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("批次总数=2", result.stdout)
            self.assertIn("LOT-2024-001", result.stdout)
            self.assertIn("2024-03-01", result.stdout)
            self.assertIn("2025-03-01", result.stdout)
            self.assertIn("数量=18", result.stdout)
            self.assertIn("数量=12", result.stdout)

            self.assertTrue((cwd / "stock_ledger.db").exists())

    def test_query_empty_returns_empty_list_with_zero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-X", "--product", "SKU-X"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("批次总数=0", result.stdout)
            self.assertFalse((cwd / "stock_ledger.db").exists())

    def test_duplicate_lot_in_same_submission_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = self.invoke_in(
                cwd,
                "register", "--warehouse", "WH-A", "--product", "SKU-1001",
                "--batch", "LOT-1,2024-01-01,2025-01-01,5",
                "--batch", "LOT-1,2024-02-01,2025-02-01,7",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("批次行 2", result.stderr)
            self.assertIn("LOT-1", result.stderr)
            self.assertEqual(result.stdout, "")

            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A", "--product", "SKU-1001"
            )
            self.assertIn("批次总数=0", result.stdout)

    def test_duplicate_lot_against_ledger_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.invoke_in(
                cwd,
                "register", "--warehouse", "WH-A", "--product", "SKU-1001",
                "--batch", "LOT-1,2024-01-01,2025-01-01,5",
            )
            result = self.invoke_in(
                cwd,
                "register", "--warehouse", "WH-A", "--product", "SKU-1001",
                "--batch", "LOT-2,2024-02-01,2025-02-01,6",
                "--batch", "LOT-1,2024-03-01,2025-03-01,7",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("批次行 2", result.stderr)
            self.assertIn("LOT-1", result.stderr)

            # The valid row of the rejected submission must not be partially booked.
            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A", "--product", "SKU-1001"
            )
            self.assertIn("批次总数=1", result.stdout)
            self.assertNotIn("LOT-2", result.stdout)

    def test_invalid_batch_lines_rejected_as_a_whole(self) -> None:
        cases = [
            ("LOT-1,not-a-date,2025-01-01,5", "批次行 1", "生产日期"),
            ("LOT-1,2024-01-01,2025-13-40,5", "批次行 1", "有效期至"),
            ("LOT-1,2024-01-02,2024-01-01,5", "批次行 1", "必须晚于"),
            ("LOT-1,2024-01-01,2025-01-01,0", "批次行 1", "正整数"),
            ("LOT-1,2024-01-01,2025-01-01,-3", "批次行 1", "正整数"),
            ("LOT-1,2024-01-01,2025-01-01,abc", "批次行 1", "正整数"),
            ("LOT-1,2024-01-01,2025-01-01", "批次行 1", "格式"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            for batch_line, line_label, field_hint in cases:
                with self.subTest(batch_line=batch_line):
                    result = self.invoke_in(
                        cwd,
                        "register", "--warehouse", "WH-A", "--product", "SKU-1001",
                        "--batch", batch_line,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(line_label, result.stderr)
                    self.assertIn(field_hint, result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_one_bad_line_rejects_whole_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = self.invoke_in(
                cwd,
                "register", "--warehouse", "WH-A", "--product", "SKU-1001",
                "--batch", "LOT-1,2024-01-01,2025-01-01,5",
                "--batch", "LOT-2,2024-01-01,2025-01-01,0",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("批次行 2", result.stderr)

            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A", "--product", "SKU-1001"
            )
            self.assertIn("批次总数=0", result.stdout)

    def test_blank_codes_and_lots_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = self.invoke_in(
                cwd,
                "register", "--warehouse", "  ", "--product", "SKU-1001",
                "--batch", "LOT-1,2024-01-01,2025-01-01,5",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("仓库代码", result.stderr)

            result = self.invoke_in(
                cwd,
                "register", "--warehouse", "WH-A", "--product", " ",
                "--batch", "LOT-1,2024-01-01,2025-01-01,5",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("商品代码", result.stderr)

            result = self.invoke_in(
                cwd,
                "register", "--warehouse", "WH-A", "--product", "SKU-1001",
                "--batch", "  ,2024-01-01,2025-01-01,5",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("批次行 1", result.stderr)
            self.assertIn("批次号", result.stderr)

    def test_same_lot_allowed_under_different_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            for warehouse, product in [
                ("WH-A", "SKU-1001"),
                ("WH-A", "SKU-1002"),
                ("WH-B", "SKU-1001"),
            ]:
                result = self.invoke_in(
                    cwd,
                    "register", "--warehouse", warehouse, "--product", product,
                    "--batch", "LOT-1,2024-01-01,2025-01-01,5",
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_whitespace_is_trimmed_from_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = self.invoke_in(
                cwd,
                "register", "--warehouse", " WH-A ", "--product", " SKU-1001 ",
                "--batch", " LOT-1 , 2024-01-01 , 2025-01-01 , 5 ",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A", "--product", "SKU-1001"
            )
            self.assertIn("批次号=LOT-1", result.stdout)
            self.assertIn("数量=5", result.stdout)

    def test_help_mentions_register_and_query(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "stock_transfer", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=ENV,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("register", result.stdout)
        self.assertIn("query", result.stdout)


class TransferCommandTests(unittest.TestCase):
    def invoke_in(self, cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "stock_transfer", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=ENV,
        )

    def register(self, cwd: Path, warehouse: str, *batches: str) -> None:
        arguments = [
            "register", "--warehouse", warehouse, "--product", "SKU-1",
        ]
        for batch in batches:
            arguments += ["--batch", batch]
        result = self.invoke_in(cwd, *arguments)
        self.assertEqual(result.returncode, 0, result.stderr)

    def query_quantities(self, cwd: Path, warehouse: str) -> dict[str, int]:
        result = self.invoke_in(
            cwd, "query", "--warehouse", warehouse, "--product", "SKU-1"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        quantities: dict[str, int] = {}
        for line in result.stdout.splitlines():
            match = re.match(r"批次号=(\S+) .*数量=(\d+)$", line)
            if match:
                quantities[match[1]] = int(match[2])
        return quantities

    def ship(self, cwd: Path, transfer: str, *lines: str,
             source: str = "WH-A", dest: str = "WH-B") -> subprocess.CompletedProcess[str]:
        arguments = [
            "ship", "--transfer", transfer,
            "--from", source, "--to", dest, "--product", "SKU-1",
        ]
        for line in lines:
            arguments += ["--line", line]
        return self.invoke_in(cwd, *arguments)

    def receive(self, cwd: Path, transfer: str, *lines: str) -> subprocess.CompletedProcess[str]:
        arguments = ["receive", "--transfer", transfer]
        for line in lines:
            arguments += ["--line", line]
        return self.invoke_in(cwd, *arguments)

    def cancel(self, cwd: Path, transfer: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.invoke_in(cwd, "cancel", "--transfer", transfer, *extra)

    def test_ship_moves_quantities_between_warehouses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,18",
                          "LOT-2,2024-04-02,2025-04-02,12")
            self.register(cwd, "WH-B", "LOT-1,2023-01-01,2024-01-01,5")

            result = self.ship(cwd, "TR-1", "LOT-1,10", "LOT-2,12")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("TR-1", result.stdout)
            self.assertIn("22", result.stdout)

            # LOT-2 fully shipped: its source row is gone.
            self.assertEqual(self.query_quantities(cwd, "WH-A"), {"LOT-1": 8})
            # Existing dest lot accumulates; new lot keeps the source dates.
            self.assertEqual(
                self.query_quantities(cwd, "WH-B"), {"LOT-1": 15, "LOT-2": 12}
            )
            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-B", "--product", "SKU-1"
            )
            self.assertIn("批次号=LOT-2 生产日期=2024-04-02 有效期至=2025-04-02",
                          result.stdout)

    def test_ship_rejections_leave_ledger_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10")
            before = self.query_quantities(cwd, "WH-A")

            cases = [
                # Unknown lot.
                ("TR-1", ("LOT-X,1",), "不存在"),
                # Quantity exceeds what the lot holds.
                ("TR-2", ("LOT-1,11",), "不足"),
                # Duplicate lot within one order.
                ("TR-3", ("LOT-1,1", "LOT-1,2"), "重复"),
                # Non-positive / non-numeric quantity.
                ("TR-4", ("LOT-1,0",), "正整数"),
                ("TR-5", ("LOT-1,abc",), "正整数"),
                # Malformed line.
                ("TR-6", ("LOT-1",), "格式"),
            ]
            for transfer, lines, hint in cases:
                with self.subTest(transfer=transfer):
                    result = self.ship(cwd, transfer, *lines)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(hint, result.stderr)
                    self.assertEqual(result.stdout, "")

            # Same source and destination.
            result = self.ship(cwd, "TR-7", "LOT-1,1", dest="WH-A")
            self.assertEqual(result.returncode, 1)
            self.assertIn("不能相同", result.stderr)

            # Blank codes.
            result = self.ship(cwd, "  ", "LOT-1,1")
            self.assertEqual(result.returncode, 1)
            self.assertIn("调拨单号", result.stderr)

            self.assertEqual(self.query_quantities(cwd, "WH-A"), before)

            # A valid order, then a duplicate transfer number.
            result = self.ship(cwd, "TR-8", "LOT-1,4")
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self.ship(cwd, "TR-8", "LOT-1,1")
            self.assertEqual(result.returncode, 1)
            self.assertIn("已存在", result.stderr)
            self.assertEqual(self.query_quantities(cwd, "WH-A"), {"LOT-1": 6})

    def test_receive_full_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10")
            self.assertEqual(self.ship(cwd, "TR-1", "LOT-1,6").returncode, 0)

            result = self.receive(cwd, "TR-1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("received", result.stdout)
            self.assertNotIn("received-with-diff", result.stdout)
            self.assertEqual(self.query_quantities(cwd, "WH-B"), {"LOT-1": 6})

            # Re-confirming is rejected and changes nothing.
            result = self.receive(cwd, "TR-1")
            self.assertEqual(result.returncode, 1)
            self.assertIn("received", result.stderr)
            self.assertEqual(self.query_quantities(cwd, "WH-B"), {"LOT-1": 6})

    def test_receive_with_difference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10",
                          "LOT-2,2024-04-02,2025-04-02,8")
            self.assertEqual(
                self.ship(cwd, "TR-1", "LOT-1,7", "LOT-2,8").returncode, 0
            )

            result = self.receive(cwd, "TR-1", "LOT-1,5")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("received-with-diff", result.stdout)
            # LOT-1 short by 2, LOT-2 (omitted) short by 8: total diff 10.
            self.assertIn("10", result.stdout)
            # Dest keeps only what was actually received per lot.
            self.assertEqual(self.query_quantities(cwd, "WH-B"), {"LOT-1": 5})
            self.assertEqual(
                self.query_quantities(cwd, "WH-A"), {"LOT-1": 3}
            )

    def test_receive_rejections_leave_order_and_ledger_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10")
            self.assertEqual(self.ship(cwd, "TR-1", "LOT-1,6").returncode, 0)
            before_a = self.query_quantities(cwd, "WH-A")
            before_b = self.query_quantities(cwd, "WH-B")

            # Unknown transfer.
            result = self.receive(cwd, "TR-X")
            self.assertEqual(result.returncode, 1)
            self.assertIn("不存在", result.stderr)

            cases = [
                # Lot not part of the order.
                (("LOT-2,1",), "不在调拨单"),
                # Received more than shipped.
                (("LOT-1,7",), "超过"),
                # Duplicate received lot.
                (("LOT-1,1", "LOT-1,2"), "重复"),
                # Non-positive quantity.
                (("LOT-1,0",), "正整数"),
            ]
            for lines, hint in cases:
                with self.subTest(lines=lines):
                    result = self.receive(cwd, "TR-1", *lines)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(hint, result.stderr)
                    self.assertEqual(result.stdout, "")

            self.assertEqual(self.query_quantities(cwd, "WH-A"), before_a)
            self.assertEqual(self.query_quantities(cwd, "WH-B"), before_b)

            # The order is still confirmable after the failed attempts.
            result = self.receive(cwd, "TR-1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("received", result.stdout)

    def test_cancel_returns_shipped_quantities_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10",
                          "LOT-2,2024-04-02,2025-04-02,8")
            self.assertEqual(
                self.ship(cwd, "TR-1", "LOT-1,6", "LOT-2,8").returncode, 0
            )

            result = self.cancel(cwd, "TR-1", "--reason", "客户取消订单")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(), "取消成功：调拨单号=TR-1 退回总数=14"
            )
            self.assertEqual(result.stderr, "")

            # LOT-1 accumulates back onto the surviving source row; LOT-2's
            # source row was fully shipped, so a new row reuses the batch dates.
            self.assertEqual(
                self.query_quantities(cwd, "WH-A"), {"LOT-1": 10, "LOT-2": 8}
            )
            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A", "--product", "SKU-1"
            )
            self.assertIn("批次号=LOT-2 生产日期=2024-04-02 有效期至=2025-04-02",
                          result.stdout)
            # The destination keeps what ship booked.
            self.assertEqual(
                self.query_quantities(cwd, "WH-B"), {"LOT-1": 6, "LOT-2": 8}
            )

            # Order state: canceled, every line received 0, no pending diff.
            conn = sqlite3.connect(cwd / "stock_ledger.db")
            try:
                status, diff_total = conn.execute(
                    "SELECT status, diff_total FROM transfers "
                    "WHERE transfer_no = 'TR-1'"
                ).fetchone()
                self.assertEqual(status, "canceled")
                self.assertEqual(diff_total, 0)
                rows = conn.execute(
                    "SELECT received_quantity FROM transfer_lines "
                    "WHERE transfer_no = 'TR-1' ORDER BY seq"
                ).fetchall()
                self.assertEqual([row[0] for row in rows], [0, 0])
            finally:
                conn.close()

            # A canceled order can neither be received nor canceled again.
            result = self.receive(cwd, "TR-1")
            self.assertEqual(result.returncode, 1)
            self.assertIn("canceled", result.stderr)
            self.assertEqual(result.stdout, "")
            result = self.cancel(cwd, "TR-1")
            self.assertEqual(result.returncode, 1)
            self.assertIn("canceled", result.stderr)
            self.assertEqual(result.stdout, "")

    def test_cancel_without_reason_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10")
            self.assertEqual(self.ship(cwd, "TR-1", "LOT-1,6").returncode, 0)

            result = self.cancel(cwd, "TR-1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("退回总数=6", result.stdout)
            self.assertEqual(self.query_quantities(cwd, "WH-A"), {"LOT-1": 10})

    def test_cancel_rejections_leave_ledger_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10")
            self.assertEqual(self.ship(cwd, "TR-1", "LOT-1,6").returncode, 0)
            before_a = self.query_quantities(cwd, "WH-A")
            before_b = self.query_quantities(cwd, "WH-B")

            # Unknown transfer.
            result = self.cancel(cwd, "TR-X")
            self.assertEqual(result.returncode, 1)
            self.assertIn("不存在", result.stderr)
            self.assertEqual(result.stdout, "")

            # Blank transfer number.
            result = self.cancel(cwd, "  ")
            self.assertEqual(result.returncode, 1)
            self.assertIn("调拨单号", result.stderr)
            self.assertEqual(result.stdout, "")

            # Blank reason.
            result = self.cancel(cwd, "TR-1", "--reason", "  ")
            self.assertEqual(result.returncode, 1)
            self.assertIn("取消原因", result.stderr)
            self.assertEqual(result.stdout, "")

            self.assertEqual(self.query_quantities(cwd, "WH-A"), before_a)
            self.assertEqual(self.query_quantities(cwd, "WH-B"), before_b)

            # A fully received order cannot be canceled.
            self.assertEqual(self.receive(cwd, "TR-1").returncode, 0)
            result = self.cancel(cwd, "TR-1")
            self.assertEqual(result.returncode, 1)
            self.assertIn("received", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(self.query_quantities(cwd, "WH-A"), before_a)
            self.assertEqual(self.query_quantities(cwd, "WH-B"), before_b)

    def test_cancel_rejected_after_received_with_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10")
            self.assertEqual(self.ship(cwd, "TR-1", "LOT-1,6").returncode, 0)
            self.assertEqual(self.receive(cwd, "TR-1", "LOT-1,4").returncode, 0)
            before_a = self.query_quantities(cwd, "WH-A")
            before_b = self.query_quantities(cwd, "WH-B")

            result = self.cancel(cwd, "TR-1", "--reason", "太迟了")
            self.assertEqual(result.returncode, 1)
            self.assertIn("received-with-diff", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(self.query_quantities(cwd, "WH-A"), before_a)
            self.assertEqual(self.query_quantities(cwd, "WH-B"), before_b)


if __name__ == "__main__":
    unittest.main()
