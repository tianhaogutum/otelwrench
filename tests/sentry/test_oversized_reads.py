"""A binary reader must honor each requested size, including the 64 KiB cap."""
import gzip
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from otelwrench.sentry.cli import main
from otelwrench.sentry.mapping import convert_bytes
from otelwrench.sentry.parser import ConversionError
from .test_bytes_subclass_eof import FalsyBytes, NoLenBytes

EVENT = b'{"message":"synthetic"}'
DIAGNOSTIC = "binary input read returned more bytes than requested"


class UnderreportedBytes(bytes):
    def __len__(self):
        return 0

    def __repr__(self):
        raise AssertionError("reader must not render chunk contents")


class OversizedReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output = self.base / "output"

    def assert_rejected(self, prefix, oversized, budget, requests, compression,
                        dry_run, existing):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            output = base / "output"
            if existing:
                output.mkdir()
                (output / "keep").write_bytes(b"untouched")
            chunks = iter([*prefix, oversized])
            binary = Mock()

            def read(size):
                convert.assert_not_called()
                publish.assert_not_called()
                self.assertGreater(size, 0, "negative/zero follow-up read")
                self.assertLessEqual(size, 64 * 1024, "unbounded follow-up read")
                try:
                    return next(chunks)
                except StopIteration:
                    self.fail("read again after an oversized result")

            binary.read.side_effect = read
            stdin = SimpleNamespace(buffer=binary, close=Mock())
            args = ["convert", "-", "--input-compression", compression,
                    "--max-input-bytes", str(budget), "--output-dir", str(output),
                    "--fail-on-trace-conflicts", "--skip-unsupported"]
            if dry_run:
                args.append("--dry-run")
            with patch("sys.stdin", stdin), \
                    patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                    patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                    patch("otelwrench.sentry.cli.convert_bytes") as convert, \
                    patch("otelwrench.sentry.cli.write_output") as publish:
                self.assertEqual(main(args), 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn(DIAGNOSTIC, stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertNotIn("synthetic", stderr.getvalue())
            self.assertEqual([call.args[0] for call in binary.read.call_args_list], requests)
            convert.assert_not_called()
            publish.assert_not_called()
            self.assertIs(stdin.buffer, binary)
            stdin.close.assert_not_called()
            binary.close.assert_not_called()
            self.assertEqual(list(base.iterdir()), [output] if existing else [])
            if existing:
                self.assertEqual(list(output.iterdir()), [output / "keep"])
                self.assertEqual((output / "keep").read_bytes(), b"untouched")

    def test_first_and_later_oversized_results_at_small_budget(self):
        for compression in ("none", "gzip"):
            data = gzip.compress(EVENT, mtime=0) if compression == "gzip" else EVENT
            for later in (False, True):
                prefix = [data[:1]] if later else []
                chunk = data[1:] if later else data
                requests = [2, 1] if later else [2]
                for chunk_type in (bytes, FalsyBytes, NoLenBytes, UnderreportedBytes):
                    for dry_run in (False, True):
                        for existing in (False, True):
                            with self.subTest(compression=compression, later=later,
                                              chunk_type=chunk_type, dry_run=dry_run, existing=existing):
                                self.assert_rejected(prefix, chunk_type(chunk), 1, requests,
                                                     compression, dry_run, existing)

    def test_oversized_result_violates_per_read_cap_even_with_budget_remaining(self):
        # The result can fit the total remaining budget and still violate read(size).
        for later in (False, True):
            prefix = [b"x" * (64 * 1024)] if later else []
            requests = [64 * 1024] * (2 if later else 1)
            for chunk_type in (bytes, NoLenBytes, UnderreportedBytes):
                for compression in ("none", "gzip"):
                    for dry_run in (False, True):
                        for existing in (False, True):
                            with self.subTest(later=later, chunk_type=chunk_type,
                                              compression=compression, dry_run=dry_run, existing=existing):
                                self.assert_rejected(prefix, chunk_type(b"x" * (64 * 1024 + 1)),
                                                     3 * 64 * 1024, requests, compression, dry_run, existing)

    def test_shared_gzip_expansion_reader_rejects_oversized_actual_extent(self):
        for later in (False, True):
            for chunk_type in (bytes, FalsyBytes, NoLenBytes, UnderreportedBytes):
                with self.subTest(later=later, chunk_type=chunk_type):
                    decoded = Mock()
                    decoded.__enter__ = Mock(return_value=decoded)
                    decoded.__exit__ = Mock(return_value=False)
                    decoded.read.side_effect = [*([EVENT[:1]] if later else []),
                                                chunk_type(EVENT[1:] if later else EVENT),
                                                AssertionError("read after oversized result")]
                    with patch("otelwrench.sentry.compression.gzip.GzipFile", return_value=decoded):
                        with self.assertRaisesRegex(ConversionError, DIAGNOSTIC):
                            convert_bytes(gzip.compress(EVENT), input_compression="gzip",
                                          max_decompressed_bytes=1)
                    self.assertEqual([call.args[0] for call in decoded.read.call_args_list],
                                     [2, 1] if later else [2])

    def test_argument_errors_precede_noncompliant_reader(self):
        for options in ([], ["--dry-run", "--max-input-bytes", "0"],
                        ["--dry-run", "--max-decompressed-bytes", str(sys.maxsize)],
                        ["--dry-run", "--input-compression", "invalid"]):
            with self.subTest(options=options):
                binary = Mock()
                binary.read.side_effect = AssertionError("read before argument rejection")
                with patch("sys.stdin", SimpleNamespace(buffer=binary)), \
                        patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                        patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    with self.assertRaises(SystemExit) as error:
                        main(["convert", "-", *options])
                self.assertEqual(error.exception.code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn(DIAGNOSTIC, stderr.getvalue())
                binary.read.assert_not_called()
                binary.close.assert_not_called()
                self.assertEqual(list(self.base.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
