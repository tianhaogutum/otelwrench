"""Binary stdin contracts: file parity, bounded reads and all-or-nothing output."""
import gzip
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from otelwrench.sentry.cli import main
from .test_parser import envelope
from .verify_output import verify_directory

ROOT = Path(__file__).resolve().parents[2]


class StdinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output = self.base / "output"

    def run_cli(self, data, *args, source="-"):
        return subprocess.run(
            [sys.executable, "-m", "otelwrench", "sentry", "convert", str(source),
             "--output-dir", str(self.output), *map(str, args)],
            input=data, cwd=self.base, capture_output=True, timeout=60)

    def assert_rejected(self, data, diagnostic, *args):
        before = set(self.base.iterdir())
        result = self.run_cli(data, *args)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr.startswith(b"otelwrench sentry:"), result.stderr)
        self.assertIn(diagnostic.encode(), result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertEqual(set(self.base.iterdir()), before)
        self.assertFalse(self.output.exists())

    def test_plain_and_gzip_stdin_match_files_at_exact_budgets(self):
        plain = (ROOT / "examples/sentry/mixed.envelope").read_bytes()
        for compression, data in [("none", plain), ("gzip", gzip.compress(plain, mtime=0))]:
            with self.subTest(compression=compression):
                source = self.base / f"{compression}.input"
                source.write_bytes(data)
                options = ["--input-compression", compression, "--max-input-bytes", len(data),
                           "--max-decompressed-bytes", len(plain)]
                self.output = self.base / f"{compression}-file"
                file_result = self.run_cli(b"ignored", *options, source=source)
                self.assertEqual(file_result.returncode, 0, file_result.stderr)
                file_output = self.output
                self.output = self.base / f"{compression}-stdin"
                result = self.run_cli(data, *options)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b"")
                self.assertEqual(json.loads(result.stdout)["convertedItems"], 2)
                self.assertEqual(verify_directory(self.output, data),
                                 {"items": 2, "logs": 1, "spans": 2, "losses": 9})
                for name in ("logs.json", "traces.json", "manifest.json"):
                    self.assertEqual((file_output / name).read_bytes(), (self.output / name).read_bytes())

    def test_dry_run_accepts_json_and_header_only_envelope(self):
        for plain, input_format, count in [(b'{}', "envelope", 0),
                                           ('{"message":"错误 café"}'.encode(), "json", 1)]:
            for compression in ("none", "gzip"):
                with self.subTest(input_format=input_format, compression=compression):
                    data = gzip.compress(plain, mtime=0) if compression == "gzip" else plain
                    result = self.run_cli(data, "--dry-run", "--input-format", input_format,
                                          "--input-compression", compression)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    manifest = json.loads(result.stdout)
                    self.assertEqual(manifest["input"]["format"], input_format)
                    self.assertEqual(manifest["summary"]["convertedItems"], count)
                    self.assertEqual(list(self.base.iterdir()), [])

    def test_stdin_input_and_expansion_overflow_publish_nothing(self):
        plain = envelope(b'{"message":"' + b"x" * 100_000 + b'"}')
        compressed = gzip.compress(plain, mtime=0)
        self.assert_rejected(plain, "--max-input-bytes", "--max-input-bytes", len(plain) - 1)
        self.assert_rejected(compressed, "--max-input-bytes", "--input-compression", "gzip",
                             "--max-input-bytes", len(compressed) - 1)
        self.assert_rejected(compressed, "--max-decompressed-bytes", "--input-compression", "gzip",
                             "--max-decompressed-bytes", len(plain) - 1)

    def test_input_overflow_does_not_wait_for_pipe_eof(self):
        # Keep the producer open: a bounded read must fail at limit+1 bytes.
        child = subprocess.Popen(
            [sys.executable, "-m", "otelwrench", "sentry", "convert", "-", "--max-input-bytes", "16",
             "--output-dir", str(self.output)], cwd=self.base,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            child.stdin.write(b"x" * 17)
            child.stdin.flush()
            self.assertEqual(child.wait(timeout=60), 2)
            self.assertIn(b"input exceeds --max-input-bytes=16", child.stderr.read())
            self.assertEqual(child.stdout.read(), b"")
            self.assertEqual(list(self.base.iterdir()), [])
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=60)

    def test_corrupt_gzip_and_later_invalid_items_publish_nothing(self):
        plain = envelope(b'{"message":"valid first"}')
        compressed = gzip.compress(plain, mtime=0)
        for data in (b"", compressed[:-1],
                     compressed[:-8] + bytes([compressed[-8] ^ 1]) + compressed[-7:],
                     compressed + b"garbage"):
            with self.subTest(data=data):
                self.assert_rejected(data, "gzip", "--input-compression", "gzip")
        malformed = plain + b'{"type":"event","length":200}\n{}'
        for compression in ("none", "gzip"):
            with self.subTest(compression=compression):
                data = gzip.compress(malformed, mtime=0) if compression == "gzip" else malformed
                self.assert_rejected(data, "item 1", "--input-compression", compression, "--skip-unsupported")
        self.assert_rejected(b"", "empty or padded envelope header")
        self.assert_rejected(compressed, "envelope header")  # gzip stays explicit

    def test_concatenated_gzip_members_share_expansion_budget(self):
        plain = envelope('{\n"message":"错误 café"\n}'.encode())
        split = plain.index("错".encode()) + 1
        data = gzip.compress(plain[:split], mtime=0) + gzip.compress(plain[split:], mtime=0)
        self.assert_rejected(data, "--max-decompressed-bytes", "--input-compression", "gzip",
                             "--max-decompressed-bytes", len(plain) - 1)
        result = self.run_cli(data, "--input-compression", "gzip", "--max-decompressed-bytes", len(plain))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(verify_directory(self.output, data)["logs"], 1)

    def test_binary_unsupported_item_requires_explicit_skip(self):
        data = envelope('{"message":"错误"}'.encode()) + b'{"type":"attachment","length":4}\n\xff\x00\n\x80\n'
        compressed = gzip.compress(data, mtime=0)
        self.assert_rejected(compressed, "unsupported item type", "--input-compression", "gzip")
        result = self.run_cli(compressed, "--input-compression", "gzip", "--skip-unsupported")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(verify_directory(self.output, compressed),
                         {"items": 2, "logs": 1, "spans": 0, "losses": 1})

    def test_dash_selects_stdin_but_dot_slash_dash_is_a_file(self):
        (self.base / "-").write_bytes(b'{"message":"from file"}')
        result = self.run_cli(b'{"message":"from stdin"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        verify_directory(self.output, b'{"message":"from stdin"}')
        self.output = self.base / "literal"
        result = self.run_cli(b"invalid stdin ignored", source="./-")
        self.assertEqual(result.returncode, 0, result.stderr)
        verify_directory(self.output, b'{"message":"from file"}')

    def test_existing_output_is_preserved(self):
        self.output.mkdir()
        (self.output / "keep").write_bytes(b"untouched")
        result = self.run_cli((ROOT / "examples/sentry/mixed.envelope.gz").read_bytes(),
                              "--input-compression", "gzip")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(b"already exists", result.stderr)
        self.assertEqual({p.name for p in self.base.iterdir()}, {"output"})
        self.assertEqual({p.name for p in self.output.iterdir()}, {"keep"})
        self.assertEqual((self.output / "keep").read_bytes(), b"untouched")

    def test_stdin_is_not_closed_and_read_errors_are_diagnostics(self):
        for fail in (False, True):
            with self.subTest(fail=fail), io.BytesIO(b'{"message":"binary stdin"}') as binary:
                stdin = unittest.mock.Mock(buffer=binary)
                if fail:
                    stdin.buffer = unittest.mock.Mock()
                    stdin.buffer.read.side_effect = OSError("synthetic read failure")
                with patch("sys.stdin", stdin), patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                        patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    code = main(["convert", "-", "--dry-run"])
                self.assertEqual(code, 2 if fail else 0, stderr.getvalue())
                if fail:
                    self.assertIn("synthetic read failure", stderr.getvalue())
                    self.assertEqual(stdout.getvalue(), "")
                    stdin.buffer.close.assert_not_called()
                else:
                    self.assertEqual(json.loads(stdout.getvalue())["summary"]["logRecords"], 1)
                    self.assertFalse(binary.closed)


if __name__ == "__main__":
    unittest.main()
