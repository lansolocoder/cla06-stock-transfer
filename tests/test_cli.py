"""Checks for the documented command-line entry point."""

import os
from pathlib import Path
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

    def register(self, cwd: Path) -> None:
        result = self.invoke_in(
            cwd,
            "register", "--warehouse", "WH-A", "--product", "SKU-1001",
            "--batch", "LOT-1,2024-01-01,2025-01-01,10",
            "--batch", "LOT-2,2024-02-01,2025-02-01,6",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_transfer_then_query_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd)
            result = self.invoke_in(
                cwd,
                "transfer", "--order", "TR-0001",
                "--source", "WH-A", "--target", "WH-B", "--product", "SKU-1001",
                "--line", "LOT-1,4",
                "--line", "LOT-2,6",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("单号=TR-0001", result.stdout)
            self.assertIn("状态=in_transit", result.stdout)
            self.assertIn("实收数量=0", result.stdout)

            # Source stock is deducted; the drained batch is gone.
            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A", "--product", "SKU-1001"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("批次总数=1", result.stdout)
            self.assertIn("批次号=LOT-1", result.stdout)
            self.assertIn("数量=6", result.stdout)
            self.assertNotIn("LOT-2", result.stdout)

            # The order is readable from a separate process invocation.
            result = self.invoke_in(cwd, "transfer-query", "--order", "TR-0001")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("调拨单号=TR-0001", result.stdout)
            self.assertIn("来源仓=WH-A", result.stdout)
            self.assertIn("目标仓=WH-B", result.stdout)
            self.assertIn("商品=SKU-1001", result.stdout)
            self.assertIn("状态=in_transit", result.stdout)
            self.assertIn("实收数量=0", result.stdout)
            self.assertIn("批次号=LOT-1 调出数量=4", result.stdout)
            self.assertIn("批次号=LOT-2 调出数量=6", result.stdout)

    def test_transfer_query_missing_order_is_empty_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            result = self.invoke_in(cwd, "transfer-query", "--order", "TR-X")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("TR-X", result.stdout)
            self.assertFalse((cwd / "stock_ledger.db").exists())

    def test_duplicate_order_rejected_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd)
            self.invoke_in(
                cwd,
                "transfer", "--order", "TR-0001",
                "--source", "WH-A", "--target", "WH-B", "--product", "SKU-1001",
                "--line", "LOT-1,4",
            )
            result = self.invoke_in(
                cwd,
                "transfer", "--order", "TR-0001",
                "--source", "WH-A", "--target", "WH-C", "--product", "SKU-1001",
                "--line", "LOT-2,3",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("TR-0001", result.stderr)

            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A", "--product", "SKU-1001"
            )
            self.assertIn("数量=6", result.stdout)  # LOT-1: 10 - 4
            self.assertIn("批次号=LOT-2", result.stdout)  # LOT-2 untouched

    def test_transfer_validation_failures_leave_stock_untouched(self) -> None:
        cases = [
            # (extra transfer args, stderr hint)
            (("--source", "WH-A", "--target", "WH-A"), "不得与来源仓相同"),
            (("--source", "  ", "--target", "WH-B"), "来源仓"),
            (("--source", "WH-A", "--target", " "), "目标仓"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.register(cwd)
            for extra, hint in cases:
                with self.subTest(extra=extra):
                    result = self.invoke_in(
                        cwd,
                        "transfer", "--order", "TR-1", *extra,
                        "--product", "SKU-1001", "--line", "LOT-1,2",
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(hint, result.stderr)
                    self.assertEqual(result.stdout, "")

            line_cases = [
                ("LOT-1", "格式"),
                ("LOT-1,0", "正整数"),
                ("LOT-1,-2", "正整数"),
                ("LOT-1,abc", "正整数"),
                (" ,3", "批次号"),
                ("LOT-9,2", "未在来源仓"),
                ("LOT-1,11", "超出"),
                ("LOT-1,2,3", "格式"),
            ]
            for line, hint in line_cases:
                with self.subTest(line=line):
                    result = self.invoke_in(
                        cwd,
                        "transfer", "--order", "TR-2",
                        "--source", "WH-A", "--target", "WH-B",
                        "--product", "SKU-1001", "--line", line,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(hint, result.stderr)
                    self.assertEqual(result.stdout, "")

            # Duplicate lot within one order.
            result = self.invoke_in(
                cwd,
                "transfer", "--order", "TR-3",
                "--source", "WH-A", "--target", "WH-B", "--product", "SKU-1001",
                "--line", "LOT-1,2", "--line", "LOT-1,3",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("重复", result.stderr)

            # Nothing was booked and no stock was deducted.
            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A", "--product", "SKU-1001"
            )
            self.assertIn("批次总数=2", result.stdout)
            self.assertIn("数量=10", result.stdout)
            self.assertIn("数量=6", result.stdout)
            for order in ("TR-1", "TR-2", "TR-3"):
                result = self.invoke_in(cwd, "transfer-query", "--order", order)
                self.assertIn("无此调拨单", result.stdout)

    def test_transfer_db_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            db = cwd / "custom.db"
            self.invoke_in(
                cwd,
                "register", "--warehouse", "WH-A", "--product", "SKU-1",
                "--batch", "LOT-1,2024-01-01,2025-01-01,5", "--db", str(db),
            )
            result = self.invoke_in(
                cwd,
                "transfer", "--order", "TR-9",
                "--source", "WH-A", "--target", "WH-B", "--product", "SKU-1",
                "--line", "LOT-1,2", "--db", str(db),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = self.invoke_in(
                cwd, "transfer-query", "--order", "TR-9", "--db", str(db)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("状态=in_transit", result.stdout)
            result = self.invoke_in(
                cwd, "query", "--warehouse", "WH-A", "--product", "SKU-1",
                "--db", str(db),
            )
            self.assertIn("数量=3", result.stdout)


if __name__ == "__main__":
    unittest.main()
