# SPDX-License-Identifier: Apache-2.0
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from prometheus_client.parser import text_string_to_metric_families
from .helpers import CLI, ROOT, document, gauge


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.output = self.directory / "out.prom"
        self.mapping = Path(str(self.output) + ".mapping.json")

    def run_cli(self, *args):
        # Run the installed entry point from another cwd, without PYTHONPATH.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        return subprocess.run([*CLI, *map(str, args)], cwd=self.directory, env=env,
                              encoding="utf-8", capture_output=True, timeout=60)

    def command(self, fixture="metrics.json", *options):
        return self.run_cli("convert", ROOT / "examples/prom-names" / fixture,
                            "--output", self.output, *options)

    def test_both_strategies_against_independent_goldens_and_parser(self):
        for strategy, golden in [("UnderscoreEscapingWithSuffixes", "with-suffixes.prom"),
                                 ("UnderscoreEscapingWithoutSuffixes", "without-suffixes.prom")]:
            with self.subTest(strategy=strategy):
                result = self.command("metrics.json", "--strategy", strategy)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                emitted = self.output.read_text()
                self.assertEqual(emitted, (ROOT / "examples/prom-names" / golden).read_text())
                report = json.loads(self.mapping.read_text())
                self.assertEqual(report["status"], "ok")
                self.assertEqual(report["strategy"], strategy)
                self.assertEqual(report["sample_count"], 5)
                parsed = list(text_string_to_metric_families(emitted))
                actual = {(f.type, s.name): (s.value, s.labels) for f in parsed for s in f.samples}
                duration = "request_duration_milliseconds" if strategy.endswith("WithSuffixes") else "request_duration"
                memory = "memory_used_bytes" if strategy.endswith("WithSuffixes") else "memory_used"
                ratio = "queue_load_ratio" if strategy.endswith("WithSuffixes") else "queue_load"
                self.assertEqual(actual, {
                    ("gauge", "active_sessions"): (3, {}),
                    ("gauge", memory): (4096, {}),
                    ("gauge", ratio): (0.25, {}),
                    ("gauge", duration): (12.5, {"route": "/cart"}),
                    ("counter", "requests_total"): (7, {"route": "/cart"})})
                # Repeat for byte-for-byte deterministic mapping as well as text.
                before = self.mapping.read_bytes()
                self.assertEqual(self.command("metrics.json", "--strategy", strategy).returncode, 0)
                self.assertEqual(self.mapping.read_bytes(), before)

    def test_custom_mapping_and_resource_promotion(self):
        custom = self.directory / "mapping.json"
        result = self.command("metrics.json", "--mapping", custom, "--promote-resource", "service.name")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(custom.is_file())
        self.assertFalse(self.mapping.exists())
        for family in text_string_to_metric_families(self.output.read_text()):
            for sample in family.samples:
                self.assertEqual(sample.labels["service_name"], "synthetic-shop")

    def test_unit_conversion_golden_and_unscaled_default(self):
        default = self.command("milliseconds.json")
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertIn('latency_milliseconds{kind="integer"} 1250\n', self.output.read_text())
        self.assertIn("elapsed_milliseconds_total 9223372036854775807\n", self.output.read_text())
        result = self.command("milliseconds.json", "--convert-unit", "ms:s")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), (ROOT / "examples/prom-names/seconds.prom").read_text())
        before = (self.output.read_bytes(), self.mapping.read_bytes())
        report = json.loads(before[1])
        self.assertEqual(report["sample_count"], 6)
        self.assertEqual(report["collisions"], [])
        self.assertEqual(report["metrics"][0]["data_points"][0]["original_value"], "1250")
        dry_run = self.command("milliseconds.json", "--convert-unit", "ms:s", "--dry-run")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertEqual(json.loads(dry_run.stdout), report)
        self.assertEqual((self.output.read_bytes(), self.mapping.read_bytes()), before)

    def test_unit_conversion_failures_preserve_existing_artifacts(self):
        self.output.write_text("previous exposition")
        self.mapping.write_text("previous mapping")
        input_path = self.directory / "input.json"
        cases = [
            (document(gauge("latency", "ms"), gauge("latency_seconds", "s")), "collision", "metric_name"),
            (document(gauge(unit="us")), "error", "unsupported unit"),
            (document(gauge(unit="ms", value=str(2**63))), "error", "integer outside"),
            (document({"name": "latency", "unit": "ms", "gauge": {
                "dataPoints": [{"asDouble": 10**309}]}}), "error", "overflows"),
            (document({"name": "latency", "unit": "ms", "gauge": {
                "dataPoints": [{"asDouble": 5e-324}]}}), "error", "underflows"),
        ]
        for source, status, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                input_path.write_text(json.dumps(source))
                result = self.run_cli("convert", input_path, "--output", self.output, "--convert-unit", "ms:s")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(json.loads(result.stderr)["status"], status)
                self.assertIn(diagnostic, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.output.read_text(), "previous exposition")
                self.assertEqual(self.mapping.read_text(), "previous mapping")

    def test_unit_conversion_rejects_unsupported_pair(self):
        result = self.command("milliseconds.json", "--convert-unit", "By:s")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("invalid choice", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.mapping.exists())

    def test_dry_run_leaves_all_files_unchanged(self):
        self.output.write_text("previous exposition")
        self.mapping.write_text("previous mapping")
        result = self.command("metrics.json", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["sample_count"], 5)
        self.assertEqual(self.output.read_text(), "previous exposition")
        self.assertEqual(self.mapping.read_text(), "previous mapping")
        self.output.unlink()
        self.mapping.unlink()
        self.assertEqual(self.command("metrics.json", "--dry-run").returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.mapping.exists())

    def test_collisions_fail_with_complete_report_no_artifacts(self):
        result = self.command("collisions.json")
        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stderr)
        self.assertEqual(report["status"], "collision")
        self.assertTrue({"label_name", "metric_name"} <= {c["kind"] for c in report["collisions"]})
        self.assertFalse(self.output.exists())
        self.assertFalse(self.mapping.exists())

    def test_failure_preserves_previous_artifacts(self):
        self.output.write_text("previous exposition")
        self.mapping.write_text("previous mapping")
        for fixture in ["collisions.json", "missing.json"]:
            with self.subTest(fixture=fixture):
                result = self.command(fixture)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(json.loads(result.stderr)["status"] in ("error", "collision"))
                self.assertEqual(self.output.read_text(), "previous exposition")
                self.assertEqual(self.mapping.read_text(), "previous mapping")

    def test_bad_json_and_unsupported_type(self):
        input_path = self.directory / "input.json"
        for raw in ['{', '{"resourceMetrics": [], "typo": []}',
                    json.dumps(document({"name": "latency", "histogram": {}}))]:
            with self.subTest(raw=raw):
                input_path.write_text(raw)
                result = self.run_cli("convert", input_path, "--output", self.output)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stderr)["status"], "error")
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.output.exists())

    def test_nan_policy(self):
        input_path = self.directory / "nan.json"
        metric = gauge()
        metric["gauge"]["dataPoints"] = [{"asDouble": "NaN"}]
        input_path.write_text(json.dumps(document(metric)))
        rejected = self.run_cli("convert", input_path, "--output", self.output)
        self.assertEqual(rejected.returncode, 2)
        allowed = self.run_cli("convert", input_path, "--output", self.output, "--allow-nonfinite")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertIn("temperature NaN\n", self.output.read_text())

    def test_path_aliases_rejected(self):
        local_input = self.directory / "input.json"
        local_input.write_text((ROOT / "examples/prom-names" / "metrics.json").read_text())
        before = local_input.read_bytes()
        for args in [("--output", local_input),
                     ("--output", self.output, "--mapping", local_input),
                     ("--output", self.output, "--mapping", self.output)]:
            with self.subTest(args=args):
                result = self.run_cli("convert", local_input, *args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("different paths", json.loads(result.stderr)["errors"][0])
                self.assertEqual(local_input.read_bytes(), before)
        self.output.symlink_to(local_input)
        self.assertEqual(self.run_cli("convert", local_input, "--output", self.output).returncode, 2)
        self.output.unlink()
        self.output.hardlink_to(local_input)
        result = self.run_cli("convert", local_input, "--output", self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("hard-link", result.stderr)
        self.assertEqual(local_input.read_bytes(), before)

    def test_invalid_destinations_preflight_both_files(self):
        for args in [("--output", self.directory),
                     ("--output", self.output, "--mapping", self.directory / "missing" / "mapping.json")]:
            with self.subTest(args=args):
                result = self.run_cli("convert", ROOT / "examples/prom-names" / "metrics.json", *args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stderr)["status"], "error")
                self.assertFalse(self.output.exists())
                self.assertFalse(self.mapping.exists())

    def test_argparse_rejects_unsupported_strategy(self):
        result = self.command("metrics.json", "--strategy", "NoTranslation")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)
        self.assertFalse(self.output.exists())
