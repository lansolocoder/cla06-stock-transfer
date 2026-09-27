"""Checks for the documented command-line entry point."""

import os
from pathlib import Path
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
        self.assertIn("transfer", result.stdout)
        self.assertIn("transfer-query", result.stdout)


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

    def seed(self, cwd: Path) -> None:
        result = self.invoke_in(
            cwd,
            "register", "--warehouse", "WH-A", "--product", "SKU-1001",
            "--db", str(cwd / "ledger.db"),
            "--batch", "LOT-1,2024-03-01,2025-03-01,18",
            "--batch", "LOT-2,2024-04-02,2025-04-02,12",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_submit_then_query_then_source_is_drawn_down(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd)

            result = self.invoke_in(
                cwd,
                "transfer", "--order", "TR-001",
                "--from", " WH-A ", "--to", "WH-B", "--product", "SKU-1001",
                "--db", db,
                "--item", "LOT-1,10",
                "--item", " LOT-2 , 12 ",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("调拨单号=TR-001", result.stdout)
            self.assertIn("来源仓=WH-A", result.stdout)
            self.assertIn("目标仓=WH-B", result.stdout)
            self.assertIn("状态=in_transit", result.stdout)
            self.assertIn("实收数量=0", result.stdout)

            result = self.invoke_in(
                cwd, "transfer-query", "--order", "TR-001", "--db", db
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("调拨单号=TR-001", result.stdout)
            self.assertIn("来源仓=WH-A", result.stdout)
            self.assertIn("目标仓=WH-B", result.stdout)
            self.assertIn("商品=SKU-1001", result.stdout)
            self.assertIn("状态=in_transit", result.stdout)
            self.assertIn("实收数量=0", result.stdout)
            self.assertIn("批次号=LOT-1 调出数量=10", result.stdout)
            self.assertIn("批次号=LOT-2 调出数量=12", result.stdout)

            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A",
                "--product", "SKU-1001", "--db", db
            )
            self.assertIn("数量=8", result.stdout)
            self.assertIn("数量=0", result.stdout)

            # Target warehouse is not credited yet.
            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-B",
                "--product", "SKU-1001", "--db", db
            )
            self.assertIn("批次总数=0", result.stdout)

    def test_query_missing_order_returns_empty_with_zero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = self.invoke_in(
                cwd, "transfer-query", "--order", "NOPE",
                "--db", str(cwd / "ledger.db")
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("调拨单号=NOPE", result.stdout)
            self.assertIn("批次数=0", result.stdout)
            self.assertFalse((cwd / "ledger.db").exists())

    def test_duplicate_order_rejected_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd)
            self.invoke_in(
                cwd,
                "transfer", "--order", "TR-001", "--from", "WH-A",
                "--to", "WH-B", "--product", "SKU-1001", "--db", db,
                "--item", "LOT-1,2",
            )
            result = self.invoke_in(
                cwd,
                "transfer", "--order", "TR-001", "--from", "WH-A",
                "--to", "WH-B", "--product", "SKU-1001", "--db", db,
                "--item", "LOT-1,3",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("TR-001", result.stderr)
            self.assertIn("唯一", result.stderr)

            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A",
                "--product", "SKU-1001", "--db", db
            )
            # Still 16 (18 - 2), not 13.
            self.assertIn("数量=16", result.stdout)

    def test_source_must_differ_from_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd)
            result = self.invoke_in(
                cwd,
                "transfer", "--order", "TR-002", "--from", "WH-A",
                "--to", " WH-A ", "--product", "SKU-1001", "--db", db,
                "--item", "LOT-1,1",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("不得与来源仓", result.stderr)

    def test_unknown_lot_and_insufficient_quantity_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd)

            result = self.invoke_in(
                cwd,
                "transfer", "--order", "TR-003", "--from", "WH-A",
                "--to", "WH-B", "--product", "SKU-1001", "--db", db,
                "--item", "LOT-X,1",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("LOT-X", result.stderr)
            self.assertIn("落账", result.stderr)

            result = self.invoke_in(
                cwd,
                "transfer", "--order", "TR-004", "--from", "WH-A",
                "--to", "WH-B", "--product", "SKU-1001", "--db", db,
                "--item", "LOT-1,99",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("超过现存数量 18", result.stderr)

            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A",
                "--product", "SKU-1001", "--db", db
            )
            self.assertIn("数量=18", result.stdout)

    def test_duplicate_lot_in_one_order_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd)
            result = self.invoke_in(
                cwd,
                "transfer", "--order", "TR-005", "--from", "WH-A",
                "--to", "WH-B", "--product", "SKU-1001", "--db", db,
                "--item", "LOT-1,1",
                "--item", "LOT-1,2",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("批次分配行 2", result.stderr)
            self.assertIn("重复", result.stderr)

    def test_invalid_item_lines_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd)
            cases = [
                ("LOT-1,0", "正整数"),
                ("LOT-1,-4", "正整数"),
                ("LOT-1,x", "正整数"),
                (" ,1", "批次号不能为空"),
                ("LOT-1", "格式"),
                ("LOT-1,1,9", "不接受汇总数量"),
            ]
            for item_line, hint in cases:
                with self.subTest(item_line=item_line):
                    result = self.invoke_in(
                        cwd,
                        "transfer", "--order", "TR-X", "--from", "WH-A",
                        "--to", "WH-B", "--product", "SKU-1001",
                        "--db", db, "--item", item_line,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(hint, result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_one_bad_item_line_rejects_whole_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd)
            result = self.invoke_in(
                cwd,
                "transfer", "--order", "TR-006", "--from", "WH-A",
                "--to", "WH-B", "--product", "SKU-1001", "--db", db,
                "--item", "LOT-2,1",
                "--item", "LOT-1,99",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("批次分配行 2", result.stderr)

            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A",
                "--product", "SKU-1001", "--db", db
            )
            self.assertIn("数量=18", result.stdout)
            self.assertIn("数量=12", result.stdout)

            result = self.invoke_in(
                cwd, "transfer-query", "--order", "TR-006", "--db", db
            )
            self.assertIn("批次数=0", result.stdout)


class ReceiveCommandTests(unittest.TestCase):
    def invoke_in(self, cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "stock_transfer", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=ENV,
        )

    def seed_in_transit(self, cwd: Path, db: str) -> None:
        result = self.invoke_in(
            cwd,
            "register", "--warehouse", "WH-A", "--product", "SKU-1001",
            "--db", db,
            "--batch", "LOT-1,2024-03-01,2025-03-01,18",
            "--batch", "LOT-2,2024-04-02,2025-04-02,12",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.invoke_in(
            cwd,
            "transfer", "--order", "TR-001", "--from", "WH-A",
            "--to", "WH-B", "--product", "SKU-1001", "--db", db,
            "--item", "LOT-1,10", "--item", "LOT-2,4",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def query_warehouse(self, cwd: Path, db: str, warehouse: str) -> str:
        result = self.invoke_in(
            cwd, "query", "--warehouse", warehouse,
            "--product", "SKU-1001", "--db", db,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_receive_with_discrepancy_credits_target_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed_in_transit(cwd, db)

            result = self.invoke_in(
                cwd, "receive", "--order", " TR-001 ", "--db", db,
                "--received", " LOT-1 , 9 ", "--received", "LOT-2,0",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("状态=received", result.stdout)
            self.assertIn("实收数量=9", result.stdout)

            # Per-batch received quantities read back in a new process.
            result = self.invoke_in(
                cwd, "transfer-query", "--order", "TR-001", "--db", db
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("状态=received", result.stdout)
            self.assertIn("实收数量=9", result.stdout)
            self.assertIn("批次号=LOT-1 调出数量=10 实收数量=9", result.stdout)
            self.assertIn("批次号=LOT-2 调出数量=4 实收数量=0", result.stdout)

            # Target gets 9 for LOT-1 with source registration dates, and
            # the zero-received LOT-2 creates no batch row.
            target = self.query_warehouse(cwd, db, "WH-B")
            self.assertIn("批次总数=1", target)
            self.assertIn("批次号=LOT-1", target)
            self.assertIn("生产日期=2024-03-01", target)
            self.assertIn("有效期至=2025-03-01", target)
            self.assertIn("数量=9", target)
            self.assertNotIn("LOT-2", target)

            # Source draw-down from the transfer is untouched.
            source = self.query_warehouse(cwd, db, "WH-A")
            self.assertIn("数量=8", source)

    def test_target_lot_accumulates_when_already_booked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed_in_transit(cwd, db)
            # Target already holds 3 of LOT-1.
            result = self.invoke_in(
                cwd,
                "register", "--warehouse", "WH-B", "--product", "SKU-1001",
                "--db", db, "--batch", "LOT-1,2024-01-01,2025-01-01,3",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            result = self.invoke_in(
                cwd, "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,10", "--received", "LOT-2,4",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            target = self.query_warehouse(cwd, db, "WH-B")
            self.assertIn("批次总数=2", target)
            self.assertIn("批次号=LOT-1", target)
            self.assertIn("数量=13", target)
            self.assertIn("批次号=LOT-2", target)
            self.assertIn("数量=4", target)

    def test_full_receipt_marks_received_with_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed_in_transit(cwd, db)
            result = self.invoke_in(
                cwd, "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,10", "--received", "LOT-2,4",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("状态=received", result.stdout)
            self.assertIn("实收数量=14", result.stdout)

    def test_duplicate_confirmation_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed_in_transit(cwd, db)
            first = self.invoke_in(
                cwd, "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,9", "--received", "LOT-2,0",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            # A late, different receipt must not overwrite the first.
            result = self.invoke_in(
                cwd, "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,10", "--received", "LOT-2,4",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("received", result.stderr)
            self.assertEqual(result.stdout, "")

            target = self.query_warehouse(cwd, db, "WH-B")
            self.assertIn("数量=9", target)
            self.assertNotIn("LOT-2", target)

    def test_rejection_cases_leave_everything_unchanged(self) -> None:
        cases = [
            (["--received", "LOT-1,10"], "缺少"),
            (["--received", "LOT-1,10", "--received", "LOT-2,4",
              "--received", "LOT-X,1"], "不存在的批次号"),
            (["--received", "LOT-1,11", "--received", "LOT-2,4"], "超过调出数量"),
            (["--received", "LOT-1,-1", "--received", "LOT-2,4"], "非负整数"),
            (["--received", "LOT-1,x", "--received", "LOT-2,4"], "非负整数"),
            (["--received", "LOT-1,10", "--received", "LOT-1,0",
              "--received", "LOT-2,4"], "重复"),
            (["--received", "LOT-1"], "格式"),
            (["--received", "  ,1"], "批次号不能为空"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed_in_transit(cwd, db)
            for extra, hint in cases:
                with self.subTest(hint=hint):
                    result = self.invoke_in(
                        cwd, "receive", "--order", "TR-001", "--db", db,
                        *extra,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(hint, result.stderr)
                    self.assertEqual(result.stdout, "")

            # Order still in transit with zero received, target untouched.
            result = self.invoke_in(
                cwd, "transfer-query", "--order", "TR-001", "--db", db
            )
            self.assertIn("状态=in_transit", result.stdout)
            self.assertIn("实收数量=0", result.stdout)
            target = self.query_warehouse(cwd, db, "WH-B")
            self.assertIn("批次总数=0", target)

    def test_unknown_order_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            result = self.invoke_in(
                cwd, "receive", "--order", "NOPE", "--db", db,
                "--received", "LOT-1,1",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("NOPE", result.stderr)
            self.assertIn("不存在", result.stderr)

    def test_cancelled_order_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed_in_transit(cwd, db)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE transfers SET status='cancelled' "
                    "WHERE order_no='TR-001'"
                )
            result = self.invoke_in(
                cwd, "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,10", "--received", "LOT-2,4",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cancelled", result.stderr)
            self.assertIn("批次总数=0", self.query_warehouse(cwd, db, "WH-B"))

    def test_blank_order_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            result = self.invoke_in(
                cwd, "receive", "--order", "   ", "--db", db,
                "--received", "LOT-1,1",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("调拨单号", result.stderr)

    def test_help_mentions_receive(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "stock_transfer", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=ENV,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("receive", result.stdout)


class SplitMergeCommandTests(unittest.TestCase):
    def invoke_in(self, cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "stock_transfer", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=ENV,
        )

    def seed(self, cwd: Path, db: str) -> None:
        result = self.invoke_in(
            cwd,
            "register", "--warehouse", "WH-A", "--product", "SKU-1001",
            "--db", db,
            "--batch", "LOT-1,2024-03-01,2025-03-01,18",
            "--batch", "LOT-2,2024-04-02,2025-04-02,12",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def query_warehouse(self, cwd: Path, db: str, warehouse: str) -> str:
        result = self.invoke_in(
            cwd, "query", "--warehouse", warehouse,
            "--product", "SKU-1001", "--db", db,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_split_books_two_new_lots_with_source_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)

            result = self.invoke_in(
                cwd, "split", "--warehouse", " WH-A ", "--product", "SKU-1001",
                "--db", db, "--lot", " LOT-1 ",
                "--into", " LOT-1A , 10 ", "--into", "LOT-1B,8",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("拆分成功", result.stdout)

            # Readable back in a separate process: source gone, two new lots.
            output = self.query_warehouse(cwd, db, "WH-A")
            self.assertIn("批次总数=3", output)
            self.assertNotIn("批次号=LOT-1 ", output)
            self.assertIn("批次号=LOT-1A 生产日期=2024-03-01 有效期至=2025-03-01 数量=10", output)
            self.assertIn("批次号=LOT-1B 生产日期=2024-03-01 有效期至=2025-03-01 数量=8", output)
            self.assertIn("批次号=LOT-2", output)

    def test_split_rejections_leave_ledger_unchanged(self) -> None:
        cases = [
            (["--lot", "LOT-X", "--into", "A,10", "--into", "B,8"], "落账"),
            (["--lot", "LOT-1", "--into", "A,10", "--into", "B,9"], "现存数量 18"),
            (["--lot", "LOT-1", "--into", "A,0", "--into", "B,18"], "正整数"),
            (["--lot", "LOT-1", "--into", "A,-1", "--into", "B,19"], "正整数"),
            (["--lot", "LOT-1", "--into", "A,x", "--into", "B,18"], "正整数"),
            (["--lot", "LOT-1", "--into", "A,10"], "恰好两行"),
            (["--lot", "LOT-1", "--into", "A,6", "--into", "A,12"], "重复"),
            (["--lot", "LOT-1", "--into", "LOT-1,10", "--into", "B,8"], "不得与来源批次号"),
            (["--lot", "LOT-1", "--into", "LOT-2,10", "--into", "B,8"], "已在仓库"),
            (["--lot", "LOT-1", "--into", " ,10", "--into", "B,8"], "新批次号不能为空"),
            (["--lot", "LOT-1", "--into", "A,10,9", "--into", "B,8"], "格式"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)
            for extra, hint in cases:
                with self.subTest(hint=hint):
                    result = self.invoke_in(
                        cwd, "split", "--warehouse", "WH-A",
                        "--product", "SKU-1001", "--db", db, *extra,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(hint, result.stderr)
                    self.assertEqual(result.stdout, "")

            output = self.query_warehouse(cwd, db, "WH-A")
            self.assertIn("批次总数=2", output)
            self.assertIn("数量=18", output)
            self.assertIn("数量=12", output)

    def test_split_blank_codes_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)
            result = self.invoke_in(
                cwd, "split", "--warehouse", "  ", "--product", "SKU-1001",
                "--db", db, "--lot", "LOT-1", "--into", "A,10", "--into", "B,8",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("仓库代码", result.stderr)
            result = self.invoke_in(
                cwd, "split", "--warehouse", "WH-A", "--product", "SKU-1001",
                "--db", db, "--lot", "   ", "--into", "A,10", "--into", "B,8",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("来源批次号", result.stderr)

    def test_merge_books_target_with_combined_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)

            result = self.invoke_in(
                cwd, "merge", "--warehouse", "WH-A", "--product", "SKU-1001",
                "--db", db, "--lot", " LOT-1 ", "--lot", "LOT-2",
                "--into", " LOT-9 ",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("合并成功", result.stdout)
            self.assertIn("数量=30", result.stdout)

            output = self.query_warehouse(cwd, db, "WH-A")
            self.assertIn("批次总数=1", output)
            self.assertNotIn("批次号=LOT-1", output)
            self.assertNotIn("批次号=LOT-2", output)
            self.assertIn("批次号=LOT-9 生产日期=2024-03-01 有效期至=2025-04-02 数量=30", output)

    def test_merge_rejections_leave_ledger_unchanged(self) -> None:
        cases = [
            (["--lot", "LOT-1", "--lot", "LOT-X", "--into", "T"], "落账"),
            (["--lot", "LOT-1", "--lot", "LOT-1", "--into", "T"], "必须不同"),
            (["--lot", "LOT-1", "--lot", "LOT-2", "--into", "LOT-1"], "不得等于"),
            (["--lot", "LOT-1", "--into", "T"], "恰好两个"),
            (["--lot", "LOT-1", "--lot", "LOT-2", "--into", "  "], "目标批次号"),
            (["--lot", "LOT-1", "--lot", " ", "--into", "T"], "来源批次号"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)
            for extra, hint in cases:
                with self.subTest(hint=hint):
                    result = self.invoke_in(
                        cwd, "merge", "--warehouse", "WH-A",
                        "--product", "SKU-1001", "--db", db, *extra,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(hint, result.stderr)
                    self.assertEqual(result.stdout, "")

            # Target lot already booked elsewhere in the same scope.
            result = self.invoke_in(
                cwd, "merge", "--warehouse", "WH-A", "--product", "SKU-1001",
                "--db", db, "--lot", "LOT-1", "--lot", "LOT-2",
                "--into", "LOT-2",
            )
            self.assertNotEqual(result.returncode, 0)

            output = self.query_warehouse(cwd, db, "WH-A")
            self.assertIn("批次总数=2", output)
            self.assertIn("数量=18", output)
            self.assertIn("数量=12", output)

    def test_merge_rejects_zero_quantity_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)
            # Draw LOT-2 down to zero via a transfer.
            result = self.invoke_in(
                cwd, "transfer", "--order", "TR-1", "--from", "WH-A",
                "--to", "WH-B", "--product", "SKU-1001", "--db", db,
                "--item", "LOT-2,12",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            result = self.invoke_in(
                cwd, "merge", "--warehouse", "WH-A", "--product", "SKU-1001",
                "--db", db, "--lot", "LOT-1", "--lot", "LOT-2",
                "--into", "LOT-9",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("LOT-2", result.stderr)
            self.assertIn("0", result.stderr)

            output = self.query_warehouse(cwd, db, "WH-A")
            self.assertIn("批次总数=2", output)
            self.assertIn("数量=18", output)

    def test_split_and_merge_do_not_touch_transfer_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)
            result = self.invoke_in(
                cwd, "transfer", "--order", "TR-1", "--from", "WH-A",
                "--to", "WH-B", "--product", "SKU-1001", "--db", db,
                "--item", "LOT-1,10",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            # Split the remaining LOT-1 balance and merge LOT-2 elsewhere.
            result = self.invoke_in(
                cwd, "split", "--warehouse", "WH-A", "--product", "SKU-1001",
                "--db", db, "--lot", "LOT-1",
                "--into", "LOT-1A,5", "--into", "LOT-1B,3",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self.invoke_in(
                cwd, "merge", "--warehouse", "WH-A", "--product", "SKU-1001",
                "--db", db, "--lot", "LOT-1A", "--lot", "LOT-2",
                "--into", "LOT-M",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            # Transfer order and its allocation lines are untouched.
            result = self.invoke_in(
                cwd, "transfer-query", "--order", "TR-1", "--db", db
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("状态=in_transit", result.stdout)
            self.assertIn("批次号=LOT-1 调出数量=10 实收数量=0", result.stdout)

            output = self.query_warehouse(cwd, db, "WH-A")
            self.assertIn("批次总数=2", output)
            self.assertIn("批次号=LOT-1B 生产日期=2024-03-01 有效期至=2025-03-01 数量=3", output)
            self.assertIn("批次号=LOT-M 生产日期=2024-03-01 有效期至=2025-04-02 数量=17", output)

    def test_split_merge_scoped_to_warehouse_and_product(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)
            result = self.invoke_in(
                cwd,
                "register", "--warehouse", "WH-B", "--product", "SKU-1001",
                "--db", db, "--batch", "LOT-1,2024-01-01,2025-01-01,4",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            # Splitting LOT-1 in WH-A must not see WH-B's LOT-1 as a conflict.
            result = self.invoke_in(
                cwd, "split", "--warehouse", "WH-A", "--product", "SKU-1001",
                "--db", db, "--lot", "LOT-1",
                "--into", "LOT-1A,10", "--into", "LOT-1B,8",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            other = self.query_warehouse(cwd, db, "WH-B")
            self.assertIn("批次总数=1", other)
            self.assertIn("数量=4", other)

    def test_help_mentions_split_and_merge(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "stock_transfer", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=ENV,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("split", result.stdout)
        self.assertIn("merge", result.stdout)


if __name__ == "__main__":
    unittest.main()
