import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from .test_parser import envelope
from .verify_output import verify_directory

ROOT = Path(__file__).resolve().parents[2]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output = self.base / "output"

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "otelwrench", "sentry", *map(str, args)],
                              cwd=self.base, capture_output=True, text=True, timeout=60)

    def test_real_error_fixture_end_to_end_independently_verified(self):
        source = ROOT / "examples/sentry/error.envelope"
        result = self.run_cli("convert", source, "--output-dir", self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["convertedItems"], 1)
        self.assertEqual(verify_directory(self.output, source.read_bytes()), {"items": 1, "logs": 1, "spans": 0, "losses": 0})
        actual = json.loads((self.output / "logs.json").read_text())
        self.assertEqual(actual, json.loads((ROOT / "tests/sentry/golden/error.logs.json").read_text()))

    def test_real_mixed_utf8_fixture_end_to_end_independently_verified(self):
        source = ROOT / "examples/sentry/mixed.envelope"
        result = self.run_cli("convert", source, "--output-dir", self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(verify_directory(self.output, source.read_bytes()), {"items": 2, "logs": 1, "spans": 2, "losses": 9})
        transaction = json.loads((self.output / "traces.json").read_text())
        self.assertEqual(transaction, json.loads((ROOT / "tests/sentry/golden/transaction.traces.json").read_text()))

    def test_unsupported_default_fails_then_opt_in_accounts_for_every_item(self):
        source = ROOT / "examples/sentry/with-attachment.envelope"
        result = self.run_cli("convert", source, "--output-dir", self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsupported item type", result.stderr)
        self.assertFalse(self.output.exists())
        result = self.run_cli("convert", source, "--skip-unsupported", "--output-dir", self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        verified = verify_directory(self.output, source.read_bytes())
        self.assertEqual(verified, {"items": 3, "logs": 1, "spans": 2, "losses": 11})

    def test_malformed_second_item_leaves_no_partial_output(self):
        source = self.base / "broken.envelope"
        source.write_bytes(envelope(b'{"message":"valid first"}') + b'{"type":"transaction"}\n{"type":"transaction"}\n')
        result = self.run_cli("convert", source, "--output-dir", self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("required for transactions", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual({p.name for p in self.base.iterdir()}, {source.name})

    def test_existing_directory_is_not_modified(self):
        self.output.mkdir()
        sentinel = self.output / "keep"
        sentinel.write_text("untouched")
        result = self.run_cli("convert", ROOT / "examples/sentry/error.envelope", "--output-dir", self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(sentinel.read_text(), "untouched")
        self.assertEqual({p.name for p in self.output.iterdir()}, {"keep"})

    def test_dry_run_creates_nothing_and_does_not_execute_input(self):
        source = self.base / "event.json"
        marker = self.base / "should-not-exist"
        source.write_text(json.dumps({"message": "m", "script": f"touch {marker}"}))
        before = set(self.base.iterdir())
        result = self.run_cli("convert", source, "--dry-run", "--output-dir", self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["unsupportedFieldCategories"], {"script": 1})
        self.assertEqual(set(self.base.iterdir()), before)
        self.assertFalse(marker.exists())

    def test_bad_input_failure_messages_without_traceback(self):
        cases = [(b'{"message":', "JSON"), (b'{"message":"m","timestamp":NaN}', "nonfinite"),
                 (b'{"message":"m","timestamp":1e-999999999}', "sub-nanosecond"),
                 (b'{"message":"m","timestamp":1e999999999999999999999999999}', "exponent"),
                 (b'{"message":"m","type":[]}', "unsupported event type"),
                 (b'{}\n{"type":"event","length":20}\n{}', "truncated"),
                 (b'\x1f\x8b\x00', "envelope header")]
        for number, (data, message) in enumerate(cases):
            source = self.base / f"bad-{number}"
            source.write_bytes(data)
            result = self.run_cli("convert", source, "--output-dir", self.output)
            with self.subTest(data=data):
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.output.exists())

    def test_input_size_limit_and_missing_files(self):
        result = self.run_cli("convert", ROOT / "examples/sentry/error.envelope", "--max-input-bytes", 10, "--output-dir", self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("exceeds", result.stderr)
        result = self.run_cli("convert", self.base / "missing", "--output-dir", self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("No such file", result.stderr)
        self.assertFalse(self.output.exists())

    def test_cli_argument_validation_and_entry_point(self):
        for args in [[], ["convert"], ["convert", ROOT / "examples/sentry/error.envelope"],
                     ["convert", ROOT / "examples/sentry/error.envelope", "--dry-run", "--max-input-bytes", "0"],
                     ["convert", ROOT / "examples/sentry/error.envelope", "--dry-run", "--input-format", "xml"]]:
            with self.subTest(args=args):
                self.assertEqual(self.run_cli(*args).returncode, 2)
        result = subprocess.run([sys.executable, "-m", "otelwrench", "sentry", "--version"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "otelwrench sentry 0.1.0")

    def test_deterministic_files_in_different_output_directories(self):
        source = ROOT / "examples/sentry/mixed.envelope"
        other = self.base / "other"
        for output in (self.output, other):
            result = self.run_cli("convert", source, "--output-dir", output)
            self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("logs.json", "traces.json", "manifest.json"):
            self.assertEqual((self.output / name).read_bytes(), (other / name).read_bytes())

    def test_concurrent_publication_has_one_winner(self):
        args = [sys.executable, "-m", "otelwrench", "sentry", "convert", str(ROOT / "examples/sentry/mixed.envelope"), "--output-dir", str(self.output)]
        children = [subprocess.Popen(args, cwd=self.base, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        results = [child.communicate(timeout=60) for child in children]
        self.assertEqual(sorted(child.returncode for child in children), [0, 2], results)
        verify_directory(self.output, (ROOT / "examples/sentry/mixed.envelope").read_bytes())
        self.assertEqual({p.name for p in self.base.iterdir()}, {"output"})


if __name__ == "__main__":
    unittest.main()
