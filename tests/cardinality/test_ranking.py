# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from otelwrench.cardinality.analysis import report
from otelwrench.cardinality.parser import InputError, parse

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "otelwrench", "cardinality"]
PLANS = [["a"], ["b"], ["a", "b"], ["debug"], ["b", "debug"], ["debug", "region"]]


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "examples/cardinality/ranking.prom").read_text()
        self.scrape = parse(self.text)

    def test_exact_counts_rank_only_safe_and_keep_collision_evidence(self):
        result = report(self.scrape, PLANS, rank_plans=True)
        self.assertEqual([p["projected_keys_after"] for p in result["plans"]], [5, 6, 4, 6, 6, 6])
        self.assertEqual([p["label_pairs_removed"] for p in result["plans"]], [4, 2, 6, 6, 8, 10])
        self.assertEqual([p["label_pairs_before"] for p in result["plans"]], [18] * 6)
        self.assertEqual([p["label_pairs_after"] for p in result["plans"]], [14, 16, 12, 12, 10, 8])
        ranking = result["candidate_ranking"]
        self.assertEqual(ranking["limits"], {"max_candidates": 64, "max_labels_per_candidate": 8})
        self.assertEqual((ranking["evaluated_candidates"], ranking["excluded_unsafe_candidates"]), (6, 2))
        self.assertEqual(ranking["objective"], "label_pairs_removed_descending")
        self.assertEqual(ranking["tie_break"], "drop_labels_lexicographic")
        self.assertEqual([(r["rank"], r["name"], r["drop_labels"], r["label_pairs_removed"])
                          for r in ranking["ranked_safe_plans"]], [
            (1, "plan-6", ["debug", "region"], 10),
            (2, "plan-5", ["b", "debug"], 8),
            (3, "plan-4", ["debug"], 6),
            (4, "plan-2", ["b"], 2),
        ])
        self.assertTrue(result["unsafe"])
        # Exact hand-written witnesses: single-label collision on y; interaction on x.
        single = result["plans"][0]["collisions"]
        self.assertEqual([(c["projected_key"]["metric"], c["sample_value_relation"],
                           [(o["line"], o["value"], o["timestamp_ms"]) for o in c["originals"]])
                          for c in single], [("y", "equal", [(3, "2", "12"), (4, "2", "12")])])
        interaction = result["plans"][2]["collisions"]
        self.assertEqual([c["projected_key"]["metric"] for c in interaction], ["x", "y"])
        self.assertEqual([(o["value"], o["timestamp_ms"]) for o in interaction[0]["originals"]],
                         [("1", "10"), ("1.0", "11")])
        self.assertTrue(all("metric_relabel_configs_yaml" not in p for p in result["plans"]))

    def test_rank_ties_ignore_candidate_label_and_sample_order(self):
        plans = [["debug"], ["region", "b"], ["b"], ["a"]]

        def ranked(scrape, candidates):
            result = report(scrape, candidates, rank_plans=True)
            return [(p["rank"], p["drop_labels"], p["label_pairs_removed"])
                    for p in result["candidate_ranking"]["ranked_safe_plans"]]

        expected = [(1, ["b", "region"], 6), (2, ["debug"], 6), (3, ["b"], 2)]
        self.assertEqual(ranked(self.scrape, plans), expected)
        self.assertEqual(ranked(parse("\n".join(reversed(self.text.splitlines())) + "\n"),
                                [list(reversed(p)) for p in reversed(plans)]), expected)
        self.assertEqual(report(self.scrape, plans, rank_plans=True),
                         report(self.scrape, plans, rank_plans=True))

    def test_scope_empty_values_and_no_safe_candidates(self):
        scrape = parse('x{p="a",blank=""} 1\ny{p="a"} 1\ny{p="b"} 1\n')
        result = report(scrape, [["p", "blank"]], metrics=["x"], rank_plans=True)
        plan = result["plans"][0]
        self.assertEqual((plan["label_pairs_before"], plan["label_pairs_after"], plan["label_pairs_removed"]), (4, 2, 2))
        self.assertEqual(plan["projected_keys_after"], 3)
        self.assertFalse(result["unsafe"])
        unsafe = report(self.scrape, [["a"], ["a", "b"]], rank_plans=True)
        self.assertEqual(unsafe["candidate_ranking"]["ranked_safe_plans"], [])

    def test_limit_boundaries_and_invalid_candidates_fail_before_projection(self):
        # 64 distinct nonempty combinations, including an 8-label candidate.
        labels = list("abcdefgh")
        scrape = parse("x{" + ",".join(f'{k}="1"' for k in labels) + "} 1\n")
        candidates = [[label for i, label in enumerate(labels) if mask & (1 << i)]
                      for mask in range(1, 64)] + [labels]
        result = report(scrape, candidates, rank_plans=True)
        self.assertEqual(result["candidate_ranking"]["evaluated_candidates"], 64)
        self.assertEqual(result["candidate_ranking"]["ranked_safe_plans"][0]["drop_labels"], labels)
        invalid = [
            ([], "at least one"),
            (candidates + [["g"]], "64"),
            ([list("abcdefghi")], "8"),
            ([["b", "a"], ["a", "b"]], "duplicate candidate"),
        ]
        for candidates, message in invalid:
            with self.subTest(candidates=candidates), patch("otelwrench.cardinality.analysis.analyze_plan") as analyze:
                with self.assertRaisesRegex(InputError, message):
                    report(scrape, candidates, rank_plans=True)
                analyze.assert_not_called()
        with self.assertRaisesRegex(InputError, "emit-config"):
            report(self.scrape, [["debug"]], rank_plans=True, include_config=True)

    def run_cli(self, *args, stdin=None):
        return subprocess.run([*CLI, *map(str, args)], input=stdin, capture_output=True, timeout=60, cwd=ROOT)

    def test_installed_cli_json_and_text_ranking(self):
        args = [ROOT / "examples/cardinality/ranking.prom", "--rank-plans"]
        for labels in PLANS:
            args += ["--drop-labels", ",".join(labels)]
        first = self.run_cli(*args, "--format", "json")
        second = self.run_cli(*args, "--format", "json")
        self.assertEqual(first.returncode, 1, first.stderr)
        self.assertFalse(first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        result = json.loads(first.stdout)
        self.assertEqual([p["name"] for p in result["candidate_ranking"]["ranked_safe_plans"]],
                         ["plan-6", "plan-5", "plan-4", "plan-2"])
        text = self.run_cli(*args)
        self.assertEqual(text.returncode, 1, text.stderr)
        for fragment in (b"max 64 candidates", b"8 labels each", b"1. plan-6", b"10 label pairs removed", b'"line": 4'):
            self.assertIn(fragment, text.stdout)
        safe = self.run_cli(ROOT / "examples/cardinality/ranking.prom", "--rank-plans", "--drop-labels", "debug")
        self.assertEqual(safe.returncode, 0, safe.stderr)
        unsafe = self.run_cli(ROOT / "examples/cardinality/ranking.prom", "--rank-plans", "--drop-labels", "a")
        self.assertIn(b"No collision-free candidates", unsafe.stdout)
        self.assertEqual(unsafe.returncode, 1)

    def test_invalid_cli_requests_never_emit_partial_rankings(self):
        cases = [[], ["--drop-labels", "debug", "--emit-config"],
                 ["--drop-labels", "debug,region", "--drop-labels", "region,debug"],
                 ["--drop-labels", "a,b,c,d,e,f,g,h,i"],
                 ["--drop-labels", "debug"] * 65,
                 ["--drop-labels", "debug", "--drop-labels", "absent"],
                 ["--drop-labels", "le"], ["--drop-labels", "a,a"],
                 ["--drop-labels", "debug", "--metric", "missing"]]
        for args in cases:
            with self.subTest(args=args):
                result = self.run_cli(ROOT / "examples/cardinality/ranking.prom", "--rank-plans", "--format", "json", *args)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(result.stdout)
                self.assertIn("error", json.loads(result.stderr))


if __name__ == "__main__":
    unittest.main()
