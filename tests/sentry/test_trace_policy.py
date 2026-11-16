"""Opt-in conflict rejection must precede stdout and all output publication."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
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
from .test_consistency import OTHER, ROOT, TRACE, source_for, transaction
from .test_parser import envelope
from .verify_output import verify_directory


class TracePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "input"
        self.output = self.base / "output"

    def run_cli(self, data, *options, stdin=False, strict=True, output=None):
        if not stdin:
            self.source.write_bytes(data)
        return subprocess.run(
            [sys.executable, "-m", "otelwrench", "sentry", "convert", "-" if stdin else str(self.source),
             *(["--fail-on-trace-conflicts"] if strict else []),
             "--output-dir", str(output or self.output), *options],
            input=data if stdin else None, cwd=self.base, capture_output=True, timeout=60)

    def assert_conflict(self, result, duplicates, cycles):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr.decode(),
                         f"otelwrench sentry: trace conflicts: conflicting duplicate groups={duplicates}, "
                         f"confirmed cycles={cycles}\n")
        self.assertEqual({p.name for p in self.base.iterdir()}, {"input"})

    def test_conflicting_mapped_definitions_resources_and_root_child_identity_reject(self):
        one = transaction("1")
        variants = [{**one, field: value} for field, value in
                    [("transaction", "changed"), ("release", "demo@2"),
                     ("timestamp", 3), ("event_id", "e" * 32)]]
        variants.append(transaction("1", parent="9"))
        for two in variants:
            with self.subTest(two=two):
                # A third matching copy must not conceal the conflicting definition.
                self.assert_conflict(self.run_cli(source_for(one, two, one)), 1, 0)
        root = transaction("2")
        root["spans"] = [{"trace_id": TRACE, "span_id": "1" * 16,
                          "start_timestamp": 1, "timestamp": 2, "op": "child"}]
        self.assert_conflict(self.run_cli(source_for(root, one)), 1, 0)

    def test_confirmed_cycles_reject_for_file_stdin_gzip_and_dry_run(self):
        source = source_for({"message": "valid earlier item"}, transaction("2", "1"),
                            transaction("1", "2"), transaction("1", "2"))
        # Keep file set stable for the no-output assertion even in stdin cases.
        self.source.write_bytes(source)
        for stdin in (False, True):
            for compressed in (False, True):
                for dry_run in (False, True):
                    with self.subTest(stdin=stdin, gzip=compressed, dry_run=dry_run):
                        data = gzip.compress(source, mtime=0) if compressed else source
                        options = ["--input-compression", "gzip"] if compressed else []
                        if dry_run:
                            options.append("--dry-run")
                        self.assert_conflict(self.run_cli(data, *options, stdin=stdin), 0, 1)

    def test_skip_does_not_bypass_both_conflict_classes_or_invent_ambiguous_cycles(self):
        source = source_for(transaction("1", "2"), transaction("2", "1"),
                            transaction("3", "4"), transaction("4", "3"), transaction("4", "9"))
        source += envelope(b"\xffopaque", kind="attachment")[3:]
        self.assert_conflict(self.run_cli(source, "--skip-unsupported"), 1, 1)

    def test_default_still_publishes_conflicts_and_cycles_and_prints_full_dry_run(self):
        source = source_for(transaction("1", "2"), transaction("2", "1"),
                            transaction("3"), {**transaction("3"), "release": "changed"})
        result = self.run_cli(source, strict=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(verify_directory(self.output, source), {"items": 4, "logs": 0, "spans": 4, "losses": 0})
        manifest = json.loads((self.output / "manifest.json").read_bytes())
        summary = manifest["traceConsistency"]["summary"]
        self.assertEqual(summary["conflictingDuplicateGroups"], 1)
        self.assertEqual(summary["cycles"], 1)
        result = self.run_cli(source, "--dry-run", strict=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), manifest)

    def test_allowed_diagnostics_keep_byte_identical_output_and_dry_run_manifest(self):
        one = transaction("1")
        normalized = deepcopy(one)
        normalized["contexts"]["trace"] = {"trace_id": "A" * 32, "span_id": "1" * 16}
        normalized["start_timestamp"] = "1970-01-01T00:00:01Z"
        normalized["ignored"] = "discarded fields do not define conflicts"
        source = source_for(one, normalized, transaction("1", trace=OTHER), transaction("2", "1"),
                            transaction("3", "9"), transaction("4", "3", trace=OTHER),
                            {"message": "uncorrelated"})
        default_output = self.base / "default"
        result = self.run_cli(source, strict=False, output=default_output)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(verify_directory(self.output, source), {"items": 7, "logs": 1, "spans": 6, "losses": 1})
        for name in ("logs.json", "traces.json", "manifest.json"):
            self.assertEqual((self.output / name).read_bytes(), (default_output / name).read_bytes())
        result = self.run_cli(source, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.output / "manifest.json").read_bytes())
        self.assertEqual(json.loads(result.stdout), manifest)
        summary = manifest["traceConsistency"]["summary"]
        self.assertEqual(summary["identicalDuplicateGroups"], 1)
        self.assertEqual(summary["parents"], {"not_supplied": 3, "resolved": 1, "ambiguous": 0,
                                             "missing": 1, "other_trace_only": 1})
        self.assertEqual(summary["uncorrelatedLogs"], 1)

    def test_empty_all_skipped_and_standalone_json_accept_strict_dry_run(self):
        for source, options, count in [(b"{}\n", ["--input-format", "envelope"], 0),
                                        (envelope(b"opaque", kind="attachment"), ["--skip-unsupported"], 0),
                                        (b'{"message":"standalone"}', [], 1),
                                        (json.dumps(transaction("1")).encode(), [], 1)]:
            with self.subTest(source=source):
                result = self.run_cli(source, "--dry-run", *options, stdin=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["summary"]["convertedItems"], count)
                self.assertEqual(list(self.base.iterdir()), [])

    def test_conflict_never_invokes_publication_and_preserves_existing_destination(self):
        source = (ROOT / "examples/sentry/trace-consistency.envelope").read_bytes()
        self.source.write_bytes(source)
        self.output.mkdir()
        sentinel = self.output / "keep"
        sentinel.write_bytes(b"unchanged")
        for dry_run in (False, True):
            stdout, stderr = io.StringIO(), io.StringIO()
            args = ["convert", str(self.source), "--fail-on-trace-conflicts", "--output-dir", str(self.output)]
            if dry_run:
                args.append("--dry-run")
            with patch("otelwrench.sentry.cli.write_output") as publish, redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(main(args), 2)
            publish.assert_not_called()
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("conflicting duplicate groups=1, confirmed cycles=0", stderr.getvalue())
        self.assertEqual(sentinel.read_bytes(), b"unchanged")
        self.assertEqual({p.name for p in self.output.iterdir()}, {"keep"})
        self.assertEqual({p.name for p in self.base.iterdir()}, {"input", "output"})

    def test_later_invalid_item_and_gzip_integrity_errors_remain_authoritative(self):
        source = source_for(transaction("1"), {**transaction("1"), "transaction": "changed"})
        bad = {**transaction("2"), "timestamp": 0}
        for data, options, diagnostic in [
            (source + source_for(bad)[3:], [], b"end precedes start"),
            (gzip.compress(source, mtime=0)[:-1], ["--input-compression", "gzip"], b"invalid or truncated gzip"),
        ]:
            with self.subTest(diagnostic=diagnostic):
                result = self.run_cli(data, "--skip-unsupported", *options)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertIn(diagnostic, result.stderr)
                self.assertNotIn(b"trace conflicts:", result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)
                self.assertEqual({p.name for p in self.base.iterdir()}, {"input"})


if __name__ == "__main__":
    unittest.main()
