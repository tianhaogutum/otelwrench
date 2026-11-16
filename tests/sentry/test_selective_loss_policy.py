"""Selective policy filters existing loss counts without changing conversion."""
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
from otelwrench.sentry.mapping import convert_bytes
from otelwrench.sentry.parser import ConversionError
from .test_consistency import source_for, transaction
from .test_loss_policy import inputs, lossy_cases, LOSSY_JSON
from .test_parser import envelope
from .verify_output import verify_documents, verify_directory

CATEGORIES = ("semantic_loss", "unsupported_field", "unsupported_item", "unused_metadata")


def mixed_source():
    semantic = transaction("1")
    semantic["contexts"]["trace"]["status"] = "synthetic_unknown"
    semantic["a/b"] = {"deep": [1, 2]}
    # SDK/header metadata is unsupported_field under the established accounting.
    return b'{"sdk":{}}\n' + source_for(semantic)[3:] + envelope(b"\xff", kind="attachment")[3:]


def diagnostic(selection, counts):
    selected = sorted(set(selection))
    return ("conversion losses: " + ", ".join(f"{name}={counts.get(name, 0)}" for name in selected)
            + f"; selected loss entries={sum(counts.get(name, 0) for name in selected)}")


class SelectiveLibraryTests(unittest.TestCase):
    def test_each_category_and_mixed_selections_use_existing_counts(self):
        cases = [(data, extra) for data, extra, _ in lossy_cases()]
        cases.append((mixed_source(), {"skip_unsupported": True}))
        for payload, extra in cases:
            for data, options, source in inputs(payload):
                expected = convert_bytes(source, **options, **extra)
                counts = expected["manifest.json"]["lossCategories"]
                for n in range(5):
                    for selection in itertools.combinations(CATEGORIES, n):
                        with self.subTest(extra=extra, kind=type(data), options=options, selection=selection):
                            kwargs = dict(options, **extra, fail_on_loss_categories=selection)
                            if any(counts.get(name, 0) for name in selection):
                                with self.assertRaises(ConversionError) as caught:
                                    convert_bytes(data, **kwargs)
                                self.assertEqual(str(caught.exception), diagnostic(selection, counts))
                            else:
                                self.assertEqual(convert_bytes(data, **kwargs), expected)
                verify_documents(expected, source)
        counts = convert_bytes(mixed_source(), skip_unsupported=True)["manifest.json"]["lossCategories"]
        self.assertEqual(counts, {"semantic_loss": 1, "unsupported_field": 2, "unsupported_item": 1})

    def test_shapes_deduplication_order_and_zero_count_diagnostics(self):
        payload = mixed_source()
        for selection in (["unsupported_field", "semantic_loss", "semantic_loss", "unused_metadata"],
                          ("unused_metadata", "semantic_loss", "unsupported_field"),
                          {"semantic_loss", "unsupported_field", "unused_metadata"},
                          frozenset({"unsupported_field", "unused_metadata", "semantic_loss"})):
            with self.assertRaises(ConversionError) as caught:
                convert_bytes(payload, skip_unsupported=True, fail_on_loss_categories=selection)
            self.assertEqual(str(caught.exception),
                             "conversion losses: semantic_loss=1, unsupported_field=2, unused_metadata=0; selected loss entries=3")
        default = convert_bytes(LOSSY_JSON)
        for selection in ((), [], set(), frozenset(), ["semantic_loss", "unused_metadata"]):
            self.assertEqual(convert_bytes(LOSSY_JSON, fail_on_loss_categories=selection), default)

    def test_invalid_policy_arguments_reject_before_input_decode(self):
        invalid = [None, "semantic_loss", b"semantic_loss", 1, True, {},
                   {"semantic_loss": True}, iter(["semantic_loss"]), [None], [True],
                   [["semantic_loss"]], [b"semantic_loss"], ["unknown"], ["Semantic_loss"], [""]]
        for selection in invalid:
            for full in (False, True):
                with self.subTest(selection=repr(selection), full=full), \
                        patch("otelwrench.sentry.mapping.decode_input") as decode:
                    with self.assertRaises(ConversionError) as caught:
                        convert_bytes(None, fail_on_loss_categories=selection, fail_on_loss=full)
                    self.assertIn("fail_on_loss_categories:", str(caught.exception))
                    decode.assert_not_called()

    def test_valid_policy_preserves_type_budget_and_full_validation_precedence(self):
        prefix = envelope(LOSSY_JSON)
        compressed = gzip.compress(prefix, mtime=0)
        corrupt = bytearray(compressed)
        corrupt[-8] ^= 1
        cases = [(None, {}, "input data must be bytes"),
                 (None, {"max_input_bytes": 0}, "--max-input-bytes: byte budget"),
                 (None, {"max_decompressed_bytes": 0}, "--max-decompressed-bytes: byte budget"),
                 (prefix, {"input_format": "wrong"}, "unsupported input format"),
                 (prefix, {"input_compression": "wrong"}, "unsupported input compression")]
        invalid_suffixes = [(envelope(b'{"message":1}')[3:], "expected string"),
                            (envelope(b'{"message":"x","x":NaN}')[3:], "nonfinite JSON number"),
                            (envelope(b'{"message":"x","x":1,"x":2}')[3:], "duplicate JSON key"),
                            (envelope(b'{"message":')[3:], "invalid JSON/UTF-8"),
                            (b'{"type":"event","length":999}\n{}', "truncated payload"),
                            (envelope(b"\xff", kind="attachment")[3:], "unsupported item type"),
                            (source_for(transaction("1", "1"))[3:], "span cannot parent itself")]
        for suffix, message in invalid_suffixes:
            for data, options, _ in inputs(prefix + suffix):
                cases.append((data, options, message))
        for data, options, _ in inputs(prefix):
            cases.append((data, dict(options, max_input_bytes=bytes.__len__(data)-1), "input exceeds"))
        for bad in (compressed[:-1], bytes(corrupt), compressed + gzip.compress(b"", mtime=0)[:-1], compressed + b"x"):
            for data, _, _ in inputs(bad):
                # Test plain byte variants of the corrupt compressed source.
                if bytes.__len__(data) != len(bad):
                    continue
                cases.append((data, {"input_compression": "gzip"}, "invalid or truncated gzip"))
        cases.append((compressed, {"input_compression": "gzip", "max_decompressed_bytes": len(prefix)-1}, "gzip expansion exceeds"))
        for data, options, message in cases:
            with self.subTest(kind=type(data), options=options, message=message):
                with self.assertRaises(ConversionError) as default:
                    convert_bytes(data, **options)
                self.assertIn(message, str(default.exception))
                with self.assertRaises(ConversionError) as selected:
                    convert_bytes(data, fail_on_loss_categories=CATEGORIES, fail_on_trace_conflicts=True, **options)
                self.assertEqual(str(selected.exception), str(default.exception))

    def test_trace_then_any_loss_then_selected_loss_precedence(self):
        payload = source_for({"message": "synthetic", "extra": {}}, transaction("1"),
                             {**transaction("1"), "release": "changed"}, transaction("2", "3"), transaction("3", "2"))
        for data, options, _ in inputs(payload):
            for selection in ((), ("unsupported_field",), ("semantic_loss",)):
                with self.assertRaisesRegex(ConversionError, "^conversion losses: loss entries=1$"):
                    convert_bytes(data, fail_on_loss=True, fail_on_loss_categories=selection, **options)
                with self.assertRaisesRegex(ConversionError,
                        "^trace conflicts: conflicting duplicate groups=1, confirmed cycles=1$"):
                    convert_bytes(data, fail_on_loss=True, fail_on_trace_conflicts=True,
                                  fail_on_loss_categories=selection, **options)
            # Diagnostics alone are not selected losses.
            default = convert_bytes(data, **options)
            self.assertEqual(convert_bytes(data, fail_on_loss_categories=("semantic_loss",), **options), default)


