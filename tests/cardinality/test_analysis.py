# SPDX-License-Identifier: Apache-2.0
import re
import unittest
from pathlib import Path

from otelwrench.cardinality.analysis import emit_config, report
from otelwrench.cardinality.parser import InputError, parse

ROOT = Path(__file__).resolve().parents[2]


class AnalysisTests(unittest.TestCase):
    def test_all_collision_groups_and_every_witness(self):
        result = report(parse((ROOT / "examples/cardinality/collisions.prom").read_text()), [["pod"]], include_config=True)
        plan = result["plans"][0]
        self.assertEqual((plan["observed_keys_before"], plan["projected_keys_after"], plan["observed_cardinality_delta"]), (4, 2, -2))
        self.assertEqual(plan["collision_group_count"], 2)
        self.assertEqual(plan["colliding_original_series"], 4)
        self.assertEqual(plan["collision_metric_names"], ["requests_total"])
        self.assertEqual([c["sample_value_relation"] for c in plan["collisions"]], ["different", "equal"])
        self.assertEqual({o["original_labels"]["pod"] for c in plan["collisions"] for o in c["originals"]}, {"a", "b", "c", "d"})
        self.assertEqual(plan["collisions"][0]["originals"][1]["timestamp_ms"], "1700000000001")
        self.assertNotIn("metric_relabel_configs_yaml", plan)
        with self.assertRaisesRegex(InputError, "unsafe"):
            emit_config(plan)

    def test_three_way_collision_includes_every_original(self):
        plan = report(parse('x{p="a"} 1\nx{p="b"} 1\nx{p="c"} 2\n'), [["p"]])["plans"][0]
        self.assertEqual(plan["collisions"][0]["original_series_count"], 3)
        self.assertEqual(plan["observed_cardinality_delta"], -2)
        self.assertEqual(plan["collisions"][0]["sample_value_relation"], "different")

    def test_numeric_relations_are_float64_and_nan_is_explicit(self):
        for a, b, relation in [("-0", "0", "equal"), ("1", "1.0", "equal"), ("Inf", "+Inf", "equal"), ("Inf", "-Inf", "different"), ("NaN", "NaN", "indeterminate_nan"), ("9007199254740992", "9007199254740993", "equal")]:
            with self.subTest(a=a, b=b):
                plan = report(parse(f'x{{p="a"}} {a}\nx{{p="b"}} {b}\n'), [["p"]])["plans"][0]
                self.assertTrue(plan["unsafe"])
                collision = plan["collisions"][0]
                self.assertEqual(collision["sample_value_relation"], relation)
                self.assertEqual([w["value"] for w in collision["originals"]], [a, b])

    def test_plans_are_independent_and_input_is_unchanged(self):
        scrape = parse('x{p="a",debug="1"} 1\nx{p="b",debug="2"} 2\n')
        before = scrape.samples
        result = report(scrape, [["debug"], ["p"], ["p", "debug"]])
        self.assertEqual([p["projected_keys_after"] for p in result["plans"]], [2, 2, 1])
        self.assertEqual([p["unsafe"] for p in result["plans"]], [False, False, True])
        self.assertEqual(scrape.samples, before)

    def test_metric_scope_includes_unselected_series_in_accounting(self):
        scrape = parse('x{p="a"} 1\ny{p="b"} 2\ny{p="c"} 3\n')
        plan = report(scrape, [["p"]], metrics=["x"], include_config=True)["plans"][0]
        self.assertFalse(plan["unsafe"])
        self.assertEqual(plan["projected_keys_after"], 3)
        self.assertEqual(plan["affected_metric_names"], ["x"])
        self.assertEqual(plan["metric_counts"], {"x": {"before": 1, "after": 1}, "y": {"before": 2, "after": 2}})
        regex_line = next(line for line in plan["metric_relabel_configs_yaml"].splitlines() if "regex:" in line)
        pattern = regex_line.split("'", 2)[1]
        self.assertIsNotNone(re.fullmatch(pattern, "x"))
        for name in ("y", "x_total", "prefixx", "xx", "x\n"):
            self.assertIsNone(re.fullmatch(pattern, name))

    def test_metric_name_itself_is_never_removed(self):
        plan = report(parse('a{p="1"} 1\nb{p="2"} 1\n'), [["p"]])["plans"][0]
        self.assertEqual(plan["projected_keys_after"], 2)
        self.assertFalse(plan["unsafe"])

    def test_config_is_exactly_scoped_and_semantically_matches_projection(self):
        # Independent interpretation of this tiny emitted YAML grammar; no production analyzer reuse.
        scrape = parse('a:metric{keep="v",debug="1"} 1\na:metric{keep="w",debug="2"} 2\nb{debug="3"} 3\n')
        plan = report(scrape, [["debug"]], metrics=["a:metric"], include_config=True)["plans"][0]
        yaml = plan["metric_relabel_configs_yaml"]
        self.assertNotIn("labeldrop", yaml)
        self.assertIn("source_labels: [__name__]", yaml)
        self.assertIn("action: replace", yaml)
        self.assertIn("replacement: ''", yaml)
        regex = next(line.split("'", 2)[1] for line in yaml.splitlines() if "regex:" in line)
        target = next(line.split("'", 2)[1] for line in yaml.splitlines() if "target_label:" in line)
        projected = set()
        for sample in scrape.samples:
            labels = dict(sample.labels)
            if re.fullmatch(regex, sample.metric):
                labels.pop(target, None)
            projected.add((sample.metric, tuple(sorted(labels.items()))))
        self.assertEqual(projected, {("a:metric", (("keep", "v"),)), ("a:metric", (("keep", "w"),)), ("b", (("debug", "3"),))})
        self.assertEqual(len(projected), plan["projected_keys_after"])

    def test_invalid_plans_and_scope(self):
        scrape = parse('x{p="1"} 1\ny{q="2"} 2\n')
        for labels in ([], ["p", "p"], [""], ["bad-name"], ["__name__"], ["__secret"], ["le"], ["quantile"], ["absent"]):
            with self.subTest(labels=labels), self.assertRaises(InputError):
                report(scrape, [labels])
        for metrics in ([], ["absent"], [".*"]):
            with self.subTest(metrics=metrics), self.assertRaises(InputError):
                report(scrape, [["p"]], metrics=metrics)
        with self.assertRaisesRegex(InputError, "absent"):
            report(scrape, [["p"]], metrics=["y"])
        with self.assertRaises(InputError):
            report(scrape, metrics=["x"])
        for plans in ([], [["p"], ["q"]]):
            with self.assertRaises(InputError):
                report(scrape, plans, include_config=True)

    def test_autogenerated_metrics_config_refused(self):
        for metric in ("up", "scrape_duration_seconds"):
            scrape = parse(f'{metric}{{debug="1"}} 1\n')
            self.assertFalse(report(scrape, [["debug"]])["unsafe"])
            with self.assertRaisesRegex(InputError, "unsupported"):
                report(scrape, [["debug"]], include_config=True)

    def test_yaml_boolean_and_null_label_names_are_quoted(self):
        for label in ("true", "false", "null", "on", "no"):
            with self.subTest(label=label):
                plan = report(parse(f'x{{{label}="1"}} 1\n'), [[label]], include_config=True)["plans"][0]
                self.assertIn(f"target_label: '{label}'", plan["metric_relabel_configs_yaml"])

    def test_declared_metadata_without_samples_is_retained(self):
        result = report(parse('# HELP empty No samples this scrape.\n# TYPE empty gauge\nx 1\n'))
        self.assertEqual(result["summary"]["declared_metadata"], {"types": {"empty": "gauge"}, "help": {"empty": "No samples this scrape."}})

    def test_independent_key_accounting_for_every_label_subset(self):
        from itertools import combinations
        records = [("x" if i % 2 else "y", {"a": str(i % 3), "b": str(i % 2), "id": str(i)}) for i in range(24)]
        text = "".join(metric + "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "} 1\n" for metric, labels in records)
        for size in (1, 2, 3):
            for dropped in combinations(("a", "b", "id"), size):
                with self.subTest(dropped=dropped):
                    expected = {(metric, frozenset((k, v) for k, v in labels.items() if k not in dropped)) for metric, labels in records}
                    plan = report(parse(text), [list(dropped)])["plans"][0]
                    self.assertEqual(plan["projected_keys_after"], len(expected))
                    self.assertEqual(plan["unsafe"], len(expected) < len(records))


if __name__ == "__main__":
    unittest.main()
