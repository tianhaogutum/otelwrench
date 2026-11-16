"""Versioned CLI errors wrap existing diagnostics without changing conversion."""
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
from otelwrench.sentry.parser import ConversionError
from .test_consistency import ROOT, source_for, transaction
from .test_parser import envelope
from .verify_output import verify_directory


class ErrorFormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "input"
        self.output = self.base / "output"

    def invoke(self, data, flags=(), *, stdin=False, dry=False, error_format="json"):
        self.source.write_bytes(data)
        args = [sys.executable, "-m", "otelwrench", "sentry", "convert", "-" if stdin else str(self.source),
                "--output-dir", str(self.output), *flags]
        if dry:
            args.append("--dry-run")
        if error_format is not None:
            args += ["--error-format", error_format]
        return subprocess.run(args, input=data if stdin else None, capture_output=True, timeout=60)

    def assert_error(self, result, kind, message):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b"")
        # json.loads rejects prefixes, suffixes and multiple objects. The exact
        # dictionary also forbids accidental exception details or input copies.
        self.assertEqual(json.loads(result.stderr),
                         {"schemaVersion": 1, "kind": kind, "message": message})
        self.assertEqual(result.stderr.count(b"\n"), 1)
        self.assertTrue(result.stderr.endswith(b"\n"))

    def matrix(self, payload, flags=()):
        for stdin, compressed, dry in itertools.product((False, True), repeat=3):
            data = gzip.compress(payload, mtime=0) if compressed else payload
            options = [*flags, "--input-compression", "gzip" if compressed else "none"]
            yield data, options, stdin, dry

    def check_rejection(self, data, flags, stdin, dry, expected):
        # Existing destinations are also untouched on conversion rejection.
        self.output.mkdir(exist_ok=True)
        (self.output / "keep").write_bytes(b"untouched")
        default = self.invoke(data, flags, stdin=stdin, dry=dry, error_format=None)
        explicit = self.invoke(data, flags, stdin=stdin, dry=dry, error_format="text")
        self.assertEqual(default.returncode, 2)
        self.assertEqual(default.stdout, b"")
        self.assertEqual(explicit.stderr, default.stderr)
        message = default.stderr.decode().removeprefix("otelwrench sentry: ").removesuffix("\n")
        self.assertIn(expected, message)
        actual = self.invoke(data, flags, stdin=stdin, dry=dry)
        self.assert_error(actual, "conversion_error", message)
        self.assertEqual((self.output / "keep").read_bytes(), b"untouched")
        self.assertEqual(set(self.output.iterdir()), {self.output / "keep"})
        self.assertEqual(set(self.base.iterdir()), {self.source, self.output})

    def test_malformed_json_and_later_envelope_items_keep_validation_precedence(self):
        prefix = source_for({"message": "valid", "extra": {}}, transaction("1"),
                            {**transaction("1"), "release": "conflict"})
        cases = [(b'{"message":', "invalid JSON/UTF-8"),
                 (prefix + envelope(b'{"message":')[3:], "invalid JSON/UTF-8"),
                 (prefix + envelope(b'{"message":1}')[3:], "expected string"),
                 (prefix + b'{"type":"event","length":99}\n{}', "truncated payload"),
                 (prefix + envelope(b'opaque', kind="attachment")[3:], "unsupported item type")]
        flags = ["--fail-on-trace-conflicts", "--fail-on-loss", "--fail-on-loss-category", "unsupported_field"]
        for payload, expected in cases:
            for data, options, stdin, dry in self.matrix(payload, flags):
                with self.subTest(expected=expected, stdin=stdin, options=options, dry=dry):
                    self.check_rejection(data, options, stdin, dry, expected)

    def test_gzip_integrity_precedes_policies(self):
        data = gzip.compress(source_for({"message": "ok", "extra": {}}, transaction("1"),
                                       {**transaction("1"), "release": "conflict"}), mtime=0)
        corrupt = bytearray(data)
        corrupt[-8] ^= 1
        for payload in (bytes(corrupt), data[:-1], data + gzip.compress(b"", mtime=0)[:-1]):
            for stdin, dry in itertools.product((False, True), repeat=2):
                with self.subTest(stdin=stdin, dry=dry, size=len(payload)):
                    self.check_rejection(payload, ["--input-compression", "gzip", "--fail-on-trace-conflicts",
                                                  "--fail-on-loss", "--fail-on-loss-category", "unsupported_field"],
                                         stdin, dry, "invalid or truncated gzip")

    def test_trace_any_loss_and_selective_policy_messages_and_precedence(self):
        payload = source_for({"message": "ok", "extra": {}}, transaction("1"),
                             {**transaction("1"), "release": "conflict"},
                             transaction("2", "3"), transaction("3", "2"))
        selection = ["--fail-on-loss-category", "unsupported_field"]
        cases = [(selection, "conversion losses: unsupported_field=1; selected loss entries=1"),
                 (["--fail-on-loss", *selection], "conversion losses: loss entries=1"),
                 (["--fail-on-trace-conflicts", "--fail-on-loss", *selection],
                  "trace conflicts: conflicting duplicate groups=1, confirmed cycles=1")]
        for flags, expected in cases:
            for data, options, stdin, dry in self.matrix(payload, flags):
                with self.subTest(flags=flags, stdin=stdin, dry=dry, options=options):
                    self.check_rejection(data, options, stdin, dry, expected)

    def test_input_and_expansion_budgets(self):
        payload = b'{"message":"synthetic"}'
        for data, options, stdin, dry in self.matrix(payload):
            cases = [(["--max-input-bytes", str(len(data)-1)], "input exceeds")]
            if options[-1] == "gzip":
                cases.append((["--max-decompressed-bytes", str(len(payload)-1)], "gzip expansion exceeds"))
            for flags, expected in cases:
                with self.subTest(options=options, stdin=stdin, dry=dry, flags=flags):
                    self.check_rejection(data, [*options, *flags], stdin, dry, expected)

    def capture(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return subprocess.CompletedProcess(args, code, out.getvalue().encode(), err.getvalue().encode())

    def test_exception_types_not_message_prose_choose_kind_and_escape_message(self):
        message = 'synthetic "quoted"\nline café \udcff'
        args = ["convert", str(ROOT / "examples/sentry/error.envelope"), "--dry-run", "--error-format", "json"]
        for exc, kind in ((ConversionError(message), "conversion_error"),
                          (OSError(message), "io_error"), (RecursionError(message), "recursion_error")):
            with self.subTest(kind=kind), patch("otelwrench.sentry.cli.convert_bytes", side_effect=exc), \
                    patch("otelwrench.sentry.cli.write_output") as writer:
                self.assert_error(self.capture(args), kind, message)
                writer.assert_not_called()
        # Unexpected programming errors retain their existing propagation.
        with patch("otelwrench.sentry.cli.convert_bytes", side_effect=ValueError("bug")):
            with self.assertRaisesRegex(ValueError, "bug"):
                self.capture(args)

    def test_conversion_rejection_never_calls_writer_for_new_destinations(self):
        self.source.write_bytes(b'{"message":"ok","extra":{}}')
        for dry in (False, True):
            args = ["convert", str(self.source), "--output-dir", str(self.output), "--fail-on-loss",
                    "--error-format", "json", *(["--dry-run"] if dry else [])]
            with patch("otelwrench.sentry.cli.write_output") as writer:
                self.assert_error(self.capture(args), "conversion_error", "conversion losses: loss entries=1")
                writer.assert_not_called()
            self.assertEqual(set(self.base.iterdir()), {self.source})

    def test_input_io_errors_and_existing_output_diagnostics(self):
        for source in (self.base / "missing", self.base):
            args = ["convert", str(source), "--dry-run"]
            default = self.capture(args)
            self.assert_error(self.capture([*args, "--error-format", "json"]), "io_error",
                              default.stderr.decode().removeprefix("otelwrench sentry: ").removesuffix("\n"))
        self.output.mkdir()
        (self.output / "keep").write_bytes(b"untouched")
        args = ["convert", str(ROOT / "examples/sentry/error.envelope"), "--output-dir", str(self.output)]
        default = self.capture(args)
        self.assert_error(self.capture([*args, "--error-format", "json"]), "conversion_error",
                          default.stderr.decode().removeprefix("otelwrench sentry: ").removesuffix("\n"))
        self.assertEqual((self.output / "keep").read_bytes(), b"untouched")
        self.assertEqual(set(self.output.iterdir()), {self.output / "keep"})

    def test_output_io_and_recursion_failures_clean_real_staging(self):
        args = ["convert", str(ROOT / "examples/sentry/error.envelope"), "--output-dir", str(self.output),
                "--error-format", "json"]
        for target in ("otelwrench.sentry.output.os.fsync", "otelwrench.sentry.output._rename_new"):
            for exc, kind in ((OSError("synthetic output failure"), "io_error"),
                              (RecursionError("synthetic recursion"), "recursion_error")):
                with self.subTest(target=target, kind=kind), patch(target, side_effect=exc):
                    self.assert_error(self.capture(args), kind, str(exc))
                self.assertEqual(list(self.base.iterdir()), [])

    def test_stdin_read_failure_retains_existing_conversion_wrapping(self):
        stream = io.BytesIO()
        with patch.object(stream, "read", side_effect=OSError("synthetic read failure")), \
                patch("otelwrench.sentry.cli.sys.stdin", new=io.TextIOWrapper(stream)):
            self.assert_error(self.capture(["convert", "-", "--dry-run", "--error-format", "json"]),
                              "conversion_error", "cannot read binary stdin: synthetic read failure")

    def test_argparse_usage_errors_stay_text_before_input_access(self):
        cases = [[], ["--error-format", "xml", "--dry-run"], ["--error-format"],
                 ["--input-format", "xml", "--dry-run"], ["--max-input-bytes", "0", "--dry-run"],
                 ["--fail-on-loss-category", "unknown", "--dry-run"], ["--unknown", "--dry-run"]]
        for flags in cases:
            with self.subTest(flags=flags), patch("otelwrench.sentry.cli.read_binary_stdin") as reader, \
                    patch("otelwrench.sentry.cli.write_output") as writer:
                err = io.StringIO()
                with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
                    main(["convert", "-", "--error-format", "json", *flags])
                self.assertEqual(caught.exception.code, 2)
                self.assertTrue(err.getvalue().startswith("usage:"), err.getvalue())
                reader.assert_not_called()
                writer.assert_not_called()

    def test_success_stdout_and_documents_match_text_mode(self):
        for payload in ('{"message":"café"}'.encode(),
                        (ROOT / "examples/sentry/mixed.envelope").read_bytes()):
            for data, flags, stdin, dry in self.matrix(payload):
                outputs = []
                for mode in (None, "text", "json"):
                    result = self.invoke(data, flags, stdin=stdin, dry=dry, error_format=mode)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, b"")
                    documents = {}
                    if not dry:
                        verify_directory(self.output, data)
                        documents = {p.name: p.read_bytes() for p in self.output.iterdir()}
                        for p in self.output.iterdir():
                            p.unlink()
                        self.output.rmdir()
                    outputs.append((result.stdout, documents))
                self.assertEqual(outputs[0], outputs[1])
                self.assertEqual(outputs[0], outputs[2])


if __name__ == "__main__":
    unittest.main()