class SelectiveCliTests(unittest.TestCase):
    def run_cli(self, data, flags, stdin, dry_run, output):
        source = output.parent / "input"
        source.write_bytes(data)
        return subprocess.run([sys.executable, "-m", "otelwrench", "sentry", "convert", "-" if stdin else str(source),
                               "--output-dir", str(output), *(["--dry-run"] if dry_run else []), *flags],
                              input=data if stdin else None, cwd=output.parent, capture_output=True, timeout=60)

    def test_file_stdin_plain_gzip_each_category_and_repeated_mixed_reject(self):
        cases = [(LOSSY_JSON, [], ["unsupported_field"], {"unsupported_field": 2}),
                 (lossy_cases()[2][0], [], ["semantic_loss"], {"semantic_loss": 1}),
                 (lossy_cases()[3][0], ["--input-format", "envelope"], ["unused_metadata"], {"unused_metadata": 1}),
                 (envelope(b"\xff", kind="attachment"), ["--skip-unsupported"], ["unsupported_item"], {"unsupported_item": 1}),
                 (mixed_source(), ["--skip-unsupported"], ["unsupported_field", "semantic_loss", "semantic_loss", "unused_metadata"],
                  {"semantic_loss": 1, "unsupported_field": 2, "unsupported_item": 1})]
        for payload, flags, selection, counts in cases:
            for compression, stdin, dry_run in itertools.product(("none", "gzip"), (False, True), (False, True)):
                data = gzip.compress(payload, mtime=0) if compression == "gzip" else payload
                extra = [*flags, "--input-compression", compression]
                for name in selection:
                    extra += ["--fail-on-loss-category", name]
                with self.subTest(selection=selection, compression=compression, stdin=stdin, dry_run=dry_run), \
                        tempfile.TemporaryDirectory() as temp:
                    output = Path(temp) / "output"
                    result = self.run_cli(data, extra, stdin, dry_run, output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, b"")
                    self.assertEqual(result.stderr.decode(), "otelwrench sentry: " + diagnostic(selection, counts) + "\n")
                    self.assertEqual({p.name for p in output.parent.iterdir()}, {"input"})

    def test_unselected_losses_and_zero_loss_preserve_documents_and_dry_run(self):
        for payload in (LOSSY_JSON, b'{"message":"synthetic"}'):
            for compression, stdin in itertools.product(("none", "gzip"), (False, True)):
                data = gzip.compress(payload, mtime=0) if compression == "gzip" else payload
                with self.subTest(compression=compression, stdin=stdin), tempfile.TemporaryDirectory() as temp:
                    base = Path(temp)
                    flags = ["--input-compression", compression]
                    default = base / "default"
                    self.assertEqual(self.run_cli(data, flags, stdin, False, default).returncode, 0)
                    flags += ["--fail-on-loss-category", "semantic_loss"]
                    output = base / "selected"
                    result = self.run_cli(data, flags, stdin, False, output)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    verify_directory(output, data)
                    for name in ("logs.json", "traces.json", "manifest.json"):
                        self.assertEqual((output / name).read_bytes(), (default / name).read_bytes())
                    result = self.run_cli(data, flags, stdin, True, base / "unused")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), json.loads((default / "manifest.json").read_bytes()))
                    self.assertEqual({p.name for p in base.iterdir()}, {"input", "default", "selected"})

    def test_invalid_names_and_missing_values_fail_before_input_access(self):
        for source in ("-", "/missing/input"):
            for flags in (["--fail-on-loss-category", "unknown"], ["--fail-on-loss-category"],
                          ["--fail-on-loss-category", "semantic_loss,unsupported_field"],
                          ["--fail-on-loss-category", "semantic_loss", "--fail-on-loss-category", "unknown", "--fail-on-loss"]):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("otelwrench.sentry.cli.read_binary_stdin") as stdin, patch.object(Path, "open") as opened, \
                        patch("otelwrench.sentry.cli.convert_bytes") as converter, patch("otelwrench.sentry.cli.write_output") as writer, \
                        redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    main(["convert", source, "--dry-run", *flags])
                self.assertEqual(caught.exception.code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("--fail-on-loss-category", stderr.getvalue())
                for mock in (stdin, opened, converter, writer):
                    mock.assert_not_called()

    def test_rejection_never_calls_writer_or_changes_existing_destination(self):
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
                    result = main(["convert", str(source), "--output-dir", str(output),
                                   "--fail-on-loss-category", "unsupported_field", *(["--dry-run"] if dry_run else [])])
                self.assertEqual(result, 2)
                writer.assert_not_called()
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "otelwrench sentry: conversion losses: unsupported_field=2; selected loss entries=2\n")
                self.assertEqual({p.name for p in base.iterdir()}, {"input", "existing"})
                self.assertEqual({p.name for p in output.iterdir()}, {"sentinel"})
                self.assertEqual((output / "sentinel").read_bytes(), b"unchanged")

    def test_validation_trace_and_any_loss_precedence_reach_cli(self):
        conflict = source_for({"message": "synthetic", "extra": {}}, transaction("1"),
                              {**transaction("1"), "release": "changed"})
        for payload, flags, message in (
                (conflict, ["--fail-on-trace-conflicts", "--fail-on-loss"], "trace conflicts:"),
                (conflict, ["--fail-on-loss"], "conversion losses: loss entries=1"),
                (conflict + envelope(b'{"message":1}')[3:], [], "expected string"),
                (conflict + envelope(b"\xff", kind="attachment")[3:], [], "unsupported item type"),
                (gzip.compress(conflict, mtime=0)[:-1], ["--input-compression", "gzip"], "invalid or truncated gzip")):
            for stdin, dry_run in itertools.product((False, True), repeat=2):
                with tempfile.TemporaryDirectory() as temp:
                    output = Path(temp) / "output"
                    result = self.run_cli(payload, [*flags, "--fail-on-loss-category", "unsupported_field"], stdin, dry_run, output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, b"")
                    self.assertIn(message, result.stderr.decode())
                    self.assertEqual({p.name for p in output.parent.iterdir()}, {"input"})


if __name__ == "__main__":
    unittest.main()
