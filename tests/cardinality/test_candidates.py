# SPDX-License-Identifier: Apache-2.0
"""Contract for ordered, named candidate-file v1 input and atomic rejection."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from otelwrench.cardinality import analysis
from otelwrench.cardinality.candidates import Candidate, load_candidates
from otelwrench.cardinality.parser import InputError, parse
from otelwrench.cardinality.scrapes import IdentifiedScrape

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "otelwrench", "cardinality"]


class CandidateFileTests(unittest.TestCase):
    def run_cli(self, source, path, *options, stdin=None):
        return subprocess.run([*CLI, *map(str, source), "--candidate-file", str(path),
                               *options], input=stdin, capture_output=True, cwd="/", timeout=60)

    def write_candidates(self, path, entries):
        path.write_text(json.dumps({"schema_version": 1, "candidates": entries}))

    def test_named_file_ranking_preserves_names_counts_and_every_witness(self):
        # Same six independently specified candidates as the hand-counted ranking
        # corpus, with names deliberately unrelated to rank or argument position.
        labels = [["a"], ["b"], ["b", "a"], ["debug"], ["debug", "b"], ["region", "debug"]]
        names = ["unsafe single", "Keep identity", "unsafe pair", "debug only", "two labels", "Trim région"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            self.write_candidates(path, [dict(name=n, drop_labels=p) for n, p in zip(names, labels)])
            original = path.read_bytes()
            args = ([ROOT / "examples/cardinality/ranking.prom"], path, "--rank-plans", "--format", "json")
            first = self.run_cli(*args)
            self.assertEqual(first.returncode, 1, first.stderr)
            self.assertFalse(first.stderr)
            self.assertEqual(first.stdout, self.run_cli(*args).stdout)
            result = json.loads(first.stdout)
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual([p["name"] for p in result["plans"]], names)
            self.assertEqual([p["projected_keys_after"] for p in result["plans"]], [5, 6, 4, 6, 6, 6])
            self.assertEqual([(p["name"], p["label_pairs_removed"]) for p in
                              result["candidate_ranking"]["ranked_safe_plans"]],
                             [(names[5], 10), (names[4], 8), (names[3], 6), (names[1], 2)])
            self.assertEqual([[(w["line"], w["value"], w["timestamp_ms"]) for w in c["originals"]]
                              for c in result["plans"][2]["collisions"]],
                             [[(1, "1", "10"), (2, "1.0", "11")], [(3, "2", "12"), (4, "2", "12")]])
            text = self.run_cli([ROOT / "examples/cardinality/ranking.prom"], path, "--rank-plans")
            self.assertEqual(text.returncode, 1, text.stderr)
            self.assertIn("1. Trim région drop debug,region: 10 label pairs removed", text.stdout.decode())
            self.assertIn(b"unsafe pair drop a,b", text.stdout)
            comparison = self.run_cli([ROOT / "examples/cardinality/ranking.prom"], path, "--format", "json")
            self.assertEqual(comparison.returncode, 1, comparison.stderr)
            compared = json.loads(comparison.stdout)
            self.assertEqual(compared["plans"], result["plans"])
            self.assertNotIn("candidate_ranking", compared)
            self.assertEqual(path.read_bytes(), original)

    def test_scrape_set_names_reach_local_union_and_ranking_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            self.write_candidates(path, [{"name": "pod identity", "drop_labels": ["pod"]},
                                         {"name": "empty zone", "drop_labels": ["zone"]}])
            source = ["--scrape-set", ROOT / "examples/cardinality/scrape-set.json"]
            result = self.run_cli(source, path, "--rank-plans", "--format", "json")
            self.assertEqual(result.returncode, 1, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["schema_version"], 2)
            self.assertEqual([p["name"] for p in output["plans"]], ["pod identity", "empty zone"])
            self.assertEqual(output["plans"][0]["status"], "unsafe_cross_scrape")
            self.assertEqual([w["scrape_id"] for w in output["plans"][0]["collisions"][0]["originals"]],
                             ["morning", "evening"])
            for scrape in output["scrapes"]:
                self.assertEqual([p["name"] for p in scrape["plans"]], ["pod identity", "empty zone"])
            self.assertEqual([p["name"] for p in output["candidate_ranking"]["ranked_safe_plans"]], ["empty zone"])
            text = self.run_cli(source, path, "--rank-plans")
            self.assertEqual(text.returncode, 1, text.stderr)
            self.assertEqual(text.stdout.count(b"pod identity drop pod"), 3)
            self.assertIn(b"1. empty zone", text.stdout)

    def test_exact_64_candidate_and_8_label_limits_with_and_without_ranking(self):
        labels = list("abcdefgh")
        candidates = [{"name": f"candidate-{mask}",
                       "drop_labels": [label for i, label in enumerate(labels) if mask & (1 << i)]}
                      for mask in range(1, 64)] + [{"name": "all eight", "drop_labels": labels}]
        scrape = ('x{' + ','.join(f'{k}="1"' for k in labels) + '} 1\n').encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            self.write_candidates(path, candidates)
            for rank in ([], ["--rank-plans"]):
                result = self.run_cli(["-"], path, *rank, "--format", "json", stdin=scrape)
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(len(output["plans"]), 64)
                self.assertTrue(all(p["projected_keys_after"] == 1 for p in output["plans"]))
                if rank:
                    self.assertEqual(output["candidate_ranking"]["ranked_safe_plans"][0]["name"], "all eight")
            for entries, diagnostic in ((candidates + [{"name": "65th", "drop_labels": ["g"]}], b"64"),
                                        ([{"name": "nine", "drop_labels": list("abcdefghi")}], b"8")):
                self.write_candidates(path, entries)
                for rank in ([], ["--rank-plans"]):
                    result = self.run_cli(["-"], path, *rank, "--format", "json", stdin=scrape)
                    self.assert_invalid(result)
                    self.assertIn(diagnostic, result.stderr)

    def assert_invalid(self, result):
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, b"")
        self.assertEqual(json.loads(result.stderr)["error_type"], "invalid_or_unsupported_input")

    def test_malformed_and_late_invalid_candidates_fail_atomically_in_both_modes(self):
        good = {"name": "valid first", "drop_labels": ["debug"]}
        invalid_entries = [None, [], {}, {**good, "extra": 1}, {"name": "no labels"},
                           {"drop_labels": ["b"]}, {**good, "name": None}, {**good, "name": 1},
                           *[{**good, "name": n} for n in ("", " ", " leading", "trailing ", "x\ny", "\x1b[31m", "a" * 65)],
                           *[{"name": "invalid later", "drop_labels": p} for p in
                             (None, "b", {}, [], [1], [True], [None], [[]], ["b", "b"], [" b"],
                              ["b,"], ["le"], ["quantile"], ["__name__"], ["missing"])],
                           {"name": "valid first", "drop_labels": ["b"]},
                           {"name": "new name", "drop_labels": ["debug"]}]
        invalid = [None, [], {}, {"schema_version": True, "candidates": [good]},
                   {"schema_version": 1.0, "candidates": [good]}, {"schema_version": 2, "candidates": [good]},
                   {"schema_version": 1, "candidates": []}, {"schema_version": 1, "candidates": {}},
                   {"schema_version": 1, "candidates": [good], "extra": 1}]
        invalid += [{"schema_version": 1, "candidates": [good, entry]} for entry in invalid_entries]
        invalid.append({"schema_version": 1, "candidates": [
            {"name": "one", "drop_labels": ["b", "debug"]},
            {"name": "two", "drop_labels": ["debug", "b"]}]})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            # Both sources expose the labels needed by the valid prefix.
            source = Path(directory) / "input.prom"
            source.write_text('x{debug="1",b="1"} 1\n')
            manifest = Path(directory) / "scrapes.json"
            manifest.write_text(json.dumps({"schema_version": 1, "scrapes": [{
                "id": "s", "source": "test", "captured_at": "2026-11-11T08:00:00Z", "path": "input.prom"}]}))
            for data in invalid:
                path.write_text(json.dumps(data))
                for args in ([source], ["--scrape-set", manifest]):
                    with self.subTest(data=data, args=args):
                        self.assert_invalid(self.run_cli(args, path, "--format", "json"))
            for data in (b'{', b'\xff', b'{"schema_version":1,"schema_version":1,"candidates":[]}',
                         b'{"schema_version":1,"candidates":[{"name":"a","name":"b","drop_labels":["b"]}]}',
                         b'{"schema_version":1,"candidates":[{"name":"a","drop_labels":["b"],"drop_labels":["debug"]}]}',
                         b'{"schema_version":NaN,"candidates":[]}', b'{"schema_version":Infinity,"candidates":[]}',
                         b'[' * 1500 + b']' * 1500):
                path.write_bytes(data)
                self.assert_invalid(self.run_cli([source], path, "--format", "json"))
            for bad_path in (path.parent, path.parent / "missing.json", "-"):
                self.assert_invalid(self.run_cli([source], bad_path, "--format", "json"))

    def test_scope_config_and_conflicting_options(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            self.write_candidates(path, [{"name": "scoped debug", "drop_labels": ["debug"]}])
            source = [ROOT / "examples/cardinality/ranking.prom"]
            result = self.run_cli(source, path, "--metric", "x", "--emit-config", "--format", "json")
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)["plans"][0]
            self.assertEqual((plan["name"], plan["scope_metric_names"], plan["label_pairs_removed"]), ("scoped debug", ["x"], 2))
            self.assertIn("metric_relabel_configs_yaml", plan)
            for args in (["--drop-labels", "a"], ["--candidate-file", str(path)],
                         ["--rank-plans", "--emit-config"], ["--metric", "missing"]):
                self.assert_invalid(self.run_cli(source, path, *args, "--format", "json"))
            self.assert_invalid(self.run_cli(["--scrape-set", ROOT / "examples/cardinality/scrape-set.json"], path,
                                             "--emit-config", "--format", "json"))
            self.write_candidates(path, [{"name": "unsafe", "drop_labels": ["a"]}])
            unsafe = self.run_cli(source, path, "--emit-config", "--format", "json")
            self.assertEqual(unsafe.returncode, 1, unsafe.stderr)
            plan = json.loads(unsafe.stdout)["plans"][0]
            self.assertEqual(plan["name"], "unsafe")
            self.assertIn("config_refusal", plan)
            self.assertNotIn("metric_relabel_configs_yaml", plan)
            self.write_candidates(path, [{"name": "one", "drop_labels": ["a"]},
                                         {"name": "two", "drop_labels": ["b"]}])
            self.assert_invalid(self.run_cli(source, path, "--emit-config", "--format", "json"))

    def test_library_loader_names_and_ranking_ties_do_not_depend_on_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            self.write_candidates(path, [{"name": "z" * 64, "drop_labels": ["debug"]},
                                         {"name": "A", "drop_labels": ["region", "b"]}])
            candidates = load_candidates(path)
            self.assertEqual(candidates, [Candidate("z" * 64, ("debug",)), Candidate("A", ("region", "b"))])
        scrape = parse((ROOT / "examples/cardinality/ranking.prom").read_text())
        scrapes = [IdentifiedScrape("s", "test", "2026-11-11T08:00:00Z", scrape)]
        labels = [c.drop_labels for c in candidates]
        for report, source in ((analysis.report, scrape), (analysis.report_scrapes, scrapes)):
            for names in ([c.name for c in candidates], ["A", "a"]):
                result = report(source, labels, plan_names=names, rank_plans=True)
                self.assertEqual([p["name"] for p in result["candidate_ranking"]["ranked_safe_plans"]], list(reversed(names)))
            for names in ([], ["only one"], ["same", "same"], "ab", [None, "ok"]):
                with self.subTest(report=report.__name__, names=names), patch("otelwrench.cardinality.analysis._analyze_validated") as project:
                    with self.assertRaises(InputError):
                        report(source, labels, plan_names=names)
                    project.assert_not_called()
            for plans in ([list("abcdefghi")], [["debug"]] * 65, [["debug"], ["debug"]]):
                with self.subTest(report=report.__name__, plans=plans), patch("otelwrench.cardinality.analysis._analyze_validated") as project:
                    with self.assertRaises(InputError):
                        report(source, plans, plan_names=[str(i) for i in range(len(plans))])
                    project.assert_not_called()
        # Ordinary library comparison retains iterable labels/plans and default names.
        result = analysis.report(scrape, (iter(p) for p in [["debug"], ["b"]]))
        self.assertEqual([p["name"] for p in result["plans"]], ["plan-1", "plan-2"])


if __name__ == "__main__":
    unittest.main()
