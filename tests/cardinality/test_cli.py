# SPDX-License-Identifier: Apache-2.0
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "otelwrench", "cardinality"]


class CLITests(unittest.TestCase):
    def run_cli(self, *args, stdin=None):
        return subprocess.run([*CLI, *map(str, args)], input=stdin, capture_output=True, cwd=ROOT, timeout=60)

    def test_real_cli_e2e_independent_fixture_count_and_no_writes(self):
        fixture = ROOT / "examples/cardinality/metrics.prom"
        original = fixture.read_bytes()
        result = self.run_cli(fixture, "--drop-labels", "debug_id", "--format", "json", "--emit-config")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        # Independent hand-written reference: no production parser or key helper.
        expected = {
            ("requests_total", (("pod", "a"), ("route", "/"))),
            ("requests_total", (("pod", "b"), ("route", "/"))),
            ("requests_total", (("pod", "a"), ("route", "/health"))),
            ("temperature_celsius", (("room", "north"),)),
            ("temperature_celsius", (("room", "south"),)),
        }
        self.assertEqual(output["summary"]["observed_series"], len(expected))
        self.assertEqual(output["plans"][0]["projected_keys_after"], len(expected))
        self.assertEqual(output["plans"][0]["collision_group_count"], 0)
        self.assertTrue(output["dry_run"])
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).digest(), hashlib.sha256(original).digest())
        self.assertFalse(result.stderr)

    def test_unsafe_cli_e2e_checks_every_output_witness(self):
        result = self.run_cli(ROOT / "examples/cardinality/collisions.prom", "--drop-labels", "pod", "--emit-config", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stderr)
        output = json.loads(result.stdout)
        plan = output["plans"][0]
        self.assertEqual(plan["projected_keys_after"], 2)
        evidence = {(c["projected_key"]["labels"]["route"], w["original_labels"]["pod"], w["value"], w["timestamp_ms"])
                    for c in plan["collisions"] for w in c["originals"]}
        self.assertEqual(evidence, {("/", "a", "2", "1700000000000"), ("/", "b", "4", "1700000000001"), ("/health", "c", "3", None), ("/health", "d", "3.0", None)})
        self.assertNotIn("metric_relabel_configs_yaml", plan)
        self.assertIn("config_refusal", plan)
        self.assertFalse(result.stderr)

    def test_stdin_and_module_entrypoint(self):
        result = self.run_cli("-", "--drop-labels", "p", "--format", "json", stdin=b'x{p="a"} 1\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["plans"][0]["projected_keys_after"], 1)
        module = subprocess.run([sys.executable, "-m", "otelwrench", "cardinality", "--version"], capture_output=True, timeout=60)
        self.assertEqual(module.returncode, 0)
        self.assertEqual(module.stdout.strip(), b"otelwrench cardinality 0.1.0")

    def test_plans_are_compared_not_chained(self):
        result = self.run_cli(ROOT / "examples/cardinality/metrics.prom", "--drop-labels", "debug_id", "--drop-labels", "pod,debug_id", "--format", "json")
        self.assertEqual(result.returncode, 1)
        plans = json.loads(result.stdout)["plans"]
        self.assertEqual([p["observed_keys_before"] for p in plans], [5, 5])
        self.assertEqual([p["projected_keys_after"] for p in plans], [5, 4])

    def test_text_format_is_complete(self):
        result = self.run_cli(ROOT / "examples/cardinality/collisions.prom", "--drop-labels", "pod", "--emit-config")
        self.assertEqual(result.returncode, 1)
        for text in (b"4 -> 2", b"different", b"equal", b'"pod": "a"', b'"pod": "d"', b"no metric_relabel_configs emitted"):
            self.assertIn(text, result.stdout)
        safe = self.run_cli(ROOT / "examples/cardinality/metrics.prom", "--drop-labels", "debug_id", "--emit-config")
        self.assertEqual(safe.returncode, 0)
        self.assertIn(b"metric_relabel_configs:", safe.stdout)

    def test_plain_summary_and_offline_runtime(self):
        env = os.environ.copy()
        env.update(HTTP_PROXY="http://127.0.0.1:1", HTTPS_PROXY="http://127.0.0.1:1")
        result = subprocess.run([*CLI, str(ROOT / "examples/cardinality/metrics.prom"), "--format", "json"], capture_output=True, timeout=60, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["plans"], [])

    def test_invalid_data_never_emits_partial_output(self):
        for data in (b'x{p="a"} 1\nx{p="a"} 2\n', b'x{p="a"} 1\ninvalid\n', b'x{p="\xff"} 1\n', b"x 1\r\n", b"x 1\n# EOF\n"):
            with self.subTest(data=data):
                result = self.run_cli("-", "--format", "json", "--drop-labels", "p", "--emit-config", stdin=data)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(result.stdout)
                self.assertIn("error", json.loads(result.stderr))

    def test_bad_labels_missing_scope_and_incompatible_flags(self):
        for args in (("--drop-labels", "le"), ("--drop-labels", "quantile"), ("--drop-labels", "__name__"), ("--drop-labels", "missing"), ("--drop-labels", "pod,"), ("--drop-labels", "pod,pod"), ("--drop-labels", "pod", "--metric", "absent"), ("--drop-labels", "pod", "--metric", "temperature_celsius"), ("--emit-config",), ("--drop-labels", "pod", "--drop-labels", "debug_id", "--emit-config"), ("--metric", "requests_total")):
            with self.subTest(args=args):
                result = self.run_cli(ROOT / "examples/cardinality/metrics.prom", "--format", "json", *args)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(result.stdout)
                self.assertIn("error", json.loads(result.stderr))

    def test_filesystem_errors_and_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(Path(directory) / "missing.prom", "--format", "json")
            self.assertEqual(result.returncode, 2)
            self.assertFalse(result.stdout)
            self.assertIn("error", json.loads(result.stderr))
            result = self.run_cli(directory, "--format", "json")
            self.assertEqual(result.returncode, 2)
        result = self.run_cli(ROOT / "examples/cardinality/metrics.prom", "--unknown-option")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(result.stdout)


if __name__ == "__main__":
    unittest.main()
