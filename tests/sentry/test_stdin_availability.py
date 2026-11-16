"""Unavailable binary stdin fails before conversion or output publication."""
import gzip
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from otelwrench.sentry.cli import main
from .verify_output import verify_directory

EVENT = b'{"message":"synthetic"}'


class StdinAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output = self.base / "output"

    def options(self, compression, dry_run):
        args = ["--input-compression", compression, "--output-dir", str(self.output)]
        if dry_run:
            args.append("--dry-run")
        return args

    def assert_stdin_error(self, code, stdout, stderr):
        self.assertEqual(code, 2, stderr)
        self.assertEqual(stdout, "")
        self.assertIn("binary stdin", stderr)
        self.assertNotIn("Traceback", stderr)

    def run_without_descriptor(self, source, *args):
        # A real missing OS descriptor makes CPython initialize sys.stdin to None.
        return subprocess.run(
            ["bash", "-c", 'exec "$@" 0<&-', "bash", sys.executable, "-m", "otelwrench", "sentry",
             "convert", str(source), *args], cwd=self.base, capture_output=True, text=True, timeout=60)

    def test_closed_descriptor_rejects_plain_gzip_and_dry_run_without_output(self):
        for compression in ("none", "gzip"):
            for dry_run in (False, True):
                with self.subTest(compression=compression, dry_run=dry_run):
                    result = self.run_without_descriptor("-", *self.options(compression, dry_run))
                    self.assert_stdin_error(result.returncode, result.stdout, result.stderr)
                    self.assertEqual(list(self.base.iterdir()), [])

    def test_missing_and_closed_binary_interfaces_never_convert_or_publish(self):
        closed_binary = io.BytesIO(EVENT)
        closed_binary.close()
        closed_buffered = io.BufferedReader(io.BytesIO(EVENT))
        closed_buffered.close()
        closed_text = io.TextIOWrapper(io.BytesIO(EVENT))
        closed_text.close()
        states = [None, object(), io.StringIO(EVENT.decode()), SimpleNamespace(buffer=None),
                  SimpleNamespace(buffer=closed_binary), SimpleNamespace(buffer=closed_buffered), closed_text]
        self.addCleanup(states[2].close)
        # Also preserve an existing destination and detect any sibling staging directories.
        self.output.mkdir()
        sentinel = self.output / "keep"
        sentinel.write_bytes(b"untouched")
        for index, stdin in enumerate(states):
            for compression in ("none", "gzip"):
                for dry_run in (False, True):
                    with self.subTest(state=index, compression=compression, dry_run=dry_run), \
                            patch("sys.stdin", stdin), \
                            patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                            patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                            patch("otelwrench.sentry.cli.convert_bytes") as convert, \
                            patch("otelwrench.sentry.cli.write_output") as publish:
                        code = main(["convert", "-", *self.options(compression, dry_run)])
                        self.assert_stdin_error(code, stdout.getvalue(), stderr.getvalue())
                        convert.assert_not_called()
                        publish.assert_not_called()
                        self.assertEqual(list(self.base.iterdir()), [self.output])
                        self.assertEqual(list(self.output.iterdir()), [sentinel])
                        self.assertEqual(sentinel.read_bytes(), b"untouched")

    def test_binary_read_errors_are_diagnostics_without_closing_caller_stream(self):
        for error in (OSError("synthetic read failure"), ValueError("synthetic closed stream")):
            binary = Mock()
            binary.read.side_effect = error
            with self.subTest(error=error), patch("sys.stdin", SimpleNamespace(buffer=binary)), \
                    patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                    patch("sys.stderr", new_callable=io.StringIO) as stderr:
                code = main(["convert", "-", *self.options("none", False)])
                self.assert_stdin_error(code, stdout.getvalue(), stderr.getvalue())
                self.assertIn(str(error), stderr.getvalue())
                binary.close.assert_not_called()
                self.assertEqual(list(self.base.iterdir()), [])

    def test_invalid_budgets_precede_binary_stdin_access(self):
        class ForbiddenStdin:
            @property
            def buffer(self):
                raise AssertionError("stdin accessed before argument rejection")

        for flag in ("--max-input-bytes", "--max-decompressed-bytes"):
            with self.subTest(flag=flag), patch("sys.stdin", ForbiddenStdin()), \
                    patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                    patch("sys.stderr", new_callable=io.StringIO) as stderr:
                with self.assertRaises(SystemExit) as error:
                    main(["convert", "-", "--dry-run", flag, str(sys.maxsize)])
                self.assertEqual(error.exception.code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn(f"argument {flag}: byte budget", stderr.getvalue())
                self.assertEqual(list(self.base.iterdir()), [])

    def test_file_input_with_closed_descriptor_keeps_exact_budgets_and_strict_policy(self):
        source = self.base / "input"
        for compression in ("none", "gzip"):
            data = gzip.compress(EVENT, mtime=0) if compression == "gzip" else EVENT
            source.write_bytes(data)
            for dry_run in (False, True):
                self.output = self.base / f"{compression}-{dry_run}"
                with self.subTest(compression=compression, dry_run=dry_run):
                    result = self.run_without_descriptor(
                        source, *self.options(compression, dry_run), "--max-input-bytes", str(len(data)),
                        "--max-decompressed-bytes", str(len(EVENT)), "--fail-on-trace-conflicts")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    if dry_run:
                        self.assertEqual(json.loads(result.stdout)["summary"]["logRecords"], 1)
                        self.assertFalse(self.output.exists())
                    else:
                        self.assertEqual(verify_directory(self.output, data),
                                         {"items": 1, "logs": 1, "spans": 0, "losses": 0})

    def test_empty_available_binary_stdin_retains_input_validation_error(self):
        with io.BytesIO() as binary, patch("sys.stdin", SimpleNamespace(buffer=binary)), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            code = main(["convert", "-", "--dry-run"])
            self.assertEqual(code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("empty or padded envelope header", stderr.getvalue())
            self.assertNotIn("binary stdin", stderr.getvalue())
            self.assertFalse(binary.closed)
            self.assertEqual(list(self.base.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
