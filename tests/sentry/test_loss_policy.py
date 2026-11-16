"""Loss rejection counts existing manifest entries only, after all validation."""
from contextlib import redirect_stderr, redirect_stdout
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
from otelwrench.sentry.mapping import convert_bytes
from otelwrench.sentry.parser import ConversionError
from .test_consistency import ROOT, source_for, transaction
from .test_direct_bytes import SUBCLASSES
from .test_parser import envelope
from .verify_output import verify_documents, verify_directory

LOSSY_JSON = b'{"message":"synthetic","a/b":{"nested":[1,2]},"contexts":{"device":{"name":"invented"}}}'


def lossy_cases():
    # Nested values are one loss per unsupported subtree, not one per leaf.
    header = b'{"sdk":{"name":"synthetic"}}\n'
    item = json.dumps({"type": "event", "length": len(LOSSY_JSON), "content_type": "application/json"}).encode()
    skipped = (b'{"event_id":"' + b'e' * 32 + b'","sdk":{}}\n'
               b'{"type":"attachment","length":2,"filename":"invented"}\n\xff\x00\n')
    semantic = transaction("1")
    semantic["contexts"]["trace"]["status"] = "invented_unknown_status"
    return [
        (LOSSY_JSON, {"input_format": "json"}, 2),
        (header + item + b"\n" + LOSSY_JSON + b"\n", {}, 4),
        (json.dumps(semantic).encode(), {}, 1),
        (b'{"event_id":"' + b'e' * 32 + b'"}\n', {"input_format": "envelope"}, 1),
        (skipped, {"skip_unsupported": True}, 4),
        (envelope(b"\xffopaque", kind="attachment"), {"skip_unsupported": True}, 1),
        ((ROOT / "examples/sentry/mixed.envelope").read_bytes(), {}, 9),
        ((ROOT / "examples/sentry/with-attachment.envelope").read_bytes(), {"skip_unsupported": True}, 11),
    ]


def inputs(payload):
    for compression in ("none", "gzip"):
        source = gzip.compress(payload, mtime=0) if compression == "gzip" else payload
        options = dict(input_compression=compression, max_input_bytes=len(source),
                       max_decompressed_bytes=len(payload))
        for kind in (bytes, *SUBCLASSES):
            yield kind(source), options, source


