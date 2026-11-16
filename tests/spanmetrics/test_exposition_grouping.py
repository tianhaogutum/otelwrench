# SPDX-License-Identifier: Apache-2.0
"""Prometheus text exposition: each metric must be one contiguous group, HELP/TYPE first."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from prometheus_client.parser import text_string_to_metric_families


ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "otelwrench", "spanmetrics"]


class ExpositionGroupingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name) / "out.prom"
        # examples/dimensions.json with one custom dimension yields several groups.
        result = subprocess.run(
            [*CLI, str(ROOT / "examples/spanmetrics/dimensions.json"), "--dimension", "span:method=method",
             "--buckets", "0.1,0.5,1", "--output", str(self.output)],
            cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.text = self.output.read_text(encoding="utf-8")

    def test_each_metric_is_one_group_with_header_first(self):
        current = None
        closed = set()
        for line in self.text.splitlines():
            if line.startswith(("# HELP ", "# TYPE ")):
                name = line.split()[2]
                if name != current:
                    self.assertNotIn(name, closed, f"metric {name} reopened after another metric")
                    if current is not None:
                        closed.add(current)
                    current = name
                continue
            self.assertFalse(line.startswith("#"), line)
            self.assertIsNotNone(current, f"sample before any HELP/TYPE: {line}")
            sample_name = line.split("{", 1)[0].split(" ", 1)[0]
            self.assertTrue(sample_name.startswith(current),
                            f"sample {sample_name} is outside its metric group {current}")
            self.assertNotIn(sample_name, closed, line)

    def test_parser_sees_typed_families_with_samples(self):
        families = list(text_string_to_metric_families(self.text))
        self.assertEqual({f.name: f.type for f in families},
                         {"spanmetrics_calls": "counter", "spanmetrics_errors": "counter",
                          "spanmetrics_duration_seconds": "histogram"})
        for family in families:
            with self.subTest(family=family.name):
                self.assertTrue(family.samples)
        calls = [s for f in families if f.name == "spanmetrics_calls" for s in f.samples
                 if s.name == "spanmetrics_calls_total"]
        self.assertEqual(sum(s.value for s in calls), 6)


if __name__ == "__main__":
    unittest.main()
