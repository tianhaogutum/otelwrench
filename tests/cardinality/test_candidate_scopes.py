# SPDX-License-Identifier: Apache-2.0
"""Candidate-file v2: independent scopes, full evidence and atomic validation."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from otelwrench.cardinality import analysis
from otelwrench.cardinality.candidates import load_candidates
from otelwrench.cardinality.parser import InputError, parse
from otelwrench.cardinality.scrapes import IdentifiedScrape

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "otelwrench", "cardinality"]
SOURCE = ROOT / "examples/cardinality/scoped-metrics.prom"
CANDIDATES = ROOT / "examples/cardinality/scoped-candidates.json"
# Literal independent observations: no production parser/key/projection helpers.
ROWS = [
    ("safe_metric", dict(pod="a", route="/a", debug="x"), "1", "10", 1),
    ("safe_metric", dict(pod="b", route="/b", debug=""), "1.0", "11", 2),
    ("risky_metric", dict(pod="a", route="/same", debug="k"), "2", "12", 3),
    ("risky_metric", dict(pod="b", route="/same", debug="k"), "3", "13", 4),
    ("aux_metric", dict(debug="aux"), "4", "14", 5),
]


class CandidateScopeTests(unittest.TestCase):
    def cli(self, source, path=CANDIDATES, *options):
        return subprocess.run([*CLI, *map(str, source), "--candidate-file", str(path),
                               *options], cwd="/", capture_output=True, timeout=60)

    def assert_invalid(self, proc):
        self.assertEqual(proc.returncode, 2, (proc.stdout, proc.stderr))
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(json.loads(proc.stderr)["error_type"], "invalid_or_unsupported_input")

    def test_v2_scopes_independently_project_score_and_retain_every_witness(self):
        proc = self.cli([SOURCE], CANDIDATES, "--rank-plans", "--format", "json")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(proc.stderr, b"")
        result = json.loads(proc.stdout)
        entries = json.loads(CANDIDATES.read_text())["candidates"]
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual([p["name"] for p in result["plans"]], [e["name"] for e in entries])
        self.assertEqual([p["projected_keys_after"] for p in result["plans"]], [5, 4, 4, 5, 5, 5])
        self.assertEqual([p["label_pairs_removed"] for p in result["plans"]], [2, 2, 4, 2, 2, 4])
        for plan, entry in zip(result["plans"], entries):
            self.assert_plan(plan, entry, ROWS)
        self.assertEqual([p["name"] for p in result["candidate_ranking"]["ranked_safe_plans"]],
                         ["Safe pair", "Safe debug", "Risky debug", "Safe pod"])
        self.assertEqual(proc.stdout, self.cli([SOURCE], CANDIDATES, "--rank-plans", "--format", "json").stdout)
        text = self.cli([SOURCE], CANDIDATES, "--rank-plans")
        self.assertEqual(text.returncode, 1, text.stderr)
        self.assertIn(b"Scope metrics: safe_metric", text.stdout)
        self.assertIn(b"Scope metrics: risky_metric, safe_metric", text.stdout)

    def write_file(self, path, entries, version=2):
        path.write_text(json.dumps(dict(schema_version=version, candidates=entries)), encoding="utf-8")

    def write_set(self, directory):
        directory = Path(directory)
        lines = SOURCE.read_text().splitlines(keepends=True)
        entries, row_sets = [], []
        # Split the colliding pair across captures; a third capture has neither
        # selected metric nor selected label. Scope validation must use the union.
        for id, indexes in (("a", [0, 2, 4]), ("b", [1, 3, 4]), ("c", [4])):
            path = directory / f"{id}.prom"
            path.write_text("".join(lines[i] for i in indexes))
            entry = dict(id=id, source=f"exporter-{id}", captured_at="2026-11-11T08:00:00Z", path=path.name)
            entries.append(entry)
            row_sets.append([(ROWS[i][0], ROWS[i][1], ROWS[i][2], ROWS[i][3], line)
                             for line, i in enumerate(indexes, 1)])
        manifest = directory / "set.json"
        manifest.write_text(json.dumps(dict(schema_version=1, scrapes=entries)))
        return manifest, entries, row_sets

    def test_scopes_validate_on_union_and_cross_scrape_witnesses_are_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, identities, row_sets = self.write_set(directory)
            proc = self.cli(["--scrape-set", manifest], CANDIDATES, "--rank-plans", "--format", "json")
            self.assertEqual(proc.returncode, 1, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["schema_version"], 2)
            self.assertEqual(result["summary"], dict(scrape_count=3, sample_observations=7, observed_series=5))
            entries = json.loads(CANDIDATES.read_text())["candidates"]
            rows, provenance = [], []
            for identity, local_rows, scrape in zip(identities, row_sets, result["scrapes"]):
                origin = dict(scrape_id=identity["id"], source=identity["source"], captured_at=identity["captured_at"])
                rows.extend(local_rows)
                provenance.extend([origin] * len(local_rows))
                self.assertEqual([p["name"] for p in scrape["plans"]], [e["name"] for e in entries])
                for plan, entry in zip(scrape["plans"], entries):
                    self.assert_plan(plan, entry, local_rows, [origin] * len(local_rows))
                    self.assertTrue(plan["safe"])
            for plan, entry in zip(result["plans"], entries):
                self.assert_plan(plan, entry, rows, provenance, union=True)
                self.assertTrue(plan["all_scrapes_safe"])
                self.assertEqual(plan["status"], "unsafe_cross_scrape" if plan["unsafe"] else "observed_safe")
            self.assertEqual([p["name"] for p in result["candidate_ranking"]["ranked_safe_plans"]],
                             ["Safe pair", "Safe debug", "Risky debug", "Safe pod"])
            text = self.cli(["--scrape-set", manifest], CANDIDATES)
            self.assertEqual(text.returncode, 1, text.stderr)
            self.assertEqual(text.stdout.count(b"Scope metrics:"), 24)
            # A metric observed only outside this single scrape must still fail.
            bad = Path(directory) / "bad.json"
            self.write_file(bad, [dict(name="not in a", drop_labels=["pod"], metrics=["absent_metric"])])
            self.assert_invalid(self.cli(["--scrape-set", manifest], bad, "--format", "json"))

    def test_wrong_late_scopes_fields_and_combinations_fail_atomically(self):
        good = dict(name="valid first", drop_labels=["pod"], metrics=["safe_metric"])
        invalid = [None, [], {}, {"name": "missing metrics", "drop_labels": ["debug"]},
                   {**good, "name": "later", "extra": 1},
                   *[{"name": "later", "drop_labels": ["pod"], "metrics": scope} for scope in
                     (None, True, 1, "safe_metric", {}, [], [None], [1], [True], [[]], [{}],
                      [""], ["safe_metric "], [" safe_metric"], ["safe_metric\n"],
                      ["safe.*"], ["safe_metric", "safe_metric"], ["absent_metric"],
                      ["safe_metric", "absent_metric"], ["aux_metric"])],
                   {"name": "valid first", "drop_labels": ["debug"], "metrics": ["risky_metric"]},
                   {"name": "same combination", "drop_labels": ["pod"], "metrics": ["safe_metric"]},
                   {"name": "missing label", "drop_labels": ["route", "missing"], "metrics": ["safe_metric"]},
                   {"name": "protected", "drop_labels": ["le"], "metrics": ["safe_metric"]}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            manifest, _, _ = self.write_set(directory)
            sources = ([SOURCE], ["--scrape-set", manifest])
            for late in invalid:
                self.write_file(path, [good, late])
                for source in sources:
                    for rank in ([], ["--rank-plans"]):
                        with self.subTest(late=late, source=source, rank=rank):
                            self.assert_invalid(self.cli(source, path, *rank, "--format", "json"))
            # Sorting both arrays defines combination identity, independently of names.
            self.write_file(path, [dict(name="first", drop_labels=["pod", "debug"], metrics=["safe_metric", "risky_metric"]),
                                   dict(name="second", drop_labels=["debug", "pod"], metrics=["risky_metric", "safe_metric"])])
            self.assert_invalid(self.cli([SOURCE], path, "--format", "json"))
            for raw in (
                '{"schema_version":2,"candidates":[{"name":"one","drop_labels":["pod"],"metrics":["safe_metric"],"metrics":["risky_metric"]}]}',
                '{"schema_version":2,"candidates":[],"unknown":true}',
                '{"schema_version":2.0,"candidates":[]}',
                '{"schema_version":3,"candidates":[]}',
            ):
                path.write_text(raw)
                self.assert_invalid(self.cli([SOURCE], path, "--format", "json"))
            for source in sources:
                for rank in ([], ["--rank-plans"]):
                    self.assert_invalid(self.cli(source, CANDIDATES, "--metric", "safe_metric", *rank, "--format", "json"))
            self.write_file(path, [good], version=1)  # v1 must reject the new field.
            self.assert_invalid(self.cli([SOURCE], path, "--format", "json"))

    def test_v2_bounds_with_same_labels_in_64_distinct_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            source = Path(directory) / "input.prom"
            source.write_text("".join(f'm{i}{{' + ','.join(f'{k}="v"' for k in "abcdefgh") + '} 1\n'
                                      for i in range(64)))
            candidates = [dict(name=f"candidate-{i}", drop_labels=["a"], metrics=[f"m{i}"]) for i in range(64)]
            candidates[-1]["drop_labels"] = list("abcdefgh")
            self.write_file(path, candidates)
            for rank in ([], ["--rank-plans"]):
                proc = self.cli([source], path, *rank, "--format", "json")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                result = json.loads(proc.stdout)
                self.assertEqual(len(result["plans"]), 64)
                self.assertEqual([p["label_pairs_removed"] for p in result["plans"]], [1] * 63 + [8])
                self.assertTrue(all(p["projected_keys_after"] == 64 for p in result["plans"]))
                if rank:
                    self.assertEqual(result["candidate_ranking"]["ranked_safe_plans"][0]["name"], "candidate-63")
            for entries, message in ((candidates + [dict(name="65th", drop_labels=["b"], metrics=["m0"])], b"64"),
                                     ([dict(name="nine", drop_labels=list("abcdefghi"), metrics=["m0"])], b"8")):
                self.write_file(path, entries)
                for rank in ([], ["--rank-plans"]):
                    proc = self.cli([source], path, *rank, "--format", "json")
                    self.assert_invalid(proc)
                    self.assertIn(message, proc.stderr)

    def test_library_scopes_prevalidate_all_candidates_and_v1_shared_scope_is_unchanged(self):
        loaded = load_candidates(CANDIDATES)
        self.assertEqual([c.metrics for c in loaded], [("safe_metric",), ("risky_metric",),
                         ("risky_metric", "safe_metric"), ("safe_metric",), ("risky_metric",), ("safe_metric",)])
        scrape = parse(SOURCE.read_text())
        identified = [IdentifiedScrape("one", "test", "2026-11-11T08:00:00Z", scrape)]
        for report, source in ((analysis.report, scrape), (analysis.report_scrapes, identified)):
            for rank in (False, True):
                result = report(source, [c.drop_labels for c in loaded], plan_names=[c.name for c in loaded],
                                plan_metrics=[c.metrics for c in loaded], rank_plans=rank)
                self.assertEqual([p["label_pairs_removed"] for p in result["plans"]], [2, 2, 4, 2, 2, 4])
            invalid = [[], "xx", [["safe_metric"]], [["safe_metric"], None],
                       [["safe_metric"], []], [["safe_metric"], ["risky_metric", "risky_metric"]],
                       [["safe_metric"], ["missing"]], [["safe_metric"], ["aux_metric"]],
                       [["safe_metric"], ["safe_metric"]]]
            for scopes in invalid:
                with self.subTest(report=report.__name__, scopes=scopes), patch("otelwrench.cardinality.analysis._analyze_validated") as project:
                    with self.assertRaises(InputError):
                        report(source, [["pod"], ["pod"]], plan_metrics=scopes)
                    project.assert_not_called()
            with self.assertRaisesRegex(InputError, "--metric"):
                report(source, [["pod"]], metrics=["safe_metric"], plan_metrics=[["safe_metric"]])
            # Existing shared metric selection remains byte-for-byte the same JSON.
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "v1.json"
                self.write_file(path, [dict(name="pod", drop_labels=["pod"]), dict(name="debug", drop_labels=["debug"])], version=1)
                candidates = load_candidates(path)
                self.assertTrue(all(c.metrics is None for c in candidates))
                expected = report(source, [["pod"], ["debug"]], metrics=["safe_metric"],
                                  plan_names=["pod", "debug"], rank_plans=True)
                actual = report(source, [c.drop_labels for c in candidates], metrics=["safe_metric"],
                                plan_names=[c.name for c in candidates], rank_plans=True)
                self.assertEqual(actual, expected)
                cli_source = [SOURCE] if report is analysis.report else ["--scrape-set", self.write_single_set(Path(directory))]
                proc = self.cli(cli_source, path, "--metric", "safe_metric", "--rank-plans", "--format", "json")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(proc.stdout), expected)
        # Exact names containing ':' are valid; family names are not expanded.
        result = analysis.report(parse('a:count{pod="x"} 1\n'), [["pod"]], plan_metrics=[["a:count"]])
        self.assertEqual(result["plans"][0]["scope_metric_names"], ["a:count"])

    def write_single_set(self, directory):
        path = directory / "single-set.json"
        path.write_text(json.dumps(dict(schema_version=1, scrapes=[dict(
            id="one", source="test", captured_at="2026-11-11T08:00:00Z", path=str(SOURCE))])))
        return path

    def test_single_scoped_config_uses_exact_candidate_metrics_and_restrictions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            entry = dict(name="safe", drop_labels=["pod"], metrics=["safe_metric"])
            self.write_file(path, [entry])
            proc = self.cli([SOURCE], path, "--emit-config", "--format", "json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            plan = json.loads(proc.stdout)["plans"][0]
            self.assertIn("regex: '^(?:safe_metric)$'", plan["metric_relabel_configs_yaml"])
            self.assertNotIn("risky_metric", plan["metric_relabel_configs_yaml"])
            self.assert_invalid(self.cli([SOURCE], path, "--rank-plans", "--emit-config", "--format", "json"))
            manifest = self.write_single_set(Path(directory))
            self.assert_invalid(self.cli(["--scrape-set", manifest], path, "--emit-config", "--format", "json"))
            entry["metrics"] = ["risky_metric"]
            self.write_file(path, [entry])
            proc = self.cli([SOURCE], path, "--emit-config", "--format", "json")
            self.assertEqual(proc.returncode, 1, proc.stderr)
            plan = json.loads(proc.stdout)["plans"][0]
            self.assertIn("config_refusal", plan)
            self.assertNotIn("metric_relabel_configs_yaml", plan)

    def assert_plan(self, plan, entry, rows, provenance=None, union=False):
        # Independently project each literal observation; empty values are absent
        # for identity while explicit original labels remain in evidence/score.
        def key(metric, labels):
            return metric, tuple(sorted((k, v) for k, v in labels.items() if v != ""))

        def obj(identity):
            return dict(metric=identity[0], labels=dict(identity[1]))

        groups, originals = {}, set()
        before_pairs = after_pairs = 0
        affected = set()
        before_metrics, after_metrics = {}, {}
        for i, (metric, labels, value, timestamp, line) in enumerate(rows):
            projected = {k: v for k, v in labels.items()
                         if metric not in entry["metrics"] or k not in entry["drop_labels"]}
            original, after = key(metric, labels), key(metric, projected)
            originals.add(original)
            before_pairs += len(labels)
            after_pairs += len(projected)
            if projected != labels:
                affected.add(metric)
            before_metrics.setdefault(metric, set()).add(original)
            after_metrics.setdefault(metric, set()).add(after)
            witness = dict(original_key=obj(original), original_labels=labels,
                           value=value, timestamp_ms=timestamp, line=line)
            if provenance:
                witness.update(provenance[i])
            groups.setdefault(after, []).append((original, witness))
        collisions = []
        for identity, group in sorted(groups.items()):
            distinct = {original for original, _ in group}
            if len(distinct) < 2:
                continue
            # Fixture collision values are deliberately unequal.
            witnesses = [w for _, w in sorted(group, key=lambda p: (
                p[1].get("scrape_id", ""), p[0]))]
            collision = dict(projected_key=obj(identity), sample_value_relation="different",
                             original_series_count=len(distinct), originals=witnesses)
            if union:
                local = {}
                for original, w in group:
                    local.setdefault(w["scrape_id"], set()).add(original)
                collision.update(sample_observation_count=len(group), classification=(
                    "within_scrape" if any(len(s) > 1 for s in local.values()) else "cross_scrape_only"))
            collisions.append(collision)
        self.assertEqual(plan["scope_metric_names"], sorted(entry["metrics"]))
        self.assertEqual(plan["drop_labels"], sorted(entry["drop_labels"]))
        self.assertEqual(plan["affected_metric_names"], sorted(affected))
        self.assertEqual((plan["observed_keys_before"], plan["projected_keys_after"]), (len(originals), len(groups)))
        self.assertEqual((plan["label_pairs_before"], plan["label_pairs_after"], plan["label_pairs_removed"]),
                         (before_pairs, after_pairs, before_pairs - after_pairs))
        self.assertEqual(plan["metric_counts"], {m: dict(before=len(s), after=len(after_metrics[m]))
                                               for m, s in before_metrics.items()})
        self.assertEqual(plan["collisions"], collisions)
        self.assertEqual(plan["collision_group_count"], len(collisions))
        self.assertEqual(plan["colliding_original_series"], sum(c["original_series_count"] for c in collisions))
        self.assertEqual(plan["collision_metric_names"], sorted({c["projected_key"]["metric"] for c in collisions}))
        self.assertEqual(plan["safe"], not collisions)
        self.assertEqual(plan["unsafe"], bool(collisions))


if __name__ == "__main__":
    unittest.main()
