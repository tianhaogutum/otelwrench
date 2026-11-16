"""Would-block is never EOF: incomplete streams must not reach publication."""
import gzip
import io
import json
import os
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


class NonblockingStdinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output = self.base / "output"

    def run_pipe(self, data, *args, eof=False):
        read_fd, write_fd = os.pipe()
        try:
            os.set_blocking(read_fd, False)
            if data:
                self.assertEqual(os.write(write_fd, data), len(data))
            if eof:
                os.close(write_fd)
                write_fd = None
            # Otherwise the writer stays open through the entire CLI run.
            return subprocess.run(
                [sys.executable, "-m", "otelwrench", "sentry", "convert", "-", *args],
                stdin=read_fd, cwd=self.base, capture_output=True, timeout=60)
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

    def assert_would_block(self, code, stdout, stderr):
        self.assertEqual(code, 2, stderr)
        self.assertEqual(stdout, b"")
        self.assertIn(b"input read would block", stderr)
        self.assertIn(b"blocking binary input", stderr)
        self.assertNotIn(b"Traceback", stderr)

    def test_open_nonblocking_pipe_never_converts_or_publishes_before_eof(self):
        envelope = b'{}\n{"type":"event","length":23}\n' + EVENT + b'\n'
        for compression, complete in (("none", EVENT), ("none", envelope),
                                      ("gzip", gzip.compress(EVENT, mtime=0))):
            for data in (b"", complete[:len(complete) // 2], complete):
                for dry_run in (False, True):
                    with self.subTest(compression=compression, data=data, dry_run=dry_run):
                        args = ["--input-compression", compression, "--output-dir", str(self.output)]
                        if dry_run:
                            args.append("--dry-run")
                        result = self.run_pipe(data, *args)
                        self.assert_would_block(result.returncode, result.stdout, result.stderr)
                        self.assertEqual(list(self.base.iterdir()), [])

    def test_would_block_keeps_existing_destination_and_caller_stream_open(self):
        self.output.mkdir()
        sentinel = self.output / "keep"
        sentinel.write_bytes(b"untouched")
        for would_block in (None, BlockingIOError("synthetic would-block")):
            for prefix in ([], [EVENT]):
                with self.subTest(would_block=would_block, prefix=prefix):
                    binary = Mock()
                    binary.read.side_effect = [*prefix, would_block]
                    with patch("sys.stdin", SimpleNamespace(buffer=binary)), \
                            patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                            patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                            patch("otelwrench.sentry.cli.convert_bytes") as convert, \
                            patch("otelwrench.sentry.cli.write_output") as publish:
                        code = main(["convert", "-", "--output-dir", str(self.output)])
                        self.assert_would_block(code, stdout.getvalue().encode(), stderr.getvalue().encode())
                    convert.assert_not_called()
                    publish.assert_not_called()
                    binary.close.assert_not_called()
                    self.assertEqual(list(self.base.iterdir()), [self.output])
                    self.assertEqual(list(self.output.iterdir()), [sentinel])
                    self.assertEqual(sentinel.read_bytes(), b"untouched")

    def test_nonblocking_pipe_with_actual_eof_preserves_exact_plain_and_gzip_conversion(self):
        for compression in ("none", "gzip"):
            data = gzip.compress(EVENT, mtime=0) if compression == "gzip" else EVENT
            for dry_run in (False, True):
                with self.subTest(compression=compression, dry_run=dry_run):
                    self.output = self.base / f"{compression}-{dry_run}"
                    args = ["--input-compression", compression, "--output-dir", str(self.output),
                            "--max-input-bytes", str(len(data)),
                            "--max-decompressed-bytes", str(len(EVENT)), "--fail-on-trace-conflicts"]
                    if dry_run:
                        args.append("--dry-run")
                    result = self.run_pipe(data, *args, eof=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, b"")
                    if dry_run:
                        self.assertEqual(json.loads(result.stdout)["summary"]["logRecords"], 1)
                        self.assertFalse(self.output.exists())
                    else:
                        self.assertEqual(verify_directory(self.output, data),
                                         {"items": 1, "logs": 1, "spans": 0, "losses": 0})

    def test_raw_and_buffered_python_stdin_reject_would_block_without_closing(self):
        for buffered in (False, True):
            with self.subTest(buffered=buffered):
                read_fd, write_fd = os.pipe()
                try:
                    os.set_blocking(read_fd, False)
                    os.write(write_fd, EVENT)
                    with os.fdopen(read_fd, "rb", buffering=-1 if buffered else 0) as binary, \
                            patch("sys.stdin", SimpleNamespace(buffer=binary)), \
                            patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                            patch("sys.stderr", new_callable=io.StringIO) as stderr:
                        code = main(["convert", "-", "--dry-run"])
                        self.assert_would_block(code, stdout.getvalue().encode(), stderr.getvalue().encode())
                        self.assertFalse(binary.closed)
                        self.assertEqual(list(self.base.iterdir()), [])
                finally:
                    os.close(write_fd)

    def test_budget_rejection_precedes_would_block_and_never_creates_output(self):
        for flag in ("--max-input-bytes", "--max-decompressed-bytes"):
            with self.subTest(flag=flag):
                result = self.run_pipe(b"", flag, str(sys.maxsize), "--output-dir", str(self.output))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertIn(f"argument {flag}: byte budget".encode(), result.stderr)
                self.assertNotIn(b"would block", result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)
                self.assertEqual(list(self.base.iterdir()), [])

    def test_input_overflow_still_rejects_without_waiting_for_eof(self):
        result = self.run_pipe(EVENT, "--max-input-bytes", str(len(EVENT) - 1),
                               "--output-dir", str(self.output))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"input exceeds --max-input-bytes", result.stderr)
        self.assertNotIn(b"would block", result.stderr)
        self.assertEqual(list(self.base.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
