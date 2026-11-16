# SPDX-License-Identifier: Apache-2.0
import unittest
from pathlib import Path

from otelwrench.cardinality.analysis import report
from otelwrench.cardinality.parser import InputError, parse

ROOT = Path(__file__).resolve().parents[2]


class CoreTests(unittest.TestCase):
    def test_safe_fixture(self):
        result = report(parse((ROOT / "examples/cardinality/metrics.prom").read_text()), [["debug_id"]], include_config=True)
        self.assertEqual(result["summary"]["observed_series"], 5)
        plan = result["plans"][0]
        self.assertFalse(plan["unsafe"])
        self.assertEqual(plan["projected_keys_after"], 5)
        self.assertIn("replacement: ''", plan["metric_relabel_configs_yaml"])

    def test_unequal_collision_and_refused_config(self):
        result = report(parse('requests_total{pod="a",route="/"} 2\nrequests_total{pod="b",route="/"} 4\n'), [["pod"]], include_config=True)
        plan = result["plans"][0]
        self.assertTrue(plan["unsafe"])
        self.assertEqual(plan["projected_keys_after"], 1)
        self.assertEqual(plan["collisions"][0]["sample_value_relation"], "different")
        self.assertEqual(len(plan["collisions"][0]["originals"]), 2)
        self.assertNotIn("metric_relabel_configs_yaml", plan)

    def test_equal_collision_is_unsafe(self):
        plan = report(parse('x{pod="a"} 2\nx{pod="b"} 2.0\n'), [["pod"]])["plans"][0]
        self.assertTrue(plan["unsafe"])
        self.assertEqual(plan["collisions"][0]["sample_value_relation"], "equal")

    def test_protected_labels(self):
        for label in ("__name__", "le", "quantile"):
            with self.subTest(label=label), self.assertRaisesRegex(InputError, "protected"):
                report(parse('x{pod="a"} 1\n'), [[label]])

    def test_duplicate_input(self):
        with self.assertRaisesRegex(InputError, "duplicate input series"):
            parse('x{a="1",b="2"} 1\nx{b="2",a="1"} 2\n')

    def test_bad_input_and_absent_label(self):
        for text in ('x{a="unterminated} 1\n', 'x nonsense\n', 'x 1 # exemplar\n'):
            with self.subTest(text=text), self.assertRaises(InputError):
                parse(text)
        with self.assertRaisesRegex(InputError, "absent"):
            report(parse("x 1\n"), [["missing"]])


if __name__ == "__main__":
    unittest.main()