class LibraryLossPolicyTests(unittest.TestCase):
    def test_total_enumerated_entries_reject_and_default_false_preserves_documents(self):
        for payload, extra, count in lossy_cases():
            for data, options, source in inputs(payload):
                with self.subTest(count=count, extra=extra, kind=type(data), options=options):
                    default = convert_bytes(source, **options, **extra)
                    manifest = default["manifest.json"]
                    self.assertEqual(manifest["summary"]["lossEntries"], count)
                    self.assertEqual(sum(len(item["losses"]) for item in manifest["items"])
                                     + len(manifest["envelopeLosses"]), count)
                    self.assertEqual(convert_bytes(data, fail_on_loss=False, **options, **extra), default)
                    with self.assertRaises(ConversionError) as caught:
                        convert_bytes(data, fail_on_loss=True, **options, **extra)
                    self.assertEqual(str(caught.exception), f"conversion losses: loss entries={count}")
        # Pointer escaping and categories retain their existing meaning.
        manifest = convert_bytes(LOSSY_JSON)["manifest.json"]
        self.assertEqual([loss["path"] for loss in manifest["items"][0]["losses"]],
                         ["/a~1b", "/contexts/device"])
        self.assertEqual(manifest["lossCategories"], {"unsupported_field": 2})
        self.assertEqual(convert_bytes(lossy_cases()[4][0], skip_unsupported=True)["manifest.json"]["lossCategories"],
                         {"unsupported_field": 2, "unsupported_item": 1, "unused_metadata": 1})
        self.assertEqual(convert_bytes(lossy_cases()[2][0])["manifest.json"]["lossCategories"], {"semantic_loss": 1})

    def test_zero_loss_notes_and_trace_diagnostics_remain_accepted(self):
        conflict = source_for(transaction("1"), {**transaction("1"), "release": "changed"},
                              transaction("2", "3"), transaction("3", "2"))
        for payload, extra in ((b'{"message":"synthetic"}', {}),
                               (b'{"exception":{"values":[{"type":"Synthetic","stacktrace":{"frames":[]}}]}}', {}),
                               (json.dumps(transaction("1", "9")).encode(), {"input_format": "json"}),
                               (envelope(b'{"message":"synthetic"}'), {}),
                               (b"{}\n", {"input_format": "envelope"}), (conflict, {})):
            for data, options, source in inputs(payload):
                with self.subTest(extra=extra, kind=type(data), options=options):
                    expected = convert_bytes(source, **options, **extra)
                    self.assertEqual(expected["manifest.json"]["summary"]["lossEntries"], 0)
                    actual = convert_bytes(data, fail_on_loss=True, **options, **extra)
                    self.assertEqual(actual, expected)
                    verify_documents(actual, source)
                    if payload != conflict:
                        self.assertEqual(convert_bytes(data, fail_on_loss=True, fail_on_trace_conflicts=True,
                                                       **options, **extra), expected)

    def test_complete_validation_errors_precede_loss_policy(self):
        prefix = envelope(LOSSY_JSON)
        invalid = [(prefix + suffix, extra, diagnostic) for suffix, extra, diagnostic in (
            (envelope(b'{"message":1}')[3:], {}, "expected string"),
            (envelope(b'{"message":"ok","unknown":NaN}')[3:], {}, "nonfinite JSON number"),
            (envelope(b'{"message":"ok","unknown":{"x":1,"x":2}}')[3:], {}, "duplicate JSON key"),
            (envelope(b'{"message":')[3:], {}, "invalid JSON/UTF-8"),
            (b'{"type":"event","length":999}\n{}', {"skip_unsupported": True}, "truncated payload"),
            (envelope(b"\xffopaque", kind="attachment")[3:], {}, "unsupported item type"),
            (source_for(transaction("1", "1"))[3:], {}, "span cannot parent itself"))]
        for payload, extra, diagnostic in invalid:
            for data, options, source in inputs(payload):
                with self.subTest(diagnostic=diagnostic, kind=type(data), options=options):
                    with self.assertRaises(ConversionError) as default:
                        convert_bytes(source, **options, **extra)
                    self.assertIn(diagnostic, str(default.exception))
                    with self.assertRaises(ConversionError) as strict:
                        convert_bytes(data, fail_on_loss=True, fail_on_trace_conflicts=True, **options, **extra)
                    self.assertEqual(str(strict.exception), str(default.exception))

    def test_type_budgets_and_gzip_integrity_remain_authoritative(self):
        source = gzip.compress(LOSSY_JSON, mtime=0)
        corrupt = bytearray(source)
        corrupt[-8] ^= 1
        cases = [(None, {}, "input data must be bytes"),
                 (bytearray(LOSSY_JSON), {}, "input data must be bytes"),
                 (None, {"max_input_bytes": 0}, "--max-input-bytes: byte budget"),
                 (None, {"max_decompressed_bytes": 0}, "--max-decompressed-bytes: byte budget"),
                 (LOSSY_JSON, {"input_format": "invalid"}, "unsupported input format"),
                 (LOSSY_JSON, {"input_compression": "invalid"}, "unsupported input compression")]
        for kind in (bytes, *SUBCLASSES):
            cases.append((kind(LOSSY_JSON), {"max_input_bytes": len(LOSSY_JSON)-1}, "input exceeds"))
            for bad in (source[:-1], bytes(corrupt), source + gzip.compress(b"", mtime=0)[:-1], source + b"x"):
                cases.append((kind(bad), {"input_compression": "gzip"}, "invalid or truncated gzip"))
            cases.append((kind(source), {"input_compression": "gzip", "max_decompressed_bytes": len(LOSSY_JSON)-1},
                          "gzip expansion exceeds"))
        for data, options, diagnostic in cases:
            with self.subTest(kind=type(data), options=options, diagnostic=diagnostic):
                with self.assertRaises(ConversionError) as default:
                    convert_bytes(data, **options)
                self.assertIn(diagnostic, str(default.exception))
                with self.assertRaises(ConversionError) as strict:
                    convert_bytes(data, fail_on_loss=True, fail_on_trace_conflicts=True, **options)
                self.assertEqual(str(strict.exception), str(default.exception))

    def test_trace_rejection_precedes_losses_when_both_policies_selected(self):
        payload = source_for({"message": "synthetic", "extra": {}}, transaction("1"),
                             {**transaction("1"), "release": "changed"}, transaction("2", "3"), transaction("3", "2"))
        payload += envelope(b"\xffopaque", kind="attachment")[3:]
        for data, options, source in inputs(payload):
            with self.subTest(kind=type(data), options=options):
                with self.assertRaisesRegex(ConversionError, "^conversion losses: loss entries=2$"):
                    convert_bytes(data, fail_on_loss=True, skip_unsupported=True, **options)
                with self.assertRaisesRegex(ConversionError,
                        "^trace conflicts: conflicting duplicate groups=1, confirmed cycles=1$"):
                    convert_bytes(data, fail_on_loss=True, fail_on_trace_conflicts=True, skip_unsupported=True, **options)


