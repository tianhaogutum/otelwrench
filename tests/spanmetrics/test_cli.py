# SPDX-License-Identifier: Apache-2.0
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from prometheus_client.parser import text_string_to_metric_families

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "otelwrench", "spanmetrics"]
FIXTURE = ROOT / "examples/spanmetrics/traces.json"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.output = self.directory / "out.prom"
        self.report = Path(str(self.output) + ".report.json")

    def run_cli(self, *options, source=FIXTURE):
        return subprocess.run([*CLI, str(source), "--output", str(self.output), *map(str, options)],
                              capture_output=True, text=True, timeout=60, cwd=self.directory)

    def invalid_capture(self):
        doc = json.loads(FIXTURE.read_text())
        spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
        spans.append(dict(spans[0], name="conflicting duplicate"))
        source = self.directory / "invalid.json"
        source.write_text(json.dumps(doc))
        return source

    def test_real_cli_e2e_saved_files_independent_prometheus_parser(self):
        proc = self.run_cli("--buckets", "0.1,0.5,1", "--dimension", "resource:deployment.environment.name=environment",
                            "--dimension", "span:http.request.method=method")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        report = json.loads(self.report.read_text())
        self.assertEqual(json.loads(proc.stdout), report)
        text = self.output.read_text()
        self.assertTrue(report["metrics_emitted"])
        self.assertEqual(report["prometheus_sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        parsed = [s for f in text_string_to_metric_families(text) for s in f.samples]
        self.assertEqual(len(parsed), 16)
        self.assertEqual(sum(s.value for s in parsed if s.name == "spanmetrics_calls_total"), 2)
        self.assertEqual(sum(s.value for s in parsed if s.name == "spanmetrics_errors_total"), 1)
        self.assertAlmostEqual(sum(s.value for s in parsed if s.name.endswith("_sum")), 0.4)
        for le, count in (("0.1", 1), ("0.5", 2), ("1", 2), ("+Inf", 2)):
            self.assertEqual(sum(s.value for s in parsed if s.name.endswith("_bucket") and s.labels["le"] == le), count)
        for sample in parsed:
            expected = {"service_name": "web", "span_name": "GET /shop", "span_kind": "2",
                        "status_code": sample.labels["status_code"], "environment": "s:synthetic", "method": "s:GET"}
            if sample.name.endswith("_bucket"):
                expected["le"] = sample.labels["le"]
            self.assertEqual(sample.labels, expected)
        self.assertEqual(report["unique_records"], 2)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(report["series"], len(parsed))
        self.assertEqual(report["omitted_attribute_keys"], {"resource": [], "scope": [], "span": []})

    def test_rejected_capture_writes_report_and_no_metrics(self):
        proc = self.run_cli(source=self.invalid_capture())
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertFalse(self.output.exists())
        report = json.loads(self.report.read_text())
        self.assertEqual(report["status"], "rejected")
        self.assertEqual((report["unique_records"], report["rejected_records"]), (2, 1))
        self.assertFalse(report["metrics_emitted"])
        self.assertIn("conflicting duplicate", report["diagnostics"][0])
        self.assertNotIn("prometheus_sha256", report)

    def test_distributed_duplicate_and_conflict_examples(self):
        proc = self.run_cli("--dry-run", source=ROOT / "examples/spanmetrics/duplicates.json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual([report[k] for k in ("input_records", "unique_records", "duplicate_records", "rejected_records")], [3, 2, 1, 0])
        proc = self.run_cli("--dry-run", source=ROOT / "examples/spanmetrics/conflict.json")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual([report[k] for k in ("input_records", "unique_records", "duplicate_records", "rejected_records")], [3, 2, 0, 1])

    def test_malformed_json_failure_report(self):
        source = self.directory / "broken.json"
        source.write_bytes(b'{"resourceSpans":')
        proc = self.run_cli(source=source)
        self.assertEqual(proc.returncode, 1)
        report = json.loads(self.report.read_text())
        self.assertFalse(report["record_counts_complete"])
        self.assertFalse(self.output.exists())
        self.assertIn("invalid OTLP JSON", report["diagnostics"][0])

    def test_dry_run_writes_nothing_and_validates(self):
        proc = self.run_cli("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["unique_records"], 2)
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["metrics_emitted"])
        self.assertEqual(list(self.directory.iterdir()), [])
        proc = self.run_cli("--dry-run", source=self.invalid_capture())
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(self.report.exists())
        self.assertFalse(self.output.exists())

    def test_dry_run_preserves_existing_files_and_does_not_evaluate_strings(self):
        self.output.write_text("sentinel")
        self.report.write_text("report sentinel")
        marker = self.directory / "executed"
        source = self.directory / "commands.json"
        doc = json.loads(FIXTURE.read_text())
        doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] = f"$(touch {marker})"
        source.write_text(json.dumps(doc))
        proc = self.run_cli("--dry-run", source=source)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.output.read_text(), "sentinel")
        self.assertEqual(self.report.read_text(), "report sentinel")

    def test_no_clobber_default_and_explicit_force(self):
        self.output.write_text("sentinel")
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("destination exists", proc.stderr)
        self.assertEqual(self.output.read_text(), "sentinel")
        self.assertFalse(self.report.exists())
        proc = self.run_cli("--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("spanmetrics_calls_total", self.output.read_text())
        self.assertTrue(json.loads(self.report.read_text())["metrics_emitted"])

    def test_failure_with_force_preserves_old_metrics_and_replaces_report(self):
        self.assertEqual(self.run_cli().returncode, 0)
        original = self.output.read_bytes()
        proc = self.run_cli("--force", source=self.invalid_capture())
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.output.read_bytes(), original)
        self.assertFalse(json.loads(self.report.read_text())["metrics_emitted"])

    def test_never_overwrite_input_even_with_force_and_aliases(self):
        source = self.directory / "source.json"
        source.write_bytes(FIXTURE.read_bytes())
        for alias in ("exact", "hardlink", "symlink"):
            with self.subTest(alias=alias):
                self.output.unlink(missing_ok=True)
                if alias == "hardlink":
                    os.link(source, self.output)
                elif alias == "symlink":
                    self.output.symlink_to(source)
                if alias == "exact":
                    # Use the actual input as output, rather than a missing file.
                    proc = subprocess.run([*CLI, str(source), "--output", str(source), "--force"],
                                          capture_output=True, text=True, timeout=60)
                else:
                    proc = self.run_cli("--force", source=source)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("overwrite input", proc.stderr)
                self.assertEqual(source.read_bytes(), FIXTURE.read_bytes())
                self.assertFalse(self.report.exists())

    def test_output_report_collision_and_symlink_targets_rejected(self):
        proc = self.run_cli("--report", self.output)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("distinct", proc.stderr)
        target = self.directory / "sentinel"
        target.write_text("untouched")
        self.output.symlink_to(target)
        proc = self.run_cli("--force")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("symlink", proc.stderr)
        self.assertEqual(target.read_text(), "untouched")

    def test_invalid_options_fail_without_writes(self):
        for options in (("--buckets", "1,0.1"), ("--buckets", "0e999999999999999999999"),
                        ("--max-series", "0"), ("--max-spans", "-2"), ("--max-input-bytes", "invalid"),
                        ("--dimension", "span:x=le"), ("--dimension", "resource:x=span_name"),
                        ("--dimension", "scope:x=scope"), ("--unknown",)):
            with self.subTest(options=options):
                proc = self.run_cli(*options)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertFalse(self.report.exists())
                self.assertFalse(self.output.exists())

    def test_missing_source_or_output_directory_fail_without_writes(self):
        for source in (self.directory / "missing.json", self.directory):
            proc = self.run_cli(source=source)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("regular file", proc.stderr)
        proc = self.run_cli("--report", self.directory / "missing" / "report.json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("directory does not exist", proc.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_series_limit_is_enforced_by_cli(self):
        proc = self.run_cli("--max-series", "8")
        self.assertEqual(proc.returncode, 1)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["rejected_records"], 1)
        self.assertIn("series cap", report["diagnostics"][0])
        self.assertFalse(self.output.exists())

    def test_byte_limit_failure_does_not_hash_truncated_input(self):
        proc = self.run_cli("--max-input-bytes", "10")
        self.assertEqual(proc.returncode, 1)
        report = json.loads(self.report.read_text())
        self.assertFalse(report["record_counts_complete"])
        self.assertNotIn("input_sha256", report)
        self.assertIn("not fully read", report["diagnostics"][0])
        self.assertFalse(self.output.exists())

    def test_custom_report_path_and_module_entrypoint(self):
        report = self.directory / "manifest.json"
        proc = self.run_cli("--report", report)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(report.is_file())
        self.assertFalse(self.report.exists())
        proc = subprocess.run([sys.executable, "-m", "otelwrench", "spanmetrics", "--help"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--dry-run", proc.stdout)


class ComparisonCliTests(unittest.TestCase):
    setUp = CliTests.setUp
    invalid_capture = CliTests.invalid_capture

    def run_compare(self, *options, source=ROOT / 'examples/spanmetrics/dimensions.json'):
        return subprocess.run([*CLI, 'compare', str(source), '--output', str(self.report),
                               '--baseline-dimension', 'span:method=method',
                               '--candidate-dimension', 'span:method=method',
                               '--candidate-dimension', 'resource:zone=zone',
                               '--candidate-dimension', 'span:customer=customer', *map(str, options)],
                              capture_output=True, text=True, timeout=60, cwd=self.directory)

    def test_compare_saved_json_and_stdout_exact_counts(self):
        proc = self.run_compare()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result, json.loads(self.report.read_text()))
        self.assertTrue(result['comparison_complete'])
        self.assertEqual(result['configurations']['baseline']['series'], 16)
        self.assertEqual(result['configurations']['candidate']['series'], 40)
        self.assertEqual(result['series_delta'], 24)
        self.assertFalse(result['metrics_emitted'])
        self.assertFalse(self.output.exists())

    def test_compare_failure_report_has_no_complete_counts(self):
        for options, source in [((), self.invalid_capture()), (('--max-series', '16'), ROOT / 'examples/spanmetrics/dimensions.json'),
                                (('--max-input-bytes', '10'), ROOT / 'examples/spanmetrics/dimensions.json')]:
            self.report.unlink(missing_ok=True)
            proc = self.run_compare(*options, source=source)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            report = json.loads(self.report.read_text())
            self.assertFalse(report['comparison_complete'])
            self.assertIsNone(report['series_delta'])
            self.assertEqual(report['partitions'], [])
            self.assertIsNone(report['configurations']['baseline']['series'])
            self.assertIsNone(report['configurations']['candidate']['series'])
            if options == ('--max-input-bytes', '10'):
                self.assertNotIn('input_sha256', report)
            self.assertFalse(self.output.exists())

    def test_compare_dry_run_and_overwrite_protection(self):
        self.report.write_text('sentinel')
        proc = self.run_compare('--dry-run')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)['comparison_complete'])
        self.assertEqual(self.report.read_text(), 'sentinel')
        proc = self.run_compare()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.report.read_text(), 'sentinel')
        proc = self.run_compare('--force')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(self.report.read_text())['comparison_complete'])

    def test_compare_refuses_aliases_symlinks_bad_dimensions_and_missing_paths(self):
        source = self.directory / 'capture.json'
        source.write_bytes(FIXTURE.read_bytes())
        os.link(source, self.report)
        proc = self.run_compare('--force', source=source)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('overwrite input', proc.stderr)
        self.assertEqual(source.read_bytes(), FIXTURE.read_bytes())
        self.report.unlink()
        self.report.symlink_to(source)
        proc = self.run_compare('--force', source=source)
        self.assertEqual(proc.returncode, 2)
        self.report.unlink()
        for options in [('--candidate-dimension', 'span:x=le'), ('--baseline-dimension', 'span:x=method'),
                        ('--output', self.directory / 'absent' / 'result.json')]:
            proc = self.run_compare(*options)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertFalse(self.report.exists())

    def test_compare_explicit_empty_dimensions(self):
        proc = subprocess.run([*CLI, 'compare', str(ROOT / 'examples/spanmetrics/dimensions.json'),
                               '--output', str(self.report)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result['configurations']['baseline']['groups'], 1)
        self.assertEqual(result['configurations']['candidate']['groups'], 1)
        self.assertEqual(result['series_delta'], 0)


if __name__ == "__main__":
    unittest.main()
