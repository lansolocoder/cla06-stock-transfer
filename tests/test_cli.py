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


class CancelCommandTests(unittest.TestCase):
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

    def ship(self, cwd: Path, transfer: str, *lines: str) -> subprocess.CompletedProcess[str]:
        arguments = [
            "ship", "--transfer", transfer,
            "--from", "WH-A", "--to", "WH-B", "--product", "SKU-1",
        ]
        for line in lines:
            arguments += ["--line", line]
        return self.invoke_in(cwd, *arguments)

    def receive(self, cwd: Path, transfer: str, *lines: str) -> subprocess.CompletedProcess[str]:
        arguments = ["receive", "--transfer", transfer]
        for line in lines:
            arguments += ["--line", line]
        return self.invoke_in(cwd, *arguments)

    def cancel(self, cwd: Path, transfer: str, reason: str | None = "客户撤单",
               db: str | None = None) -> subprocess.CompletedProcess[str]:
        arguments = ["cancel", "--transfer", transfer]
        if reason is not None:
            arguments += ["--reason", reason]
        if db is not None:
            arguments += ["--db", db]
        return self.invoke_in(cwd, *arguments)

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

    def query_batch(self, cwd: Path, warehouse: str, lot: str) -> tuple[str, str, int] | None:
        result = self.invoke_in(
            cwd, "query", "--warehouse", warehouse, "--product", "SKU-1"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(
            rf"批次号={lot} 生产日期=(\S+) 有效期至=(\S+) 数量=(\d+)",
            result.stdout,
        )
        if match is None:
            return None
        return match[1], match[2], int(match[3])

    def order_state(self, cwd: Path, transfer: str) -> tuple[str, int, dict[str, int | None]]:
        conn = sqlite3.connect(cwd / "stock_ledger.db")
        try:
            row = conn.execute(
                "SELECT status, diff_total FROM transfers WHERE transfer_no = ?",
                (transfer,),
            ).fetchone()
            self.assertIsNotNone(row)
            status, diff_total = row
            lines = {
                lot: received
                for lot, received in conn.execute(
                    "SELECT lot, received_quantity FROM transfer_lines "
                    "WHERE transfer_no = ? ORDER BY seq",
                    (transfer,),
                )
            }
        finally:
            conn.close()
        return status, diff_total, lines

    def test_cancel_returns_quantities_and_freezes_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10",
                          "LOT-2,2024-04-02,2025-04-02,8")
            self.assertEqual(
                self.ship(cwd, "TR-1", "LOT-1,6", "LOT-2,8").returncode, 0
            )
            # LOT-2 was fully shipped, so its source row no longer exists.
            self.assertEqual(self.query_quantities(cwd, "WH-A"), {"LOT-1": 4})
            self.assertEqual(
                self.query_quantities(cwd, "WH-B"), {"LOT-1": 6, "LOT-2": 8}
            )

            result = self.cancel(cwd, "TR-1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(
                result.stdout.strip(), "取消成功：调拨单号=TR-1 退回总数=14"
            )

            # Everything shipped is back at the source; LOT-2 is appended as a
            # new row keeping the batch's current dates.
            self.assertEqual(
                self.query_quantities(cwd, "WH-A"), {"LOT-1": 10, "LOT-2": 8}
            )
            self.assertEqual(
                self.query_batch(cwd, "WH-A", "LOT-2"),
                ("2024-04-02", "2025-04-02", 8),
            )
            # The destination keeps exactly what ship booked.
            self.assertEqual(
                self.query_quantities(cwd, "WH-B"), {"LOT-1": 6, "LOT-2": 8}
            )

            status, diff_total, lines = self.order_state(cwd, "TR-1")
            self.assertEqual(status, "canceled")
            self.assertEqual(diff_total, 0)
            self.assertEqual(lines, {"LOT-1": 0, "LOT-2": 0})

    def test_cancel_accumulates_onto_remaining_source_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10")
            # Other movements on the same lot must not be disturbed by the
            # accumulation (pre-existing source row just gets the return added).
            self.assertEqual(self.ship(cwd, "TR-1", "LOT-1,6").returncode, 0)
            self.assertEqual(self.ship(cwd, "TR-2", "LOT-1,1").returncode, 0)
            self.assertEqual(self.query_quantities(cwd, "WH-A"), {"LOT-1": 3})

            result = self.cancel(cwd, "TR-1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("退回总数=6", result.stdout)
            self.assertEqual(self.query_quantities(cwd, "WH-A"), {"LOT-1": 9})
            # TR-1's destination booking stays put.
            self.assertEqual(self.query_quantities(cwd, "WH-B"), {"LOT-1": 7})

    def test_cancel_without_reason_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10")
            self.assertEqual(self.ship(cwd, "TR-1", "LOT-1,6").returncode, 0)

            result = self.cancel(cwd, "TR-1", reason=None)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(), "取消成功：调拨单号=TR-1 退回总数=6"
            )

    def test_cancel_respects_custom_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / "nested").mkdir()
            db = str(cwd / "nested" / "ledger.db")
            self.invoke_in(
                cwd, "register", "--warehouse", "WH-A", "--product", "SKU-1",
                "--batch", "LOT-1,2024-03-01,2025-03-01,10", "--db", db,
            )
            result = self.invoke_in(
                cwd, "ship", "--transfer", "TR-1",
                "--from", "WH-A", "--to", "WH-B", "--product", "SKU-1",
                "--line", "LOT-1,6", "--db", db,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            result = self.cancel(cwd, "TR-1", db=db)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("取消成功：调拨单号=TR-1 退回总数=6", result.stdout)
            self.assertFalse((cwd / "stock_ledger.db").exists())

    def test_cancel_rejections_leave_everything_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10",
                          "LOT-2,2024-04-02,2025-04-02,8")
            self.assertEqual(
                self.ship(cwd, "TR-1", "LOT-1,6", "LOT-2,8").returncode, 0
            )
            before_a = self.query_quantities(cwd, "WH-A")
            before_b = self.query_quantities(cwd, "WH-B")

            # Unknown transfer.
            result = self.cancel(cwd, "TR-X")
            self.assertEqual(result.returncode, 1)
            self.assertIn("不存在", result.stderr)
            self.assertEqual(result.stdout, "")

            # Blank transfer number / blank reason.
            result = self.cancel(cwd, "   ")
            self.assertEqual(result.returncode, 1)
            self.assertIn("调拨单号", result.stderr)
            self.assertEqual(result.stdout, "")
            result = self.cancel(cwd, "TR-1", reason="   ")
            self.assertEqual(result.returncode, 1)
            self.assertIn("取消原因", result.stderr)
            self.assertEqual(result.stdout, "")

            self.assertEqual(self.query_quantities(cwd, "WH-A"), before_a)
            self.assertEqual(self.query_quantities(cwd, "WH-B"), before_b)
            self.assertEqual(self.order_state(cwd, "TR-1")[0], "shipped")

            # The order is still cancelable after the failed attempts.
            result = self.cancel(cwd, "TR-1")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cancel_refused_unless_exactly_shipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10",
                          "LOT-2,2024-04-02,2025-04-02,8")
            self.assertEqual(self.ship(cwd, "TR-1", "LOT-1,6").returncode, 0)
            self.assertEqual(
                self.ship(cwd, "TR-2", "LOT-2,5").returncode, 0
            )

            self.assertEqual(self.receive(cwd, "TR-1").returncode, 0)
            self.assertEqual(
                self.receive(cwd, "TR-2", "LOT-2,3").returncode, 0
            )

            for transfer, hint in [("TR-1", "received"), ("TR-2", "diff")]:
                with self.subTest(transfer=transfer):
                    result = self.cancel(cwd, transfer)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(hint, result.stderr)
                    self.assertEqual(result.stdout, "")

            # Repeat cancellation is refused as well.
            self.register(cwd, "WH-A", "LOT-3,2024-05-02,2025-05-02,4")
            self.assertEqual(self.ship(cwd, "TR-3", "LOT-3,4").returncode, 0)
            result = self.cancel(cwd, "TR-3")
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self.cancel(cwd, "TR-3")
            self.assertEqual(result.returncode, 1)
            self.assertIn("canceled", result.stderr)
            self.assertEqual(result.stdout, "")
            # Returned quantity must not be returned a second time.
            self.assertEqual(self.query_quantities(cwd, "WH-A")["LOT-3"], 4)

    def test_receive_after_cancel_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10")
            self.assertEqual(self.ship(cwd, "TR-1", "LOT-1,6").returncode, 0)
            self.assertEqual(self.cancel(cwd, "TR-1").returncode, 0)

            result = self.receive(cwd, "TR-1")
            self.assertEqual(result.returncode, 1)
            self.assertIn("canceled", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(self.order_state(cwd, "TR-1")[0], "canceled")

    def test_cancel_atomicity_when_a_return_is_illegal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10")
            # Ship the whole lot so the source row is deleted; the only copy
            # of the batch dates now sits at the destination.
            self.assertEqual(self.ship(cwd, "TR-1", "LOT-1,10").returncode, 0)

            # Tamper with the ledger so the batch exists nowhere: no source
            # row to append to and no current dates to carry back.
            conn = sqlite3.connect(cwd / "stock_ledger.db")
            try:
                conn.execute("DELETE FROM stock_batches")
                conn.commit()
            finally:
                conn.close()

            result = self.cancel(cwd, "TR-1")
            self.assertEqual(result.returncode, 1)
            self.assertIn("无法退回", result.stderr)
            self.assertEqual(result.stdout, "")

            # Whole cancellation rejected: nothing returned, order untouched.
            self.assertEqual(self.query_quantities(cwd, "WH-A"), {})
            self.assertEqual(self.order_state(cwd, "TR-1")[0], "shipped")


class ResolveCommandTests(unittest.TestCase):
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

    def ship(self, cwd: Path, transfer: str, *lines: str) -> subprocess.CompletedProcess[str]:
        arguments = [
            "ship", "--transfer", transfer,
            "--from", "WH-A", "--to", "WH-B", "--product", "SKU-1",
        ]
        for line in lines:
            arguments += ["--line", line]
        return self.invoke_in(cwd, *arguments)

    def receive(self, cwd: Path, transfer: str, *lines: str) -> subprocess.CompletedProcess[str]:
        arguments = ["receive", "--transfer", transfer]
        for line in lines:
            arguments += ["--line", line]
        return self.invoke_in(cwd, *arguments)

    def resolve(self, cwd: Path, transfer: str, *lines: str) -> subprocess.CompletedProcess[str]:
        arguments = ["resolve", "--transfer", transfer]
        for line in lines:
            arguments += ["--line", line]
        return self.invoke_in(cwd, *arguments)

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

    def order_state(
        self, cwd: Path, transfer: str
    ) -> tuple[str, int, dict[str, tuple[int, int | None]]]:
        conn = sqlite3.connect(cwd / "stock_ledger.db")
        try:
            row = conn.execute(
                "SELECT status, diff_total FROM transfers WHERE transfer_no = ?",
                (transfer,),
            ).fetchone()
            self.assertIsNotNone(row)
            status, diff_total = row
            lines = {
                lot: (shipped, received)
                for lot, shipped, received in conn.execute(
                    "SELECT lot, shipped_quantity, received_quantity "
                    "FROM transfer_lines WHERE transfer_no = ? ORDER BY seq",
                    (transfer,),
                )
            }
        finally:
            conn.close()
        return status, diff_total, lines

    def setup_diff_order(self, cwd: Path) -> None:
        # After receipt: LOT-1 shipped 7 / received 5 (diff 2), LOT-2 shipped 8 /
        # received 0 (diff 8); pending differences total 10.
        self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10",
                      "LOT-2,2024-04-02,2025-04-02,8")
        self.assertEqual(
            self.ship(cwd, "TR-1", "LOT-1,7", "LOT-2,8").returncode, 0
        )
        self.assertEqual(self.receive(cwd, "TR-1", "LOT-1,5").returncode, 0)

    def test_resolve_closes_all_differences_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.setup_diff_order(cwd)
            before_a = self.query_quantities(cwd, "WH-A")
            before_b = self.query_quantities(cwd, "WH-B")
            _, _, received_before = self.order_state(cwd, "TR-1")

            result = self.resolve(cwd, "TR-1", "LOT-1,2", "LOT-2,8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(
                result.stdout.strip(),
                "差异处理成功：调拨单号=TR-1 结案总数=10",
            )
            self.assertEqual(len(result.stdout.splitlines()), 1)

            status, diff_total, lines = self.order_state(cwd, "TR-1")
            self.assertEqual(status, "resolved")
            self.assertEqual(diff_total, 0)
            # Received quantities are frozen at their pre-resolution values.
            self.assertEqual(
                lines,
                {"LOT-1": (7, 5), "LOT-2": (8, 0)},
            )
            self.assertEqual(lines, received_before)
            # Neither warehouse's on-hand quantities move.
            self.assertEqual(self.query_quantities(cwd, "WH-A"), before_a)
            self.assertEqual(self.query_quantities(cwd, "WH-B"), before_b)

    def test_resolve_without_lines_closes_every_pending_difference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.setup_diff_order(cwd)

            result = self.resolve(cwd, "TR-1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(),
                "差异处理成功：调拨单号=TR-1 结案总数=10",
            )
            status, diff_total, _ = self.order_state(cwd, "TR-1")
            self.assertEqual(status, "resolved")
            self.assertEqual(diff_total, 0)

    def test_partial_resolve_keeps_remaining_difference_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.setup_diff_order(cwd)

            result = self.resolve(cwd, "TR-1", "LOT-1,2")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(),
                "差异处理成功：调拨单号=TR-1 结案总数=2",
            )
            status, diff_total, lines = self.order_state(cwd, "TR-1")
            self.assertEqual(status, "received-with-diff")
            self.assertEqual(diff_total, 8)
            self.assertEqual(lines, {"LOT-1": (7, 5), "LOT-2": (8, 0)})

            # The still-pending lot can be closed later.
            result = self.resolve(cwd, "TR-1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(),
                "差异处理成功：调拨单号=TR-1 结案总数=8",
            )
            status, diff_total, _ = self.order_state(cwd, "TR-1")
            self.assertEqual(status, "resolved")
            self.assertEqual(diff_total, 0)

    def test_partial_quantity_leaves_lot_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.setup_diff_order(cwd)

            result = self.resolve(cwd, "TR-1", "LOT-2,3")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("结案总数=3", result.stdout)
            status, diff_total, _ = self.order_state(cwd, "TR-1")
            self.assertEqual(status, "received-with-diff")
            self.assertEqual(diff_total, 7)

            result = self.resolve(cwd, "TR-1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("结案总数=7", result.stdout)
            self.assertEqual(self.order_state(cwd, "TR-1")[0], "resolved")

    def test_resolve_received_order_without_difference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10")
            self.assertEqual(self.ship(cwd, "TR-1", "LOT-1,6").returncode, 0)
            self.assertEqual(self.receive(cwd, "TR-1").returncode, 0)

            result = self.resolve(cwd, "TR-1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(),
                "差异处理成功：调拨单号=TR-1 结案总数=0",
            )
            status, diff_total, lines = self.order_state(cwd, "TR-1")
            self.assertEqual(status, "resolved")
            self.assertEqual(diff_total, 0)
            self.assertEqual(lines, {"LOT-1": (6, 6)})

    def test_resolve_rejections_leave_order_and_ledger_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.setup_diff_order(cwd)
            before_a = self.query_quantities(cwd, "WH-A")
            before_b = self.query_quantities(cwd, "WH-B")
            before_state = self.order_state(cwd, "TR-1")

            # Unknown transfer.
            result = self.resolve(cwd, "TR-X")
            self.assertEqual(result.returncode, 1)
            self.assertIn("不存在", result.stderr)
            self.assertEqual(result.stdout, "")

            cases = [
                # Lot not part of the order.
                (("LOT-9,1",), "不在调拨单"),
                # Quantity exceeds that lot's pending difference.
                (("LOT-1,3",), "超过"),
                (("LOT-2,9",), "超过"),
                # Duplicate lot among the lines.
                (("LOT-1,1", "LOT-1,1"), "重复"),
                # Non-positive / non-numeric quantity.
                (("LOT-1,0",), "正整数"),
                (("LOT-1,-2",), "正整数"),
                (("LOT-1,abc",), "正整数"),
                # Malformed line / blank lot.
                (("LOT-1",), "格式"),
                (("  ,2",), "批次号"),
            ]
            for lines, hint in cases:
                with self.subTest(lines=lines):
                    result = self.resolve(cwd, "TR-1", *lines)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(hint, result.stderr)
                    self.assertEqual(result.stdout, "")

            # Blank transfer number.
            result = self.resolve(cwd, "   ")
            self.assertEqual(result.returncode, 1)
            self.assertIn("调拨单号", result.stderr)
            self.assertEqual(result.stdout, "")

            # Every failed attempt left the order exactly as it started.
            self.assertEqual(self.query_quantities(cwd, "WH-A"), before_a)
            self.assertEqual(self.query_quantities(cwd, "WH-B"), before_b)
            self.assertEqual(self.order_state(cwd, "TR-1"), before_state)

            # A lot whose pending difference is already zero cannot be settled;
            # the failed attempt must not undo the successful partial resolve.
            self.assertEqual(
                self.resolve(cwd, "TR-1", "LOT-1,2").returncode, 0
            )
            pending_state = self.order_state(cwd, "TR-1")
            self.assertEqual(pending_state[0], "received-with-diff")
            self.assertEqual(pending_state[1], 8)
            result = self.resolve(cwd, "TR-1", "LOT-1,1")
            self.assertEqual(result.returncode, 1)
            self.assertIn("超过", result.stderr)
            self.assertEqual(result.stdout, "")

            self.assertEqual(self.query_quantities(cwd, "WH-A"), before_a)
            self.assertEqual(self.query_quantities(cwd, "WH-B"), before_b)
            self.assertEqual(self.order_state(cwd, "TR-1"), pending_state)

    def test_resolve_refused_for_shipped_canceled_and_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd, "WH-A", "LOT-1,2024-03-01,2025-03-01,10",
                          "LOT-2,2024-04-02,2025-04-02,8",
                          "LOT-3,2024-05-02,2025-05-02,4")
            self.assertEqual(self.ship(cwd, "TR-1", "LOT-1,6").returncode, 0)
            self.assertEqual(self.ship(cwd, "TR-2", "LOT-2,5").returncode, 0)
            self.assertEqual(self.ship(cwd, "TR-3", "LOT-3,4").returncode, 0)
            self.assertEqual(
                self.receive(cwd, "TR-2", "LOT-2,3").returncode, 0
            )

            # Still shipped.
            result = self.resolve(cwd, "TR-1")
            self.assertEqual(result.returncode, 1)
            self.assertIn("shipped", result.stderr)
            self.assertEqual(result.stdout, "")

            # Canceled.
            self.assertEqual(
                self.invoke_in(
                    cwd, "cancel", "--transfer", "TR-3", "--reason", "x"
                ).returncode,
                0,
            )
            result = self.resolve(cwd, "TR-3")
            self.assertEqual(result.returncode, 1)
            self.assertIn("canceled", result.stderr)
            self.assertEqual(result.stdout, "")

            # Fully resolved: cannot be resolved a second time, received, or
            # canceled.
            self.assertEqual(self.resolve(cwd, "TR-2").returncode, 0)
            result = self.resolve(cwd, "TR-2")
            self.assertEqual(result.returncode, 1)
            self.assertIn("resolved", result.stderr)
            self.assertEqual(result.stdout, "")
            result = self.receive(cwd, "TR-2")
            self.assertEqual(result.returncode, 1)
            self.assertIn("resolved", result.stderr)
            self.assertEqual(result.stdout, "")
            result = self.invoke_in(
                cwd, "cancel", "--transfer", "TR-2", "--reason", "x"
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("resolved", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(self.order_state(cwd, "TR-2")[0], "resolved")

    def test_resolve_respects_custom_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / "nested").mkdir()
            db = str(cwd / "nested" / "ledger.db")
            self.invoke_in(
                cwd, "register", "--warehouse", "WH-A", "--product", "SKU-1",
                "--batch", "LOT-1,2024-03-01,2025-03-01,10", "--db", db,
            )
            self.assertEqual(
                self.invoke_in(
                    cwd, "ship", "--transfer", "TR-1",
                    "--from", "WH-A", "--to", "WH-B", "--product", "SKU-1",
                    "--line", "LOT-1,6", "--db", db,
                ).returncode,
                0,
            )
            self.assertEqual(
                self.invoke_in(
                    cwd, "receive", "--transfer", "TR-1",
                    "--line", "LOT-1,4", "--db", db,
                ).returncode,
                0,
            )
            result = self.invoke_in(
                cwd, "resolve", "--transfer", "TR-1",
                "--line", "LOT-1,2", "--db", db,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("差异处理成功：调拨单号=TR-1 结案总数=2", result.stdout)
            self.assertFalse((cwd / "stock_ledger.db").exists())


if __name__ == "__main__":
    unittest.main()
