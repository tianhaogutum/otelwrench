"""Only bytes results, including empty bytes EOF, can complete binary input."""
import gzip
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from otelwrench.sentry.cli import main
from .verify_output import verify_directory

EVENT = b'{"message":"synthetic"}'


class BinaryReadResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output = self.base / "output"

    def assert_rejected(self, binary, compression, dry_run):
        args = ["convert", "-", "--input-compression", compression,
                "--output-dir", str(self.output)]
        if dry_run:
            args.append("--dry-run")
        before = {p.relative_to(self.base): p.read_bytes()
                  for p in self.base.rglob("*") if p.is_file()}
        with patch("sys.stdin", SimpleNamespace(buffer=binary)), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                patch("otelwrench.sentry.cli.convert_bytes") as convert, \
                patch("otelwrench.sentry.cli.write_output") as publish:
            code = main(args)
            self.assertEqual(code, 2, stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("input read must return bytes", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertNotIn("synthetic", stderr.getvalue())  # Do not echo input values.
            convert.assert_not_called()
            publish.assert_not_called()
        after = {p.relative_to(self.base): p.read_bytes()
                 for p in self.base.rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        expected = [self.output] if self.output.exists() else []
        self.assertEqual(list(self.base.iterdir()), expected)  # No sibling staging.

    def test_text_buffer_empty_partial_and_complete_never_converts_or_publishes(self):
        for text in ("", EVENT[:10].decode(), EVENT.decode()):
            for compression in ("none", "gzip"):
                for dry_run in (False, True):
                    with self.subTest(text=text, compression=compression, dry_run=dry_run), \
                            io.StringIO(text) as binary:
                        self.assert_rejected(binary, compression, dry_run)
                        self.assertFalse(binary.closed)

    def test_unsupported_results_after_valid_bytes_never_complete_an_existing_destination(self):
        self.output.mkdir()
        (self.output / "keep").write_bytes(b"untouched")
        # Empty non-bytes must not act as EOF; truthy results must not reach len/join.
        unsupported = ("", EVENT.decode(), 0, 1, False, [], [1], object(),
                       bytearray(), bytearray(EVENT), memoryview(b""), memoryview(EVENT))
        for result in unsupported:
            for compression in ("none", "gzip"):
                data = gzip.compress(EVENT, mtime=0) if compression == "gzip" else EVENT
                for prefix in ([], [data]):
                    for dry_run in (False, True):
                        with self.subTest(result=result, compression=compression,
                                          prefix=bool(prefix), dry_run=dry_run):
                            binary = Mock()
                            binary.read.side_effect = [*prefix, result, b""]
                            self.assert_rejected(binary, compression, dry_run)
                            self.assertEqual(binary.read.call_count, len(prefix) + 1)
                            binary.close.assert_not_called()

    def test_invalid_budgets_still_reject_before_reading_custom_binary_stdin(self):
        for flag in ("--max-input-bytes", "--max-decompressed-bytes"):
            with self.subTest(flag=flag):
                binary = Mock()
                binary.read.return_value = EVENT.decode()
                with patch("sys.stdin", SimpleNamespace(buffer=binary)), \
                        patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                        patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    with self.assertRaises(SystemExit) as error:
                        main(["convert", "-", "--output-dir", str(self.output), flag, "0"])
                    self.assertEqual(error.exception.code, 2)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn(f"argument {flag}: byte budget", stderr.getvalue())
                    self.assertNotIn("input read", stderr.getvalue())
                binary.read.assert_not_called()
                binary.close.assert_not_called()
                self.assertEqual(list(self.base.iterdir()), [])

    def test_bytes_and_bytes_subclasses_with_real_eof_preserve_plain_and_gzip_conversion(self):
        class BinaryBytes(bytes):
            pass

        for chunk_type in (bytes, BinaryBytes):
            for compression in ("none", "gzip"):
                data = gzip.compress(EVENT, mtime=0) if compression == "gzip" else EVENT
                for dry_run in (False, True):
                    with self.subTest(chunk_type=chunk_type, compression=compression, dry_run=dry_run):
                        self.output = self.base / f"{chunk_type.__name__}-{compression}-{dry_run}"
                        binary = Mock()
                        binary.read.side_effect = [chunk_type(data), chunk_type(b"")]
                        args = ["convert", "-", "--output-dir", str(self.output),
                                "--input-compression", compression, "--max-input-bytes", str(len(data)),
                                "--max-decompressed-bytes", str(len(EVENT)), "--fail-on-trace-conflicts"]
                        if dry_run:
                            args.append("--dry-run")
                        with patch("sys.stdin", SimpleNamespace(buffer=binary)), \
                                patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                                patch("sys.stderr", new_callable=io.StringIO) as stderr:
                            self.assertEqual(main(args), 0, stderr.getvalue())
                            self.assertEqual(stderr.getvalue(), "")
                            self.assertEqual(json.loads(stdout.getvalue())["summary"]["logRecords"]
                                             if dry_run else json.loads(stdout.getvalue())["logRecords"], 1)
                        if dry_run:
                            self.assertFalse(self.output.exists())
                        else:
                            self.assertEqual(verify_directory(self.output, data),
                                             {"items": 1, "logs": 1, "spans": 0, "losses": 0})
                        binary.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
