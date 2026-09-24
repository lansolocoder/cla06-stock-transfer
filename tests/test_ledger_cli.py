"""库存登记与批次查询的端到端测试。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LedgerCliTests(unittest.TestCase):
    def invoke(self, *arguments: str, cwd: str) -> subprocess.CompletedProcess[str]:
        env = {"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}
        return subprocess.run(
            [sys.executable, "-m", "stock_transfer", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def query(self, warehouse: str = "WH-A", product: str = "SKU-1001") -> dict:
        result = self.invoke(
            "query", "--warehouse", warehouse, "--product", product, cwd=self.cwd
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_register_multiple_batches_and_query(self) -> None:
        result = self.invoke(
            "register",
            "--warehouse", "WH-A",
            "--product", "SKU-1001",
            "--batch", "LOT-2024-001,2024-03-01,2025-03-01,18",
            "--batch", "LOT-2024-002,2024-04-02,2025-04-02,12",
            cwd=self.cwd,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WH-A", result.stdout)
        self.assertIn("SKU-1001", result.stdout)
        self.assertIn("30", result.stdout)
        self.assertIn("2", result.stdout)

        data = self.query()
        self.assertEqual(
            data["batches"],
            [
                {
                    "batch_no": "LOT-2024-001",
                    "production_date": "2024-03-01",
                    "expiry_date": "2025-03-01",
                    "quantity": 18,
                },
                {
                    "batch_no": "LOT-2024-002",
                    "production_date": "2024-04-02",
                    "expiry_date": "2025-04-02",
                    "quantity": 12,
                },
            ],
        )

    def test_new_distinct_batch_is_appended(self) -> None:
        first = self.invoke(
            "register",
            "--warehouse", "WH-A",
            "--product", "SKU-1001",
            "--batch", "LOT-1,2024-03-01,2025-03-01,18",
            cwd=self.cwd,
        )
        self.assertEqual(first.returncode, 0, first.stderr)

        second = self.invoke(
            "register",
            "--warehouse", "WH-A",
            "--product", "SKU-1001",
            "--batch", "LOT-2,2024-05-01,2025-05-01,5",
            cwd=self.cwd,
        )
        self.assertEqual(second.returncode, 0, second.stderr)

        data = self.query()
        self.assertEqual([b["batch_no"] for b in data["batches"]], ["LOT-1", "LOT-2"])
        self.assertEqual([b["quantity"] for b in data["batches"]], [18, 5])

    def test_batch_no_duplicate_with_existing_rejects_and_keeps_data(self) -> None:
        first = self.invoke(
            "register",
            "--warehouse", "WH-A",
            "--product", "SKU-1001",
            "--batch", "LOT-1,2024-03-01,2025-03-01,18",
            cwd=self.cwd,
        )
        self.assertEqual(first.returncode, 0, first.stderr)

        bad = self.invoke(
            "register",
            "--warehouse", "WH-A",
            "--product", "SKU-1001",
            "--batch", "LOT-1,2024-06-01,2025-06-01,9",
            cwd=self.cwd,
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(bad.stdout, "")
        self.assertIn("批次行 1", bad.stderr)
        self.assertIn("批次号", bad.stderr)

        data = self.query()
        self.assertEqual(len(data["batches"]), 1)
        self.assertEqual(data["batches"][0]["quantity"], 18)

    def test_duplicate_batch_within_submission_rejects_whole_submission(self) -> None:
        bad = self.invoke(
            "register",
            "--warehouse", "WH-A",
            "--product", "SKU-1001",
            "--batch", "LOT-1,2024-03-01,2025-03-01,18",
            "--batch", "LOT-1,2024-04-02,2025-04-02,12",
            cwd=self.cwd,
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("批次行 2", bad.stderr)
        self.assertEqual(self.query()["batches"], [])

    def test_invalid_rows_reject_whole_submission_without_partial_records(self) -> None:
        cases = [
            ("LOT-X,2024-03-01,2024-03-01,10", "批次行 1", "有效期至"),
            ("LOT-X,2024-03-02,2024-03-01,10", "批次行 1", "有效期至"),
            ("LOT-X,2024-03-01,2025-03-01,0", "批次行 1", "数量"),
            ("LOT-X,2024-03-01,2025-03-01,-3", "批次行 1", "数量"),
            ("LOT-X,2024-03-01,2025-03-01,abc", "批次行 1", "数量"),
            ("LOT-X,2024-3-01,2025-03-01,10", "批次行 1", "生产日期"),
            ("LOT-X,2024-13-01,2025-03-01,10", "批次行 1", "生产日期"),
            ("LOT-X,2024/03/01,2025-03-01,10", "批次行 1", "生产日期"),
            ("  ,2024-03-01,2025-03-01,10", "批次行 1", "批次号"),
        ]
        for line, row_label, field in cases:
            with self.subTest(line=line):
                result = self.invoke(
                    "register",
                    "--warehouse", "WH-A",
                    "--product", "SKU-1001",
                    "--batch", line,
                    cwd=self.cwd,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(row_label, result.stderr)
                self.assertIn(field, result.stderr)
                self.assertEqual(self.query()["batches"], [])

    def test_one_bad_row_rejects_all_rows(self) -> None:
        result = self.invoke(
            "register",
            "--warehouse", "WH-A",
            "--product", "SKU-1001",
            "--batch", "LOT-GOOD,2024-03-01,2025-03-01,10",
            "--batch", "LOT-BAD,2024-04-01,2024-04-01,20",
            cwd=self.cwd,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("批次行 2", result.stderr)
        self.assertEqual(self.query()["batches"], [])

    def test_empty_warehouse_or_product_code_rejected(self) -> None:
        result = self.invoke(
            "register",
            "--warehouse", "   ",
            "--product", "SKU-1001",
            "--batch", "LOT-1,2024-03-01,2025-03-01,10",
            cwd=self.cwd,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("仓库代码", result.stderr)

    def test_query_empty_returns_empty_list_with_zero_exit(self) -> None:
        result = self.invoke(
            "query", "--warehouse", "WH-A", "--product", "SKU-9999", cwd=self.cwd
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["batches"], [])

    def test_warehouse_and_product_are_independent_scopes(self) -> None:
        for warehouse, product, batch_no in [
            ("WH-A", "SKU-1001", "LOT-1"),
            ("WH-B", "SKU-1001", "LOT-1"),
            ("WH-A", "SKU-2002", "LOT-1"),
        ]:
            result = self.invoke(
                "register",
                "--warehouse", warehouse,
                "--product", product,
                "--batch", f"{batch_no},2024-03-01,2025-03-01,7",
                cwd=self.cwd,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(len(self.query("WH-A", "SKU-1001")["batches"]), 1)
        self.assertEqual(len(self.query("WH-B", "SKU-1001")["batches"]), 1)
        self.assertEqual(len(self.query("WH-A", "SKU-2002")["batches"]), 1)
        self.assertEqual(self.query("WH-B", "SKU-2002")["batches"], [])

    def test_ledger_file_created_in_cwd(self) -> None:
        self.assertFalse((Path(self.cwd) / "stock_ledger.db").exists())
        result = self.invoke(
            "register",
            "--warehouse", "WH-A",
            "--product", "SKU-1001",
            "--batch", "LOT-1,2024-03-01,2025-03-01,1",
            cwd=self.cwd,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((Path(self.cwd) / "stock_ledger.db").exists())


if __name__ == "__main__":
    unittest.main()