class CliLossPolicyTests(unittest.TestCase):
    def run_cli(self, data, extra, stdin, dry_run, output, strict=True):
        source = output.parent / "input"
        source.write_bytes(data)
        return subprocess.run([sys.executable, "-m", "otelwrench", "sentry", "convert", "-" if stdin else str(source),
                               "--output-dir", str(output), *(["--fail-on-loss"] if strict else []),
                               *(["--dry-run"] if dry_run else []), *extra],
                              input=data if stdin else None, cwd=output.parent, capture_output=True, timeout=60)

    def test_file_stdin_plain_gzip_rejection_never_publishes_or_prints_manifest(self):
        for payload, options, count in lossy_cases():
            flags = ["--skip-unsupported"] if options.get("skip_unsupported") else []
            if "input_format" in options:
                flags += ["--input-format", options["input_format"]]
            for compression in ("none", "gzip"):
                data = gzip.compress(payload, mtime=0) if compression == "gzip" else payload
                for stdin in (False, True):
                    for dry_run in (False, True):
                        with self.subTest(count=count, compression=compression, stdin=stdin, dry_run=dry_run), \
                                tempfile.TemporaryDirectory() as temp:
                            base = Path(temp)
                            output = base / "output"
                            result = self.run_cli(data, [*flags, "--input-compression", compression], stdin, dry_run, output)
                            self.assertEqual(result.returncode, 2, result.stderr)
                            self.assertEqual(result.stdout, b"")
                            self.assertEqual(result.stderr.decode(), f"otelwrench sentry: conversion losses: loss entries={count}\n")
                            self.assertEqual({p.name for p in base.iterdir()}, {"input"})

    def test_zero_loss_and_default_outputs_and_dry_run_remain_identical(self):
        for payload, flags in ((b'{"message":"synthetic"}', []),
                               (source_for(transaction("1"), transaction("1"), transaction("2", "9")), []),
                               (b"{}\n", ["--input-format", "envelope"])):
            for compression in ("none", "gzip"):
                data = gzip.compress(payload, mtime=0) if compression == "gzip" else payload
                for stdin in (False, True):
                    with self.subTest(compression=compression, stdin=stdin), tempfile.TemporaryDirectory() as temp:
                        base = Path(temp)
                        extra = [*flags, "--input-compression", compression]
                        default = base / "default"
                        result = self.run_cli(data, extra, stdin, False, default, strict=False)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        output = base / "strict"
                        result = self.run_cli(data, extra, stdin, False, output)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(verify_directory(output, data)["losses"], 0)
                        for name in ("logs.json", "traces.json", "manifest.json"):
                            self.assertEqual((default / name).read_bytes(), (output / name).read_bytes())
                        result = self.run_cli(data, extra, stdin, True, base / "unused")
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(json.loads(result.stdout), json.loads((default / "manifest.json").read_bytes()))
                        self.assertEqual({p.name for p in base.iterdir()}, {"input", "default", "strict"})

    def test_rejection_does_not_call_writer_or_touch_existing_destination(self):
        for dry_run in (False, True):
            with tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                source = base / "input"
                source.write_bytes(LOSSY_JSON)
                output = base / "existing"
                output.mkdir()
                (output / "sentinel").write_bytes(b"unchanged")
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("otelwrench.sentry.cli.write_output") as writer, redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(["convert", str(source), "--fail-on-loss", "--output-dir", str(output),
                                 *(["--dry-run"] if dry_run else [])])
                self.assertEqual(code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "otelwrench sentry: conversion losses: loss entries=2\n")
                writer.assert_not_called()
                self.assertEqual({p.name for p in base.iterdir()}, {"input", "existing"})
                self.assertEqual({p.name for p in output.iterdir()}, {"sentinel"})
                self.assertEqual((output / "sentinel").read_bytes(), b"unchanged")

    def test_validation_and_trace_precedence_reaches_cli(self):
        conflict = source_for({"message": "synthetic", "extra": {}}, transaction("1"),
                              {**transaction("1"), "release": "changed"})
        for payload, extra, diagnostic in (
                (conflict, ["--fail-on-trace-conflicts"], "trace conflicts: conflicting duplicate groups=1, confirmed cycles=0"),
                (conflict + envelope(b'{"message":1}')[3:], ["--fail-on-trace-conflicts"], "expected string"),
                (conflict + envelope(b"\xff", kind="attachment")[3:], [], "unsupported item type"),
                (gzip.compress(conflict, mtime=0)[:-1], ["--input-compression", "gzip"], "invalid or truncated gzip")):
            for stdin in (False, True):
                for dry_run in (False, True):
                    with self.subTest(diagnostic=diagnostic, stdin=stdin, dry_run=dry_run), \
                            tempfile.TemporaryDirectory() as temp:
                        output = Path(temp) / "output"
                        result = self.run_cli(payload, extra, stdin, dry_run, output)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertEqual(result.stdout, b"")
                        self.assertIn(diagnostic, result.stderr.decode())
                        self.assertEqual({p.name for p in output.parent.iterdir()}, {"input"})


if __name__ == "__main__":
    unittest.main()
