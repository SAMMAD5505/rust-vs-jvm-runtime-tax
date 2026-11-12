"""Regression tests for the Parquet output verifier."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


VERIFY_PATH = Path(__file__).resolve().parents[1] / "verify.py"
spec = importlib.util.spec_from_file_location("rvj_verify", VERIFY_PATH)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class VerifyOutputTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.directory = Path(self.temp_dir.name)

    def write_output(self, name, hours, sums=None):
        """Create a small Parquet output for testing."""
        n = len(hours)
        if sums is None:
            sums = [1.0] * n

        table = pa.table({
            "hour_bucket": pa.array(hours, type=pa.int64()),
            "count": pa.array([1] * n, type=pa.int64()),
            "sum_metric": pa.array(sums, type=pa.float64()),
            "min_metric": pa.array([1.0] * n, type=pa.float64()),
            "max_metric": pa.array([1.0] * n, type=pa.float64()),
            "checksum": pa.array([0] * n, type=pa.uint64()),
        })

        path = self.directory / name
        pq.write_table(table, path)
        return path

    def test_identical_sorted_outputs_pass(self):
        first = self.write_output("rust.parquet", [1, 2, 3])
        second = self.write_output("scala.parquet", [1, 2, 3])

        errors = verify.compare_outputs([first, second])

        self.assertEqual(errors, [])

    def test_duplicate_hour_buckets_fail(self):
        first = self.write_output("rust.parquet", [1, 1, 2])
        second = self.write_output("scala.parquet", [1, 1, 2])

        errors = verify.compare_outputs([first, second])

        self.assertTrue(
            any("not strictly ascending" in error for error in errors)
        )

    def test_negative_zero_differs_from_positive_zero(self):
        first = self.write_output(
            "rust.parquet", [1], sums=[0.0]
        )
        second = self.write_output(
            "scala.parquet", [1], sums=[-0.0]
        )

        errors = verify.compare_outputs([first, second])

        self.assertTrue(
            any("sum_metric differs" in error for error in errors)
        )


if __name__ == "__main__":
    unittest.main()
