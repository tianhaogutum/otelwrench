"""A custom binary stdin buffer must expose a callable read method."""
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from otelwrench.sentry.cli import main

EVENT = b'{"message":"synthetic"}'


class StdinInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output = self.base / "output"

    def test_missing_none_and_noncallable_read_never_convert_or_publish(self):
        # Include falsy and truthy non-callables; diagnostics must not render them.
        class Unprintable:
            def __repr__(self):
                raise AssertionError("read value rendered")

        states = [object(), SimpleNamespace(close=Mock())]
        states += [SimpleNamespace(read=value, close=Mock())
                   for value in (None, 0, "synthetic", Unprintable())]
        for existing in (False, True):
            if existing:
                self.output.mkdir()
                (self.output / "keep").write_bytes(b"untouched")
            for index, binary in enumerate(states):
                for compression in ("none", "gzip"):
                    for dry_run in (False, True):
                        with self.subTest(existing=existing, state=index,
                                          compression=compression, dry_run=dry_run):
                            stdin = SimpleNamespace(buffer=binary, close=Mock())
                            args = ["convert", "-", "--input-compression", compression,
                                    "--output-dir", str(self.output)]
                            if dry_run:
                                args.append("--dry-run")
                            with patch("sys.stdin", stdin), \
                                    patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                                    patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                                    patch("otelwrench.sentry.cli.convert_bytes") as convert, \
                                    patch("otelwrench.sentry.cli.write_output") as publish:
                                code = main(args)
                                self.assertEqual(code, 2, stderr.getvalue())
                                self.assertEqual(stdout.getvalue(), "")
                                self.assertIn("binary stdin must provide a callable read method", stderr.getvalue())
                                self.assertNotIn("Traceback", stderr.getvalue())
                                self.assertNotIn("synthetic", stderr.getvalue())
                                convert.assert_not_called()
                                publish.assert_not_called()
                            self.assertIs(stdin.buffer, binary)
                            stdin.close.assert_not_called()
                            if hasattr(binary, "close"):
                                binary.close.assert_not_called()
                            self.assertEqual(list(self.base.iterdir()), [self.output] if existing else [])
                            if existing:
                                self.assertEqual(list(self.output.iterdir()), [self.output / "keep"])
                                self.assertEqual((self.output / "keep").read_bytes(), b"untouched")

    def test_invalid_budgets_precede_custom_interface_validation(self):
        for read_state in ({}, {"read": None}, {"read": 1}):
            for flag in ("--max-input-bytes", "--max-decompressed-bytes"):
                for budget in (0, sys.maxsize):
                    for compression in ("none", "gzip"):
                        for dry_run in (False, True):
                            with self.subTest(read_state=read_state, flag=flag, budget=budget,
                                              compression=compression, dry_run=dry_run):
                                binary = SimpleNamespace(**read_state, close=Mock())
                                args = ["convert", "-", "--output-dir", str(self.output),
                                        "--input-compression", compression, flag, str(budget)]
                                if dry_run:
                                    args.append("--dry-run")
                                with patch("sys.stdin", SimpleNamespace(buffer=binary)), \
                                        patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                                        patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                                        patch("otelwrench.sentry.cli.read_binary_stdin") as read, \
                                        patch("otelwrench.sentry.cli.convert_bytes") as convert, \
                                        patch("otelwrench.sentry.cli.write_output") as publish:
                                    with self.assertRaises(SystemExit) as error:
                                        main(args)
                                    self.assertEqual(error.exception.code, 2)
                                    self.assertEqual(stdout.getvalue(), "")
                                    self.assertIn(f"argument {flag}: byte budget", stderr.getvalue())
                                    self.assertNotIn("binary stdin", stderr.getvalue())
                                    self.assertNotIn("Traceback", stderr.getvalue())
                                    read.assert_not_called()
                                    convert.assert_not_called()
                                    publish.assert_not_called()
                                binary.close.assert_not_called()
                                self.assertEqual(list(self.base.iterdir()), [])

    def test_required_output_argument_precedes_read_attribute_access(self):
        class ForbiddenRead:
            @property
            def read(self):
                raise AssertionError("read attribute accessed before argument rejection")

        with patch("sys.stdin", SimpleNamespace(buffer=ForbiddenRead())), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as error:
                main(["convert", "-"])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("--output-dir is required", stderr.getvalue())
            self.assertEqual(list(self.base.iterdir()), [])

    def test_callable_read_and_converter_programming_errors_are_not_masked(self):
        for error_type in (AttributeError, TypeError):
            for stage in ("read", "convert"):
                with self.subTest(error_type=error_type, stage=stage), io.BytesIO(EVENT) as binary:
                    error = error_type("synthetic programming error")
                    with patch("sys.stdin", SimpleNamespace(buffer=binary)), \
                            patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                            patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                            patch("otelwrench.sentry.cli.write_output") as publish:
                        target = (patch.object(binary, "read", side_effect=error) if stage == "read"
                                  else patch("otelwrench.sentry.cli.convert_bytes", side_effect=error))
                        with target, self.assertRaises(error_type) as raised:
                            main(["convert", "-", "--output-dir", str(self.output)])
                        self.assertIs(raised.exception, error)
                        self.assertEqual(stdout.getvalue(), "")
                        self.assertEqual(stderr.getvalue(), "")
                        publish.assert_not_called()
                        self.assertFalse(binary.closed)
                        self.assertEqual(list(self.base.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
