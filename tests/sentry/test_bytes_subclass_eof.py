"""Only actual byte extent can establish EOF or consume a byte budget."""
import gzip
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from otelwrench.sentry.cli import main
from otelwrench.sentry.compression import read_bounded
from otelwrench.sentry.mapping import convert_bytes
from otelwrench.sentry.output import write_output
from otelwrench.sentry.parser import ConversionError
from .verify_output import verify_directory

EVENT = b'{"message":"synthetic"}'


class FalsyBytes(bytes):
    def __bool__(self):
        return False


class TruthyBytes(bytes):
    def __bool__(self):
        return True


class NoBoolBytes(bytes):
    def __bool__(self):
        raise AssertionError("reader must not invoke bytes-subclass truthiness")


class NoLenBytes(NoBoolBytes):
    def __len__(self):
        raise AssertionError("reader must use actual byte extent")


class BytesSubclassEOFTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def run_input(self, chunks, compression, dry_run, existing=False, budget=None):
        output = self.base / "output"
        if existing:
            output.mkdir()
            (output / "keep").write_bytes(b"untouched")
        binary = Mock()
        chunks = iter(chunks)

        def read(size):
            convert.assert_not_called()
            publish.assert_not_called()
            self.assertGreater(size, 0)
            self.assertLessEqual(size, 64 * 1024)
            try:
                result = next(chunks)
            except StopIteration:
                self.fail("reader continued past actual EOF or budget+1")
            if isinstance(result, Exception):
                raise result
            return result

        binary.read.side_effect = read
        args = ["convert", "-", "--input-compression", compression,
                "--output-dir", str(output), "--fail-on-trace-conflicts"]
        if dry_run:
            args.append("--dry-run")
        if budget is not None:
            args.extend(["--max-input-bytes", str(budget)])
        with patch("sys.stdin", SimpleNamespace(buffer=binary)), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                patch("otelwrench.sentry.cli.convert_bytes", wraps=convert_bytes) as convert, \
                patch("otelwrench.sentry.cli.write_output", wraps=write_output) as publish:
            code = main(args)
        binary.close.assert_not_called()
        return code, stdout.getvalue(), stderr.getvalue(), binary, convert, publish

    def clear_output(self):
        output = self.base / "output"
        if output.exists():
            for child in output.iterdir():
                child.unlink()
            output.rmdir()

    def assert_untouched(self, existing):
        output = self.base / "output"
        self.assertEqual(list(self.base.iterdir()), [output] if existing else [])
        if existing:
            self.assertEqual(list(output.iterdir()), [output / "keep"])
            self.assertEqual((output / "keep").read_bytes(), b"untouched")

    def test_invalid_falsy_suffix_is_retained_through_real_eof_before_validation(self):
        for compression in ("none", "gzip"):
            prefix = gzip.compress(EVENT, mtime=0) if compression == "gzip" else EVENT
            for dry_run in (False, True):
                for existing in (False, True):
                    with self.subTest(compression=compression, dry_run=dry_run, existing=existing):
                        code, stdout, stderr, binary, convert, publish = self.run_input(
                            [prefix, FalsyBytes(b"not EOF"), b""], compression, dry_run, existing)
                        self.assertEqual(code, 2, stderr)
                        self.assertEqual(stdout, "")
                        self.assertNotIn("Traceback", stderr)
                        self.assertIn("invalid", stderr)
                        self.assertEqual(binary.read.call_count, 3)
                        self.assertEqual(convert.call_args.args[0], prefix + b"not EOF")
                        publish.assert_not_called()
                        self.assert_untouched(existing)
                    self.clear_output()

    def test_complete_falsy_chunks_and_truthy_empty_eof_preserve_full_provenance(self):
        for chunk_type in (FalsyBytes, NoBoolBytes, NoLenBytes):
            for compression in ("none", "gzip"):
                data = gzip.compress(EVENT, mtime=0) if compression == "gzip" else EVENT
                for dry_run, existing in ((False, False), (True, False), (True, True)):
                    with self.subTest(chunk_type=chunk_type, compression=compression,
                                      dry_run=dry_run, existing=existing):
                        code, stdout, stderr, binary, convert, _ = self.run_input(
                            [chunk_type(data[:5]), chunk_type(data[5:]), TruthyBytes(b"")],
                            compression, dry_run, existing, budget=len(data))
                        self.assertEqual(code, 0, stderr)
                        self.assertEqual(stderr, "")
                        self.assertEqual(binary.read.call_count, 3)
                        self.assertEqual(convert.call_args.args[0], data)
                        if dry_run:
                            manifest = json.loads(stdout)
                            self.assertEqual(manifest["input"]["bytes"], len(data))
                            self.assertEqual(manifest, convert_bytes(data, input_compression=compression)["manifest.json"])
                            self.assert_untouched(existing)
                        else:
                            self.assertEqual(verify_directory(self.base / "output", data),
                                             {"items": 1, "logs": 1, "spans": 0, "losses": 0})
                    self.clear_output()

    def test_empty_subclass_eof_does_not_read_again(self):
        for eof in (TruthyBytes(b""), NoBoolBytes(b""), NoLenBytes(b"")):
            with self.subTest(eof_type=type(eof)):
                binary = Mock()
                binary.read.side_effect = [eof, AssertionError("read past EOF")]
                self.assertEqual(read_bounded(binary, 10), b"")
                self.assertEqual(binary.read.call_count, 1)
                binary.close.assert_not_called()

    def test_would_block_after_falsy_complete_prefix_never_converts(self):
        for compression in ("none", "gzip"):
            data = gzip.compress(EVENT, mtime=0) if compression == "gzip" else EVENT
            for dry_run in (False, True):
                for blocked in (None, BlockingIOError("would block")):
                    with self.subTest(compression=compression, dry_run=dry_run, blocked=blocked):
                        code, stdout, stderr, binary, convert, publish = self.run_input(
                            [FalsyBytes(data), blocked], compression, dry_run, existing=True)
                        self.assertEqual(code, 2, stderr)
                        self.assertEqual(stdout, "")
                        self.assertIn("input read would block", stderr)
                        self.assertEqual(binary.read.call_count, 2)
                        convert.assert_not_called()
                        publish.assert_not_called()
                        self.assert_untouched(True)
                    self.clear_output()

    def test_falsy_overflow_stops_at_detection_byte_without_eof_or_conversion(self):
        for compression in ("none", "gzip"):
            data = gzip.compress(EVENT, mtime=0) if compression == "gzip" else EVENT
            for dry_run in (False, True):
                with self.subTest(compression=compression, dry_run=dry_run):
                    code, stdout, stderr, binary, convert, publish = self.run_input(
                        [data[:-1], FalsyBytes(data[-1:])], compression, dry_run,
                        existing=True, budget=len(data) - 1)
                    self.assertEqual(code, 2, stderr)
                    self.assertEqual(stdout, "")
                    self.assertIn("input exceeds --max-input-bytes", stderr)
                    self.assertEqual([call.args[0] for call in binary.read.call_args_list], [len(data), 1])
                    convert.assert_not_called()
                    publish.assert_not_called()
                    self.assert_untouched(True)
                self.clear_output()

    def test_gzip_expansion_reader_uses_the_same_actual_extent_rules(self):
        for budget, overflow in ((len(EVENT), False), (len(EVENT) - 1, True)):
            with self.subTest(budget=budget):
                decoded = Mock()
                decoded.__enter__ = Mock(return_value=decoded)
                decoded.__exit__ = Mock(return_value=False)
                decoded.read.side_effect = [NoLenBytes(EVENT), TruthyBytes(b"")]
                with patch("otelwrench.sentry.compression.gzip.GzipFile", return_value=decoded):
                    if overflow:
                        with self.assertRaisesRegex(ConversionError, "gzip expansion exceeds"):
                            convert_bytes(gzip.compress(EVENT), input_compression="gzip",
                                          max_decompressed_bytes=budget)
                        self.assertEqual(decoded.read.call_count, 1)
                    else:
                        documents = convert_bytes(gzip.compress(EVENT), input_compression="gzip",
                                                  max_decompressed_bytes=budget)
                        self.assertEqual(documents["manifest.json"]["input"]["decompressedBytes"], len(EVENT))
                        self.assertEqual(decoded.read.call_count, 2)


if __name__ == "__main__":
    unittest.main()
