# SPDX-License-Identifier: Apache-2.0
"""Selected reviewed pair side: flag parity, strict validation and publication."""
import copy
import hashlib
import json
import os
import subprocess
import unittest

from prometheus_client.parser import text_string_to_metric_families

from . import test_dimension_config as pairs
from .test_jsonl import jsonl, split_requests


class ConversionDimensionTests(unittest.TestCase):
    def setUp(self):
        pairs.DimensionConfigTests.setUp(self)
        self.output = self.directory / "out.prom"
        self.report = self.directory / "manifest.json"

    save = pairs.DimensionConfigTests.save

    def run_cli(self, *options, source=pairs.CAPTURE, file=True, side="candidate", data=None):
        return subprocess.run(
            [*pairs.CLI, str(source), "--output", str(self.output), "--report", str(self.report),
             *(["--dimensions-file", str(self.config), "--dimensions-side", side] if file else []),
             *map(str, options)], input=data, capture_output=True, text=True, timeout=60, cwd=self.directory)

    def assert_invalid(self, expected):
        self.output.write_bytes(b"metrics sentinel")
        self.report.write_bytes(b"manifest sentinel")
        original = self.config.read_bytes()
        proc = self.run_cli("--force")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn(expected, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(self.output.read_bytes(), b"metrics sentinel")
        self.assertEqual(self.report.read_bytes(), b"manifest sentinel")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()),
                         ["manifest.json", "out.prom", "pair.json"])

    # Exercise the entire established malformed-pair matrix through conversion,
    # including invalid entries on the unselected baseline side.
    test_schema_fields_and_types_are_strict = pairs.DimensionConfigTests.test_schema_fields_and_types_are_strict
    test_sources_keys_and_labels_are_validated_without_lossy_coercion = pairs.DimensionConfigTests.test_sources_keys_and_labels_are_validated_without_lossy_coercion
    test_collisions_and_dimension_caps_on_both_sides = pairs.DimensionConfigTests.test_collisions_and_dimension_caps_on_both_sides
    test_json_syntax_duplicate_keys_constants_and_encoding = pairs.DimensionConfigTests.test_json_syntax_duplicate_keys_constants_and_encoding
    test_file_byte_limit_is_independent_of_capture_limit = pairs.DimensionConfigTests.test_file_byte_limit_is_independent_of_capture_limit
    test_missing_file_directory_and_invalid_config_create_no_output = pairs.DimensionConfigTests.test_missing_file_directory_and_invalid_config_create_no_output

    def flags(self, side):
        return [arg for d in pairs.PAIR[side]
                for arg in ("--dimension", f'{d["source"]}:{d["key"]}={d["label"]}')]

    def assert_metrics(self, side):
        parsed = [s for f in text_string_to_metric_families(self.output.read_text()) for s in f.samples]
        self.assertEqual(len(parsed), 16 if side == "baseline" else 40)
        calls = [s for s in parsed if s.name == "spanmetrics_calls_total"]
        self.assertEqual(sum(s.value for s in calls), 6)
        self.assertEqual(sum(s.value for s in parsed if s.name == "spanmetrics_errors_total"), 0)
        self.assertAlmostEqual(sum(s.value for s in parsed if s.name.endswith("_sum")), 0.6)
        expected = ({("s:GET",): 4, ("s:POST",): 2} if side == "baseline" else
                    {("s:GET", "s:east", "s:1"): 1, ("s:GET", "s:east", "i:1"): 1,
                     ("s:GET", "s:west", "missing"): 2, ("s:POST", "s:east", "s:a|b"): 1,
                     ("s:POST", "s:west", "s:missing"): 1})
        keys = ("method",) if side == "baseline" else ("method", "zone", "customer")
        self.assertEqual({tuple(s.labels[k] for k in keys): s.value for s in calls}, expected)
        for sample in parsed:
            self.assertEqual({k: sample.labels[k] for k in ("service_name", "span_name", "span_kind", "status_code")},
                             dict(service_name="web", span_name="request", span_kind="2", status_code="0"))
            count = expected[tuple(sample.labels[k] for k in keys)]
            if sample.name.endswith("_bucket") or sample.name.endswith("_count"):
                self.assertEqual(sample.value, count)
        return parsed

    def test_selected_sides_equal_flags_and_independent_samples(self):
        original = self.config.read_bytes()
        for side in ("baseline", "candidate"):
            with self.subTest(side=side):
                proc = self.run_cli("--force", side=side)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                metrics, manifest = self.output.read_bytes(), self.report.read_bytes()
                self.assertEqual(proc.stdout.encode(), manifest)
                report = json.loads(manifest)
                self.assertEqual(report["prometheus_sha256"], hashlib.sha256(metrics).hexdigest())
                self.assertEqual([report[k] for k in ("unique_records", "duplicate_records", "groups", "series")],
                                 [6, 1, 2, 16] if side == "baseline" else [6, 1, 5, 40])
                parsed = self.assert_metrics(side)
                flags = self.run_cli(*self.flags(side), "--force", file=False)
                self.assertEqual(flags.returncode, 0, flags.stderr)
                self.assertEqual(self.output.read_bytes(), metrics)
                self.assertEqual(self.report.read_bytes(), manifest)
                self.assertEqual(flags.stdout, proc.stdout)
                self.assertEqual(self.assert_metrics(side), parsed)
                self.assertEqual(self.config.read_bytes(), original)
        reversed_pair = copy.deepcopy(pairs.PAIR)
        reversed_pair["candidate"].reverse()
        self.save(reversed_pair)
        proc = self.run_cli("--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.output.read_bytes(), metrics)
        self.assertEqual(self.report.read_bytes(), manifest)

    def test_cli_requires_explicit_file_side_pair_and_rejects_mixing(self):
        for options, error in [(("--dimensions-file", self.config), "requires --dimensions-side"),
                               (("--dimensions-side", "baseline"), "requires --dimensions-file"),
                               (("--dimensions-file", self.config, "--dimensions-side", "unknown"), "invalid choice"),
                               (("--dimensions-file", self.config, "--dimensions-side", "candidate",
                                 "--dimension", "span:x=x"), "cannot combine")]:
            for dry_run in (False, True):
                with self.subTest(options=options, dry_run=dry_run):
                    self.output.write_text("old metrics")
                    self.report.write_text("old manifest")
                    proc = self.run_cli(*options, "--force", *(["--dry-run"] if dry_run else []), file=False)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertIn(error, proc.stderr)
                    self.assertEqual(proc.stdout, "")
                    self.assertEqual(self.output.read_text(), "old metrics")
                    self.assertEqual(self.report.read_text(), "old manifest")
        proc = subprocess.run([*pairs.CLI, "compare", str(pairs.CAPTURE), "--output", str(self.report),
                               "--dimensions-file", str(self.config), "--dimensions-side", "baseline", "--force"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("unrecognized arguments", proc.stderr)
        self.assertEqual(self.report.read_text(), "old manifest")

    def test_both_destinations_protect_pair_aliases_even_with_force(self):
        original = self.config.read_bytes()
        for destination in ("--output", "--report"):
            for kind in ("path", "hardlink", "symlink", "config_symlink", "parent_symlink"):
                with self.subTest(destination=destination, kind=kind):
                    self.output.write_text("old metrics")
                    self.report.write_text("old manifest")
                    alias = self.directory / "alias"
                    if kind == "hardlink":
                        os.link(self.config, alias)
                    elif kind in ("symlink", "config_symlink"):
                        alias.symlink_to(self.config)
                    elif kind == "parent_symlink":
                        alias.symlink_to(self.directory, target_is_directory=True)
                    target = (self.config if kind in ("path", "config_symlink") else
                              alias / "pair.json" if kind == "parent_symlink" else alias)
                    proc = self.run_cli(destination, target, "--force",
                                        *(["--dimensions-file", alias] if kind == "config_symlink" else []))
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertIn("overwrite input", proc.stderr)
                    self.assertEqual(proc.stdout, "")
                    self.assertEqual(self.config.read_bytes(), original)
                    self.assertEqual(self.output.read_text(), "old metrics")
                    self.assertEqual(self.report.read_text(), "old manifest")
                    alias.unlink(missing_ok=True)
                    self.assertFalse(list(self.directory.glob(".spanmetrics-*")))
        # The derived default report path must also be protected.
        self.report.unlink()
        default_report = self.directory / "out.prom.report.json"
        os.link(self.config, default_report)
        command = [*pairs.CLI, str(pairs.CAPTURE), "--output", str(self.output), "--force",
                   "--dimensions-file", str(self.config), "--dimensions-side", "baseline"]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(default_report.read_bytes(), original)
        self.assertEqual(self.output.read_text(), "old metrics")

    def test_jsonl_file_and_explicit_stdin_match_flags(self):
        data = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes()))).decode()
        source = self.directory / "capture.jsonl"
        source.write_text(data)
        for side in ("baseline", "candidate"):
            for capture in (source, "-"):
                with self.subTest(side=side, capture=capture):
                    options = ("--input-format", "jsonl", "--force")
                    proc = self.run_cli(*options, source=capture, side=side, data=data if capture == "-" else None)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    metrics = self.output.read_bytes()
                    parsed = self.assert_metrics(side)
                    flags = self.run_cli(*options, *self.flags(side), file=False, source=capture,
                                         data=data if capture == "-" else None)
                    self.assertEqual(flags.returncode, 0, flags.stderr)
                    self.assertEqual(proc.stdout, flags.stdout)
                    self.assertEqual(self.output.read_bytes(), metrics)
                    self.assertEqual(self.assert_metrics(side), parsed)

    def test_empty_selection_and_dry_run_preserve_files(self):
        self.save(dict(pairs.PAIR, baseline=[]))
        proc = self.run_cli(side="baseline")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["series"], 8)
        metrics, manifest = self.output.read_bytes(), self.report.read_bytes()
        proc = self.run_cli("--dry-run", side="candidate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["series"], 40)
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["metrics_emitted"])
        self.assertEqual(self.output.read_bytes(), metrics)
        self.assertEqual(self.report.read_bytes(), manifest)

    def test_capture_rejections_keep_metrics_and_match_flag_reports(self):
        data = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes()))).decode()
        for contents, options, error in [(data, ("--max-series", "16"), "series cap"),
                                         (data, ("--max-spans", "1"), "span cap"),
                                         (data, ("--max-input-bytes", "10"), "byte cap"),
                                         (data, ("--max-line-bytes", "10"), "line byte cap"),
                                         (data + "{\n", (), "line 8"),
                                         (data + "\n", (), "line 8")]:
            for existing in (False, True):
                with self.subTest(options=options, error=error, existing=existing):
                    self.output.unlink(missing_ok=True)
                    if existing:
                        self.output.write_text("old metrics")
                    proc = self.run_cli("--input-format", "jsonl", "--force", *options, source="-", data=contents)
                    self.assertEqual(proc.returncode, 1, proc.stderr)
                    report = json.loads(proc.stdout)
                    self.assertEqual(report, json.loads(self.report.read_bytes()))
                    self.assertEqual(report["status"], "rejected")
                    self.assertFalse(report["metrics_emitted"])
                    self.assertTrue(any(error in d for d in report["diagnostics"]), report["diagnostics"])
                    self.assertNotIn("prometheus_sha256", report)
                    self.assertEqual(self.output.exists(), existing)
                    if existing:
                        self.assertEqual(self.output.read_text(), "old metrics")
                    flags = self.run_cli("--input-format", "jsonl", "--force", *options,
                                         *self.flags("candidate"), file=False, source="-", data=contents)
                    self.assertEqual(flags.returncode, 1, flags.stderr)
                    self.assertEqual(flags.stdout, proc.stdout)
                    self.assertFalse(list(self.directory.glob(".spanmetrics-*")))
        proc = self.run_cli("--force", source=pairs.ROOT / "examples/spanmetrics/conflict.json")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("conflicting duplicate", json.loads(proc.stdout)["diagnostics"][0])
        self.assertEqual(self.output.read_text(), "old metrics")


if __name__ == "__main__":
    unittest.main()
