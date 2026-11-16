"""Selected stdout documents validate the entire source before any emission."""
from contextlib import redirect_stderr, redirect_stdout
import gzip
import io
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from otelwrench.sentry.cli import main
from .test_consistency import ROOT, source_for, transaction
from .test_parser import envelope
from .verify_output import verify_directory

SELECTIONS = ("logs", "traces", "manifest")


class StdoutDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "input"
        self.output = self.base / "output"

    def invoke(self, data, flags, *, stdin=False):
        self.source.write_bytes(data)
        return subprocess.run([sys.executable, "-m", "otelwrench", "sentry", "convert",
                               "-" if stdin else str(self.source), *map(str, flags)],
                              input=data if stdin else None, cwd=self.base,
                              capture_output=True, timeout=60)

    def capture(self, data, flags, *, stdin=False):
        self.source.write_bytes(data)
        stream = io.TextIOWrapper(io.BytesIO(data))
        self.addCleanup(stream.close)
        out, err = io.StringIO(), io.StringIO()
        with patch("otelwrench.sentry.cli.sys.stdin", stream), redirect_stdout(out), redirect_stderr(err), \
                patch("otelwrench.sentry.cli.write_output") as writer, \
                patch("otelwrench.sentry.output.tempfile.mkdtemp") as staging:
            code = main(["convert", "-" if stdin else str(self.source), *flags])
            writer.assert_not_called()
            staging.assert_not_called()
            self.assertFalse(stream.closed)
        self.assertEqual(set(self.base.iterdir()), {self.source})
        return code, out.getvalue(), err.getvalue()

    def assert_rejection(self, result, message, error_format="text", kind="conversion_error"):
        code, out, err = result
        self.assertEqual(code, 2, err)
        self.assertEqual(out, "")
        if error_format == "json":
            document = json.loads(err)
            self.assertEqual(set(document), {"schemaVersion", "kind", "message"})
            self.assertEqual(document["schemaVersion"], 1)
            self.assertEqual(document["kind"], kind)
            self.assertIn(message, document["message"])
            self.assertEqual(err.count("\n"), 1)
        else:
            self.assertTrue(err.startswith("otelwrench sentry: "), err)
            self.assertIn(message, err)
        self.assertNotIn("Traceback", err)

    def test_selected_document_matches_directory_bytes(self):
        cases = [(' {"message":"café\\nline"} '.encode(), "json"),
                 ((ROOT / "examples/sentry/mixed.envelope").read_bytes(), "envelope")]
        for (payload, fmt), compressed, stdin, explicit in itertools.product(
                cases, (False, True), (False, True), (False, True)):
            data = gzip.compress(payload, mtime=0) if compressed else payload
            flags = ["--input-compression", "gzip" if compressed else "none",
                     "--input-format", fmt if explicit else "auto",
                     "--max-input-bytes", str(len(data)), "--max-decompressed-bytes", str(len(payload))]
            with self.subTest(fmt=fmt, compressed=compressed, stdin=stdin, explicit=explicit):
                result = self.invoke(data, [*flags, "--output-dir", self.output], stdin=stdin)
                self.assertEqual(result.returncode, 0, result.stderr)
                verify_directory(self.output, data)
                expected = {name: (self.output / f"{name}.json").read_bytes() for name in SELECTIONS}
                for path in self.output.iterdir():
                    path.unlink()
                self.output.rmdir()
                for name in SELECTIONS:
                    result = self.invoke(data, [*flags, "--stdout-document", name, "--error-format", "json"], stdin=stdin)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, b"")
                    self.assertEqual(result.stdout, expected[name])
                    self.assertTrue(result.stdout.endswith(b"\n"))
                    json.loads(result.stdout)  # Exactly one document, no summary/prefix/suffix.
                    self.assertEqual(set(self.base.iterdir()), {self.source})

    def test_empty_and_unused_signals_and_passing_policies_never_use_writer(self):
        cases = [(b'{"message":"synthetic"}', [], "traces", {"resourceSpans": []}),
                 (json.dumps(transaction("1")).encode(), [], "logs", {"resourceLogs": []}),
                 (b"{}\n", ["--input-format", "envelope"], "logs", {"resourceLogs": []}),
                 (b"{}\n", ["--input-format", "envelope"], "traces", {"resourceSpans": []}),
                 (envelope(b"opaque", kind="attachment"), ["--skip-unsupported"], "logs", {"resourceLogs": []})]
        for payload, extra, selection, expected in cases:
            # A skipped unsupported item is a loss, but has no semantic_loss.
            policies = ["--fail-on-trace-conflicts", "--fail-on-loss-category", "semantic_loss"]
            if not extra or "--skip-unsupported" not in extra:
                policies += ["--fail-on-loss"]
            for compressed, stdin in itertools.product((False, True), repeat=2):
                data = gzip.compress(payload, mtime=0) if compressed else payload
                flags = [*extra, *policies, "--input-compression", "gzip" if compressed else "none"]
                code, out, err = self.capture(data, [*flags, "--stdout-document", selection], stdin=stdin)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(json.loads(out), expected)
                code, manifest, err = self.capture(data, [*flags, "--stdout-document", "manifest"], stdin=stdin)
                self.assertEqual((code, err), (0, ""))
                code, dry, err = self.capture(data, [*flags, "--dry-run"], stdin=stdin)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(manifest, dry)

    def test_late_invalid_items_precede_policy_and_selected_signal(self):
        prefix = source_for({"message": "valid", "extra": {}}, transaction("1"),
                            {**transaction("1"), "release": "conflict"})
        cases = [(prefix + envelope(b'{"message":')[3:], "invalid JSON/UTF-8"),
                 (prefix + envelope(b'{"message":1}')[3:], "expected string"),
                 (prefix + envelope(b'{"type":"transaction"}', kind="transaction")[3:], "required for transactions"),
                 (prefix + b'{"type":"event","length":99}\n{}', "truncated payload"),
                 (prefix + envelope(b"opaque", kind="attachment")[3:], "unsupported item type")]
        policies = ["--fail-on-trace-conflicts", "--fail-on-loss", "--fail-on-loss-category", "unsupported_field"]
        for (payload, message), compressed, stdin, name, error in itertools.product(
                cases, (False, True), (False, True), SELECTIONS, ("text", "json")):
            data = gzip.compress(payload, mtime=0) if compressed else payload
            flags = [*policies, "--input-compression", "gzip" if compressed else "none",
                     "--stdout-document", name, "--error-format", error]
            with self.subTest(message=message, compressed=compressed, stdin=stdin, name=name, error=error):
                self.assert_rejection(self.capture(data, flags, stdin=stdin), message, error)

    def test_corrupt_gzip_members_and_trailers_reject_before_any_document(self):
        valid = gzip.compress(envelope(b'{"message":"valid"}'), mtime=0)
        corrupt = valid[:-8] + bytes([valid[-8] ^ 1]) + valid[-7:]
        for data, stdin, name, error in itertools.product(
                (valid[:-1], corrupt, valid + corrupt, valid + b"garbage"),
                (False, True), SELECTIONS, ("text", "json")):
            flags = ["--input-compression", "gzip", "--skip-unsupported", "--stdout-document", name,
                     "--error-format", error, "--fail-on-loss"]
            self.assert_rejection(self.capture(data, flags, stdin=stdin), "gzip", error)

    def test_strict_policies_reject_even_when_selected_signal_is_empty(self):
        conflict = source_for(transaction("1"), {**transaction("1"), "release": "conflict", "extra": {}})
        cycle = source_for(transaction("2", "3"), transaction("3", "2"))
        cases = [(conflict, ["--fail-on-trace-conflicts", "--fail-on-loss", "--fail-on-loss-category", "unsupported_field"], "trace conflicts:"),
                 (cycle, ["--fail-on-trace-conflicts"], "confirmed cycles=1"),
                 (conflict, ["--fail-on-loss", "--fail-on-loss-category", "unsupported_field"], "conversion losses: loss entries=1"),
                 (conflict, ["--fail-on-loss-category", "unsupported_field"], "unsupported_field=1; selected loss entries=1"),
                 (envelope(b"opaque", kind="attachment"), ["--skip-unsupported", "--fail-on-loss-category", "unsupported_item"], "unsupported_item=1")]
        for (payload, policies, message), compressed, stdin, name, error in itertools.product(
                cases, (False, True), (False, True), SELECTIONS, ("text", "json")):
            data = gzip.compress(payload, mtime=0) if compressed else payload
            flags = [*policies, "--input-compression", "gzip" if compressed else "none",
                     "--stdout-document", name, "--error-format", error]
            self.assert_rejection(self.capture(data, flags, stdin=stdin), message, error)

    def test_byte_budgets_reject_before_stdout(self):
        payload = b'{"message":"valid"}'
        compressed = gzip.compress(payload, mtime=0)
        cases = [(payload, ["--max-input-bytes", str(len(payload) - 1)], "input exceeds"),
                 (compressed, ["--input-compression", "gzip", "--max-input-bytes", str(len(compressed) - 1)], "input exceeds"),
                 (compressed, ["--input-compression", "gzip", "--max-decompressed-bytes", str(len(payload) - 1)], "gzip expansion exceeds")]
        for (data, flags, message), stdin, name in itertools.product(cases, (False, True), SELECTIONS):
            self.assert_rejection(self.capture(data, [*flags, "--stdout-document", name, "--error-format", "json"], stdin=stdin), message, "json")

    def test_argument_errors_are_text_and_precede_input_access(self):
        cases = [["--stdout-document", "metrics"], ["--stdout-document"], [],
                 ["--stdout-document", "logs", "--max-input-bytes", "0"],
                 ["--stdout-document", "logs", "--fail-on-loss-category", "unknown"]]
        for name in SELECTIONS:
            for other in (["--dry-run"], ["--output-dir", str(self.output)],
                          ["--dry-run", "--output-dir", str(self.output)]):
                cases += [["--stdout-document", name, *other], [*other, "--stdout-document", name]]
        for flags, source in itertools.product(cases, ("-", str(self.base / "missing"))):
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(flags=flags, source=source), redirect_stdout(out), redirect_stderr(err), \
                    patch("otelwrench.sentry.cli.read_binary_stdin") as reader, patch.object(Path, "open") as opener, \
                    patch("otelwrench.sentry.cli.write_output") as writer, self.assertRaises(SystemExit) as caught:
                main(["convert", source, "--error-format", "json", *flags])
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(out.getvalue(), "")
            self.assertTrue(err.getvalue().startswith("usage:"), err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())
            reader.assert_not_called()
            opener.assert_not_called()
            writer.assert_not_called()
        self.assertEqual(list(self.base.iterdir()), [])

    def test_selected_document_is_serialized_completely_before_printing(self):
        for name in SELECTIONS:
            with patch("otelwrench.sentry.cli.json.dumps", side_effect=RecursionError("synthetic serialization failure")):
                self.assert_rejection(self.capture(b'{"message":"valid"}', ["--stdout-document", name]),
                                      "synthetic serialization failure")


if __name__ == "__main__":
    unittest.main()
