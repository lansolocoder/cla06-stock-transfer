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


if __name__ == "__main__":
    unittest.main()
