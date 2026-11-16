"""Byte-budget range, early rejection and allocation-safe read boundaries."""
import gzip
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from otelwrench.sentry.cli import main
from otelwrench.sentry.mapping import convert_bytes
from otelwrench.sentry.parser import ConversionError

MAX_BUDGET = sys.maxsize - 1
FLAGS = ("--max-input-bytes", "--max-decompressed-bytes")
EVENT = b'{"message":"synthetic"}'


class GuardedInput(io.BytesIO):
    """Fail before allocation if any caller asks for a huge read."""
    def read(self, size=-1):
        if not 0 < size <= 64 * 1024:
            raise AssertionError(f"unsafe read size: {size}")
        return super().read(size)


class ByteBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "input"
        self.output = self.base / "output"

    def run_cli(self, data, source="-", *args):
        return subprocess.run(
            [sys.executable, "-m", "otelwrench", "sentry", "convert", str(source), *map(str, args)],
            input=data, cwd=self.base, capture_output=True, timeout=60)

    def assert_budget_error(self, result, flag):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertIn(flag.encode(), result.stderr)
        self.assertIn(b"byte budget", result.stderr)
        self.assertIn(f"1..{MAX_BUDGET}".encode(), result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_reported_sys_maxsize_stdin_dry_run_regression(self):
        result = self.run_cli(EVENT, "-", "--dry-run", "--max-input-bytes", 9223372036854775807)
        self.assert_budget_error(result, "--max-input-bytes")
        self.assertEqual(list(self.base.iterdir()), [])

    def test_oversized_budgets_reject_files_and_stdin_without_output(self):
        for compression in ("none", "gzip"):
            data = gzip.compress(EVENT, mtime=0) if compression == "gzip" else EVENT
            self.source.write_bytes(data)
            for source in ("-", self.source):
                for flag in FLAGS:
                    for budget in (sys.maxsize, sys.maxsize + 1, 10**100):
                        for dry_run in (False, True):
                            with self.subTest(compression=compression, source=source, flag=flag,
                                              budget=budget, dry_run=dry_run):
                                args = ["--input-compression", compression, flag, budget,
                                        "--output-dir", self.output]
                                if dry_run:
                                    args.append("--dry-run")
                                result = self.run_cli(data, source, *args)
                                self.assert_budget_error(result, flag)
                                self.assertEqual(set(self.base.iterdir()), {self.source})

    def test_invalid_arguments_precede_all_input_decode_and_publication(self):
        for source in ("-", self.source):
            for compression in ("none", "gzip"):
                for flag in FLAGS:
                    with self.subTest(source=source, compression=compression, flag=flag):
                        # An object without .buffer also detects accessing stdin before parsing finishes.
                        with patch("sys.stdin", object()), \
                                patch("pathlib.Path.open", side_effect=AssertionError("input opened")) as opened, \
                                patch("otelwrench.sentry.cli.convert_bytes") as convert, \
                                patch("otelwrench.sentry.cli.write_output") as publish, \
                                patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                                patch("sys.stderr", new_callable=io.StringIO) as stderr:
                            with self.assertRaises(SystemExit) as error:
                                main(["convert", str(source), "--input-compression", compression,
                                      flag, str(sys.maxsize), "--output-dir", str(self.output)])
                        self.assert_budget_error(subprocess.CompletedProcess(
                            [], error.exception.code, stdout.getvalue().encode(), stderr.getvalue().encode()), flag)
                        opened.assert_not_called()
                        convert.assert_not_called()
                        publish.assert_not_called()
                        self.assertEqual(list(self.base.iterdir()), [])

    def test_upper_bound_stdin_uses_only_small_read_requests(self):
        with GuardedInput(EVENT) as binary, patch("sys.stdin", Mock(buffer=binary)), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(main(["convert", "-", "--dry-run", "--max-input-bytes", str(MAX_BUDGET),
                                   "--max-decompressed-bytes", str(MAX_BUDGET)]), 0)
            self.assertFalse(binary.closed)
            self.assertEqual(json.loads(stdout.getvalue())["summary"]["logRecords"], 1)

    def test_supported_upper_bound_real_files_and_stdin_plain_and_gzip(self):
        # Only tiny inputs are allocated even when the configured budget is enormous.
        for compression in ("none", "gzip"):
            data = gzip.compress(EVENT, mtime=0) if compression == "gzip" else EVENT
            self.source.write_bytes(data)
            for source in ("-", self.source):
                with self.subTest(compression=compression, source=source):
                    result = self.run_cli(data, source, "--dry-run", "--input-compression", compression,
                                          "--max-input-bytes", MAX_BUDGET,
                                          "--max-decompressed-bytes", MAX_BUDGET)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, b"")
                    self.assertEqual(json.loads(result.stdout)["summary"]["logRecords"], 1)
                    self.assertEqual(set(self.base.iterdir()), {self.source})

    def test_library_rejects_oversized_budgets_before_gzip_construction(self):
        for compression in ("none", "gzip"):
            for flag in FLAGS:
                for budget in (sys.maxsize, sys.maxsize + 1, 10**100):
                    with self.subTest(compression=compression, flag=flag, budget=budget), \
                            patch("otelwrench.sentry.compression.gzip.GzipFile") as decode:
                        with self.assertRaisesRegex(ConversionError, f"{flag}.*byte budget.*1\\.\\.{MAX_BUDGET}"):
                            convert_bytes(EVENT, input_compression=compression,
                                          **{flag[2:].replace("-", "_"): budget})
                        decode.assert_not_called()

    def test_library_upper_bound_gzip_read_requests_are_small(self):
        data = gzip.compress(EVENT, mtime=0)
        with GuardedInput(EVENT) as decoded, \
                patch("otelwrench.sentry.compression.gzip.GzipFile", return_value=decoded):
            result = convert_bytes(data, input_compression="gzip", max_input_bytes=MAX_BUDGET,
                                   max_decompressed_bytes=MAX_BUDGET)
        self.assertEqual(result["logs.json"], convert_bytes(EVENT)["logs.json"])

    def test_short_reads_continue_and_overflow_consumes_only_detection_byte(self):
        class ShortInput(GuardedInput):
            def read(self, size=-1):
                return super().read(min(size, 3))

        for budget, expected in ((len(EVENT), 0), (len(EVENT) - 2, 2)):
            with self.subTest(budget=budget), ShortInput(EVENT) as binary, \
                    patch("sys.stdin", Mock(buffer=binary)), \
                    patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                    patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(main(["convert", "-", "--dry-run", "--max-input-bytes", str(budget)]), expected)
                self.assertEqual(binary.tell(), min(len(EVENT), budget + 1))
                self.assertFalse(binary.closed)
                if expected:
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn("input exceeds --max-input-bytes", stderr.getvalue())
                else:
                    self.assertEqual(json.loads(stdout.getvalue())["summary"]["logRecords"], 1)


if __name__ == "__main__":
    unittest.main()
