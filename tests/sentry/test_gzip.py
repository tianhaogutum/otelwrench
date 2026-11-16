"""Synthetic gzip transport -> real envelope/JSON -> CLI publication regressions."""
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from otelwrench.sentry.mapping import convert_bytes
from otelwrench.sentry.parser import ConversionError
from .test_parser import envelope
from .verify_output import verify_directory

ROOT = Path(__file__).resolve().parents[2]


class GzipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "input.gz"
        self.output = self.base / "output"

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "otelwrench", "sentry", "convert", str(self.source),
             "--output-dir", str(self.output), *map(str, args)],
            cwd=self.base, capture_output=True, text=True, timeout=60)

    def assert_rejected(self, data, diagnostic, *args):
        self.source.write_bytes(data)
        result = self.run_cli("--input-compression", "gzip", *args)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(diagnostic, result.stderr)
        self.assertTrue(result.stderr.startswith("otelwrench sentry:"), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual({p.name for p in self.base.iterdir()}, {self.source.name})

    def test_compressed_and_plain_fixtures_have_identical_otlp_framing_and_losses(self):
        for fixture, skip, counts in [
            ("error.envelope", False, {"items": 1, "logs": 1, "spans": 0, "losses": 0}),
            ("mixed.envelope", False, {"items": 2, "logs": 1, "spans": 2, "losses": 9}),
            ("with-attachment.envelope", True, {"items": 3, "logs": 1, "spans": 2, "losses": 11}),
        ]:
            with self.subTest(fixture=fixture):
                plain = (ROOT / "examples/sentry" / fixture).read_bytes()
                compressed = gzip.compress(plain, mtime=0)
                self.source.write_bytes(compressed)
                self.output = self.base / fixture
                args = ["--skip-unsupported"] if skip else []
                result = self.run_cli("--input-compression", "gzip", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(verify_directory(self.output, compressed), counts)
                expected = convert_bytes(plain, skip_unsupported=skip)
                for name in ("logs.json", "traces.json", "manifest.json"):
                    actual = json.loads((self.output / name).read_text())
                    if name == "manifest.json":
                        self.assertEqual(actual.pop("input"), {
                            "format": "envelope", "compression": "gzip",
                            "bytes": len(compressed),
                            "sha256": hashlib.sha256(compressed).hexdigest(),
                            "decompressedBytes": len(plain),
                            "decompressedSha256": hashlib.sha256(plain).hexdigest(),
                        })
                        expected[name].pop("input")
                    self.assertEqual(actual, expected[name])

    def test_gzip_is_explicit_not_inferred_from_magic_or_filename(self):
        plain = envelope(b'{"message":"synthetic"}')
        self.source.write_bytes(gzip.compress(plain, mtime=0))
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn("envelope header", result.stderr)
        self.assertFalse(self.output.exists())
        self.source.write_bytes(plain)  # .gz suffix alone has no meaning
        self.assertEqual(self.run_cli().returncode, 0)

    def test_independent_compressed_and_expansion_limits_at_boundary(self):
        plain = envelope(b'{"message":"' + b"x" * 100_000 + b'"}')
        compressed = gzip.compress(plain, mtime=0)
        self.assertLess(len(compressed), 1024)
        self.assert_rejected(compressed, "--max-input-bytes", "--max-input-bytes", len(compressed) - 1)
        self.assert_rejected(compressed, "--max-decompressed-bytes", "--max-decompressed-bytes", 1024)
        self.assert_rejected(compressed, "--max-decompressed-bytes", "--max-decompressed-bytes", len(plain) - 1)
        result = self.run_cli("--input-compression", "gzip", "--max-input-bytes", len(compressed),
                              "--max-decompressed-bytes", len(plain))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(verify_directory(self.output, compressed)["logs"], 1)

    def test_truncated_gzip_and_corrupt_trailers_publish_nothing(self):
        plain = envelope(b'{"message":"valid before trailer"}')
        compressed = gzip.compress(plain, mtime=0)
        cases = [b"", compressed[:1], compressed[:9], compressed[:15],
                 compressed[:-8], compressed[:-1],
                 compressed[:-8] + bytes([compressed[-8] ^ 1]) + compressed[-7:],
                 compressed[:-4] + bytes([compressed[-4] ^ 1]) + compressed[-3:],
                 compressed + b"trailing garbage",
                 compressed + gzip.compress(b"", mtime=0)[:-1],
                 b"not gzip", compressed[:10] + b"\x07" + compressed[-8:]]
        for data in cases:
            with self.subTest(data=data):
                self.assert_rejected(data, "gzip", "--max-decompressed-bytes", len(plain))

    def test_bad_item_lengths_after_valid_item_publish_nothing_even_with_skips(self):
        prefix = envelope(b'{"message":"valid first"}')
        utf8 = '{"message":"错误"}'.encode()
        tails = [b'{"type":"event","length":200}\n{}',
                 b'{"type":"event","length":true}\n{}',
                 b'{"type":"event","length":16}\n' + utf8,
                 b'{"type":"attachment","length":100}\nopaque']
        for tail in tails:
            with self.subTest(tail=tail):
                self.assert_rejected(gzip.compress(prefix + tail, mtime=0), "item 1", "--skip-unsupported")

    def test_unsupported_items_still_require_opt_in(self):
        compressed = gzip.compress((ROOT / "examples/sentry/with-attachment.envelope").read_bytes(), mtime=0)
        self.assert_rejected(compressed, "unsupported item type")

    def test_concatenated_members_frame_bytes_across_member_boundary_and_share_limit(self):
        plain = envelope('{\n"message":"错误 café"\n}'.encode())
        split = plain.index("错".encode()) + 1  # split inside a UTF-8 sequence
        compressed = gzip.compress(plain[:split], mtime=0) + gzip.compress(plain[split:], mtime=0)
        self.assert_rejected(compressed, "--max-decompressed-bytes", "--max-decompressed-bytes", len(plain) - 1)
        self.source.write_bytes(compressed)
        result = self.run_cli("--input-compression", "gzip", "--max-decompressed-bytes", len(plain))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(verify_directory(self.output, compressed)["logs"], 1)

    def test_gzip_dry_run_and_explicit_formats(self):
        for plain, input_format, count in [(b'{}', "envelope", 0),
                                            (b'{"message":"synthetic"}', "json", 1)]:
            with self.subTest(input_format=input_format):
                self.source.write_bytes(gzip.compress(plain, mtime=0))
                result = self.run_cli("--input-compression", "gzip", "--input-format", input_format, "--dry-run")
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads(result.stdout)
                self.assertEqual(manifest["input"]["format"], input_format)
                self.assertEqual(manifest["summary"]["inputItems"], count)
                self.assertEqual({p.name for p in self.base.iterdir()}, {self.source.name})

    def test_library_conversion_uses_same_compression_and_limits(self):
        plain = envelope(b'{"message":"synthetic"}')
        compressed = gzip.compress(plain, mtime=0)
        result = convert_bytes(compressed, input_compression="gzip",
                               max_input_bytes=len(compressed), max_decompressed_bytes=len(plain))
        self.assertEqual(result["logs.json"], convert_bytes(plain)["logs.json"])
        for options, diagnostic in [({"max_input_bytes": len(compressed) - 1}, "--max-input-bytes"),
                                    ({"max_decompressed_bytes": len(plain) - 1}, "--max-decompressed-bytes")]:
            with self.subTest(options=options), self.assertRaisesRegex(ConversionError, diagnostic):
                convert_bytes(compressed, input_compression="gzip", **options)

    def test_compression_arguments_are_validated(self):
        self.source.write_bytes(gzip.compress(b'{}', mtime=0))
        for args in [("--input-compression", "zip"),
                     ("--max-decompressed-bytes", "0"),
                     ("--max-decompressed-bytes", "-1")]:
            with self.subTest(args=args):
                self.assertEqual(self.run_cli(*args).returncode, 2)
        for options in ({"input_compression": "zip"}, {"max_input_bytes": 0},
                        {"max_decompressed_bytes": -1}, {"max_decompressed_bytes": True}):
            with self.subTest(options=options), self.assertRaises(ConversionError):
                convert_bytes(b'{"message":"synthetic"}', **options)


if __name__ == "__main__":
    unittest.main()
