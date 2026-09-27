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
        self.assertIn("receive", result.stdout)


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

    def seed(self, cwd: Path, db: str) -> None:
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
            "--item", "LOT-1,10",
            "--item", "LOT-2,12",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_full_receipt_credits_target_and_closes_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)

            result = self.invoke_in(
                cwd,
                "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,10",
                "--received", " LOT-2 , 12 ",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("调拨单号=TR-001", result.stdout)
            self.assertIn("状态=received", result.stdout)
            self.assertIn("实收数量=22", result.stdout)

            # Everything reads back from a separate process invocation.
            result = self.invoke_in(
                cwd, "transfer-query", "--order", "TR-001", "--db", db
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("状态=received", result.stdout)
            self.assertIn("实收数量=22", result.stdout)
            self.assertIn("批次号=LOT-1 调出数量=10 实收数量=10", result.stdout)
            self.assertIn("批次号=LOT-2 调出数量=12 实收数量=12", result.stdout)

            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-B",
                "--product", "SKU-1001", "--db", db
            )
            self.assertIn("批次总数=2", result.stdout)
            self.assertIn("LOT-1", result.stdout)
            self.assertIn("2024-03-01", result.stdout)
            self.assertIn("2025-03-01", result.stdout)
            self.assertIn("数量=10", result.stdout)
            self.assertIn("数量=12", result.stdout)

            # The earlier source draw-down stays exactly as it was.
            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A",
                "--product", "SKU-1001", "--db", db
            )
            self.assertIn("数量=8", result.stdout)
            self.assertIn("数量=0", result.stdout)

    def test_discrepancy_and_zero_lot_are_booked_as_received(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)

            result = self.invoke_in(
                cwd,
                "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,7",
                "--received", "LOT-2,0",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("状态=received", result.stdout)
            self.assertIn("实收数量=7", result.stdout)

            # LOT-2 arrived with 0: no quantity and no new batch row.
            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-B",
                "--product", "SKU-1001", "--db", db
            )
            self.assertIn("批次总数=1", result.stdout)
            self.assertIn("LOT-1", result.stdout)
            self.assertNotIn("LOT-2", result.stdout)
            self.assertIn("数量=7", result.stdout)

            result = self.invoke_in(
                cwd, "transfer-query", "--order", "TR-001", "--db", db
            )
            self.assertIn("批次号=LOT-1 调出数量=10 实收数量=7", result.stdout)
            self.assertIn("批次号=LOT-2 调出数量=12 实收数量=0", result.stdout)

    def test_existing_target_lot_accumulates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)
            self.invoke_in(
                cwd,
                "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,10",
                "--received", "LOT-2,12",
            )
            # A second in-transit order of the same lots into WH-B.
            result = self.invoke_in(
                cwd,
                "transfer", "--order", "TR-002", "--from", "WH-A",
                "--to", "WH-B", "--product", "SKU-1001", "--db", db,
                "--item", "LOT-1,4",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self.invoke_in(
                cwd,
                "receive", "--order", "TR-002", "--db", db,
                "--received", "LOT-1,4",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-B",
                "--product", "SKU-1001", "--db", db
            )
            self.assertIn("批次总数=2", result.stdout)
            self.assertIn("数量=14", result.stdout)
            self.assertIn("数量=12", result.stdout)

    def test_unknown_order_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            result = self.invoke_in(
                cwd,
                "receive", "--order", "NOPE", "--db", db,
                "--received", "LOT-1,1",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("NOPE", result.stderr)
            self.assertIn("不存在", result.stderr)
            self.assertEqual(result.stdout, "")
            result = self.invoke_in(
                cwd, "transfer-query", "--order", "NOPE", "--db", db
            )
            self.assertIn("批次数=0", result.stdout)

    def test_blank_order_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = self.invoke_in(
                cwd,
                "receive", "--order", "   ",
                "--db", str(cwd / "ledger.db"),
                "--received", "LOT-1,1",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("调拨单号", result.stderr)

    def test_repeat_receipt_rejected_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)
            self.invoke_in(
                cwd,
                "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,10",
                "--received", "LOT-2,12",
            )

            # A late, different receipt attempt must be refused as a whole.
            result = self.invoke_in(
                cwd,
                "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,1",
                "--received", "LOT-2,1",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("received", result.stderr)

            result = self.invoke_in(
                cwd, "transfer-query", "--order", "TR-001", "--db", db
            )
            self.assertIn("实收数量=22", result.stdout)
            self.assertIn("实收数量=10", result.stdout)
            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-B",
                "--product", "SKU-1001", "--db", db
            )
            self.assertIn("数量=10", result.stdout)
            self.assertIn("数量=12", result.stdout)

    def test_cancelled_order_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE transfers SET status = 'cancelled' "
                    "WHERE order_no = 'TR-001'"
                )
            result = self.invoke_in(
                cwd,
                "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,10",
                "--received", "LOT-2,12",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cancelled", result.stderr)

    def test_missing_or_unknown_lot_lines_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)

            result = self.invoke_in(
                cwd,
                "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,10",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("LOT-2", result.stderr)
            self.assertIn("覆盖", result.stderr)

            result = self.invoke_in(
                cwd,
                "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,10",
                "--received", "LOT-2,12",
                "--received", "LOT-X,1",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("实收行 3", result.stderr)
            self.assertIn("LOT-X", result.stderr)

    def test_duplicate_lot_in_one_receipt_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)
            result = self.invoke_in(
                cwd,
                "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,5",
                "--received", "LOT-1,5",
                "--received", "LOT-2,12",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("实收行 2", result.stderr)
            self.assertIn("重复", result.stderr)

    def test_received_over_shipped_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)
            result = self.invoke_in(
                cwd,
                "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,11",
                "--received", "LOT-2,12",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("实收行 1", result.stderr)
            self.assertIn("LOT-1", result.stderr)
            self.assertIn("大于调出数量 10", result.stderr)

    def test_invalid_received_lines_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)
            cases = [
                ("LOT-1,-4", "非负整数"),
                ("LOT-1,x", "非负整数"),
                ("LOT-1,1.5", "非负整数"),
                (" ,1", "批次号不能为空"),
                ("LOT-1", "格式"),
                ("LOT-1,1,9", "不接受汇总数量"),
            ]
            for received_line, hint in cases:
                with self.subTest(received_line=received_line):
                    result = self.invoke_in(
                        cwd,
                        "receive", "--order", "TR-001", "--db", db,
                        "--received", received_line,
                        "--received", "LOT-2,12",
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(hint, result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_rejection_leaves_order_and_both_warehouses_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = str(cwd / "ledger.db")
            self.seed(cwd, db)
            result = self.invoke_in(
                cwd,
                "receive", "--order", "TR-001", "--db", db,
                "--received", "LOT-1,99",
                "--received", "LOT-2,12",
            )
            self.assertNotEqual(result.returncode, 0)

            result = self.invoke_in(
                cwd, "transfer-query", "--order", "TR-001", "--db", db
            )
            self.assertIn("状态=in_transit", result.stdout)
            self.assertIn("实收数量=0", result.stdout)
            self.assertIn("实收数量=0", result.stdout)

            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-B",
                "--product", "SKU-1001", "--db", db
            )
            self.assertIn("批次总数=0", result.stdout)

            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A",
                "--product", "SKU-1001", "--db", db
            )
            self.assertIn("数量=8", result.stdout)
            self.assertIn("数量=0", result.stdout)


if __name__ == "__main__":
    unittest.main()
