from decimal import Inexact, InvalidOperation, ROUND_DOWN, ROUND_UP, getcontext, localcontext
import json
from pathlib import Path
import unittest

from otelwrench.promdiff.compare import Config, DEFAULT_POLICY, compare, validate_allowlist
from otelwrench.promdiff.parser import InputError, parse_snapshot


def report(before, after, config=None, allowlist=None):
    return compare(parse_snapshot(before), parse_snapshot(after), config, allowlist)


def expected(event):
    return {key: event[key] for key in ("kind", "family", "before", "after")}


def series(n, offset=0):
    return '# TYPE x gauge\n' + ''.join(f'x{{id="{i}"}} {i}\n' for i in range(offset, offset + n))


class CompareTests(unittest.TestCase):
    def test_classic_family_value_changes_do_not_break_contract(self):
        before = (Path(__file__).resolve().parents[2] / "examples/promdiff/classic.prom").read_text()
        after = before.replace('} 10\n', '} 999\n').replace('} 2\n', '} 3\n').replace('} 0.7\n', '} 1.7\n')
        result = report(before, after)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["candidate"], {"families": 3, "observed_series": 9})
        self.assertEqual(result["changes"], [])

    def test_bucket_boundary_change_is_series_churn(self):
        before = (Path(__file__).resolve().parents[2] / "examples/promdiff/classic.prom").read_text()
        after = before.replace(',le="0.5"', ',le="0.75"')
        result = report(before, after)
        self.assertEqual(result["status"], "warn")
        self.assertEqual([e["kind"] for e in result["changes"]], ["series_added", "series_missing"])
        self.assertEqual(result["observed_series"]["new"], 1)
        self.assertEqual(result["observed_series"]["missing"], 1)

    def test_removed_family_is_failure(self):
        result = report("x 1\ny 2\n", "x 3\n")
        self.assertEqual(result["status"], "fail")
        self.assertEqual([e["kind"] for e in result["changes"]], ["family_removed"])
        self.assertEqual(result["observed_series"]["missing"], 1)

    def test_missing_series_is_warning_not_removed_metric(self):
        result = report(series(3), series(2))
        self.assertEqual(result["status"], "warn")
        self.assertEqual([e["kind"] for e in result["changes"]], ["series_missing"])
        self.assertEqual(result["changes"][0]["before"], 1)
        self.assertTrue(result["observed_series"]["by_family"][0]["candidate_present"])

    def test_changed_type_is_failure_with_same_wire_counter_name(self):
        result = report('# TYPE x_total counter\nx_total 1\n', '# TYPE x_total gauge\nx_total 1\n')
        self.assertEqual([e["kind"] for e in result["changes"]], ["type_changed"])
        self.assertEqual(result["changes"][0]["before"], "counter")
        self.assertEqual(result["changes"][0]["after"], "gauge")

    def test_added_family_is_warning_below_growth_budget(self):
        result = report(series(10), series(10) + "y 1\n")
        self.assertEqual(result["status"], "warn")
        self.assertEqual([e["kind"] for e in result["changes"]], ["family_added"])
        self.assertEqual(result["observed_series"]["new"], 1)
        self.assertEqual(result["observed_series"]["net_growth_percent"], 10)

    def test_help_change_and_absence_are_visible(self):
        result = report('# HELP x Old\nx 1\n', '# HELP x New\nx 2\n')
        self.assertEqual(result["status"], "warn")
        self.assertEqual([e["kind"] for e in result["changes"]], ["help_changed"])
        self.assertEqual(result["changes"][0]["after"], "New")
        self.assertIsNone(report('x 1\n', '# HELP x\nx 1\n')["changes"][0]["before"])

    def test_label_schema_add_remove_rename(self):
        for before, after in [('x 1\n', 'x{a="1"} 1\n'),
                              ('x{a="1"} 1\n', 'x 1\n'),
                              ('x{a="1"} 1\n', 'x{b="1"} 1\n')]:
            with self.subTest(after=after):
                result = report(before, after)
                self.assertEqual(result["summary"]["failures"], 1)
                self.assertEqual(result["observed_series"]["new"], 1)
                self.assertEqual(result["observed_series"]["missing"], 1)

    def test_label_value_churn_and_timestamps_not_schema_changes(self):
        result = report('x{a="1"} 1 1000\n', 'x{a="2"} 9 2000\n')
        self.assertEqual(result["status"], "warn")
        self.assertEqual([e["kind"] for e in result["changes"]], ["series_added", "series_missing"])
        self.assertEqual(result["observed_series"]["net_growth_percent"], 0)
        self.assertEqual(report('x 1 1\n', 'x 9 999\n')["status"], "pass")

    def test_metadata_only_family_is_not_removed(self):
        result = report('# TYPE x gauge\nx 1\n', '# TYPE x gauge\n')
        self.assertNotIn("family_removed", [e["kind"] for e in result["changes"]])
        self.assertEqual(result["observed_series"]["missing"], 1)
        self.assertEqual(result["candidate"]["families"], 1)

    def test_policy_partial_override_can_warn_breakage(self):
        config = Config.from_dict({"policy": {"family_removed": "warn"}})
        result = report("x 1\n", "", config)
        self.assertEqual(result["status"], "warn")
        self.assertEqual(config.policy["type_changed"], "fail")
        self.assertEqual(DEFAULT_POLICY["family_removed"], "fail")

    def test_policy_can_fail_observed_series_churn(self):
        config = Config.from_dict({"policy": {"series_missing": "fail"}})
        self.assertEqual(report(series(2), series(1), config)["status"], "fail")

    def test_growth_exact_boundary_and_excess(self):
        self.assertEqual(report(series(10), series(12))["status"], "warn")
        excess = report(series(10), series(13))
        self.assertEqual(excess["status"], "fail")
        self.assertEqual(excess["observed_series"]["net_growth_percent"], 30)
        self.assertTrue(excess["observed_series"]["growth_limit_exceeded"])
        self.assertIn("series_growth", [e["kind"] for e in excess["changes"]])

    def test_fractional_growth_boundary_is_not_display_rounding(self):
        exact = report(series(1000), series(1001), Config.from_dict({"max_series_growth_percent": 0.1}))
        self.assertFalse(exact["observed_series"]["growth_limit_exceeded"])
        lower = report(series(1000), series(1001), Config.from_dict({"max_series_growth_percent": 0.09999999}))
        self.assertTrue(lower["observed_series"]["growth_limit_exceeded"])

    def test_growth_is_independent_of_caller_decimal_context(self):
        # Explicit oracles include both half-even display ties, a recurring
        # ratio, exact decimal budgets, churn/shrink and empty snapshots.
        cases = [(3, 4, 33, True, 33.333333),
                 (10, 12, 20, False, 20.0), (10, 13, 20, True, 30.0),
                 (1000, 1001, 0.1, False, 0.1),
                 (1000, 1001, 0.09999999999999999, True, 0.1),
                 (512, 513, 0.1953125, False, 0.195312),
                 (512, 515, 1, False, 0.585938),
                 (3, 3, 0, False, 0.0), (3, 2, 0, False, 0.0),
                 (0, 0, 0, False, 0.0), (0, 1, 1000000, True, None)]
        for before, after, limit, exceeds, percent in cases:
            left, right = parse_snapshot(series(before)), parse_snapshot(series(after))
            config = Config.from_dict({'max_series_growth_percent': limit})
            for trap in [True, False]:
                for rounding in [ROUND_DOWN, ROUND_UP]:
                    for flagged in [True, False]:
                        with self.subTest(before=before, after=after, limit=limit, trap=trap,
                                          rounding=rounding, flagged=flagged), localcontext() as ctx:
                            ctx.prec, ctx.rounding, ctx.Emin, ctx.Emax, ctx.clamp = 1, rounding, -1, 1, 1
                            for signal in ctx.traps:
                                ctx.traps[signal] = True
                            ctx.traps[InvalidOperation] = trap
                            ctx.clear_flags()
                            ctx.flags[Inexact] = flagged
                            original = str(ctx)
                            result = compare(left, right, config)
                            stats = result['observed_series']
                            self.assertEqual(stats['growth_limit_exceeded'], exceeds)
                            self.assertEqual(stats['net_growth_percent'], percent)
                            self.assertEqual(stats['zero_baseline_growth'], before == 0 and after > 0)
                            self.assertEqual('series_growth' in [e['kind'] for e in result['changes']], exceeds)
                            self.assertEqual(result['status'], 'fail' if exceeds else (
                                'pass' if before == after else 'warn'))
                            json.dumps(result, allow_nan=False)
                            self.assertIs(getcontext(), ctx)
                            self.assertEqual(str(ctx), original)

    def test_growth_policy_allowlist_and_raw_projection_preserve_caller_context(self):
        left = parse_snapshot(series(3).replace('x', 'old').replace('id=', 'old_key='))
        right = parse_snapshot(series(4))
        mapping = {'metrics': {'old': 'x'}, 'labels': {'old': {'old_key': 'id'}}}
        growth_event = {'kind': 'series_growth', 'family': None, 'before': 3, 'after': 4}
        for policy, allowlist, status in [({}, [], 'fail'),
                                          ({'series_growth': 'warn'}, [], 'warn'),
                                          ({}, [growth_event], 'warn')]:
            for trap in [True, False]:
                with self.subTest(policy=policy, allowlist=allowlist, trap=trap), localcontext() as ctx:
                    ctx.prec = 1
                    ctx.traps[InvalidOperation] = trap
                    ctx.clear_flags()
                    original = str(ctx)
                    result = compare(left, right, Config.from_dict({
                        'max_series_growth_percent': 33, 'policy': policy}), allowlist,
                        rename_map=mapping, include_raw_changes=True)
                    self.assertEqual(result['status'], status)
                    self.assertEqual(result['observed_series']['net_growth_percent'], 33.333333)
                    self.assertTrue(result['observed_series']['growth_limit_exceeded'])
                    self.assertEqual(result['observed_series']['new'], 1)
                    self.assertEqual(result['observed_series']['missing'], 0)
                    self.assertEqual(result['raw_changes'][0], growth_event)
                    self.assertEqual([e['kind'] for e in result['raw_changes']],
                                     ['series_growth', 'family_removed', 'family_added'])
                    self.assertEqual(result['changes'][0], dict(growth_event,
                        severity=policy.get('series_growth', 'fail'), allowed=bool(allowlist)))
                    json.dumps(result, allow_nan=False)
                    self.assertIs(getcontext(), ctx)
                    self.assertEqual(str(ctx), original)

    def test_zero_baseline_behavior(self):
        empty = report("", "")
        self.assertEqual(empty["status"], "pass")
        self.assertEqual(empty["observed_series"]["net_growth_percent"], 0)
        first = report("", "x 1\n", Config.from_dict({"max_series_growth_percent": 1000000}))
        self.assertEqual(first["status"], "fail")
        self.assertIsNone(first["observed_series"]["net_growth_percent"])
        self.assertTrue(first["observed_series"]["zero_baseline_growth"])
        json.dumps(first, allow_nan=False)

    def test_growth_is_net_growth_not_new_identity_count(self):
        result = report(series(10), series(10, 100))
        self.assertEqual(result["observed_series"]["new"], 10)
        self.assertEqual(result["observed_series"]["missing"], 10)
        self.assertFalse(result["observed_series"]["growth_limit_exceeded"])
        shrink = report(series(10), series(1))
        self.assertEqual(shrink["observed_series"]["net_growth_percent"], 0)

    def test_exact_allowlist_passes_and_preserves_evidence(self):
        before, after = 'x{a="1"} 1\n', 'x{a="1",r="eu"} 2\n'
        initial = report(before, after)
        allowlist = [expected(e) for e in initial["changes"]]
        allowed = report(before, after, allowlist=allowlist)
        self.assertEqual(allowed["status"], "pass")
        self.assertEqual(allowed["summary"], {"failures": 0, "warnings": 0, "allowed": 3})
        self.assertTrue(all(e["allowed"] for e in allowed["changes"]))
        self.assertEqual(allowlist, [expected(e) for e in initial["changes"]])

    def test_allowlist_only_suppresses_specific_change(self):
        before, after = 'x 1\ny 1\n', '# TYPE x counter\nx 1\n'
        initial = report(before, after)
        only_type = [expected(e) for e in initial["changes"] if e["kind"] == "type_changed"]
        allowed = report(before, after, allowlist=only_type)
        self.assertEqual(allowed["summary"]["failures"], 1)
        self.assertEqual(allowed["summary"]["allowed"], 1)

    def test_unused_allowlist_is_error(self):
        entry = {"kind": "type_changed", "family": "x", "before": "gauge", "after": "counter"}
        for before, after in [('x 1\n', 'x 2\n'), ('# TYPE x gauge\nx 1\n', '# TYPE x untyped\nx 1\n')]:
            with self.assertRaisesRegex(InputError, "unused"):
                report(before, after, allowlist=[entry])

    def test_allowlist_rejects_broad_partial_and_duplicate_entries(self):
        valid = {"kind": "family_removed", "family": "x", "before": {}, "after": None}
        for bad in [{}, ["x"], [{"kind": "family_removed", "family": "x"}],
                    [dict(valid, family="*")], [dict(valid, family="x?")],
                    [dict(valid, kind="*")], [dict(valid, kind=[])],
                    [dict(valid, family=None)], [dict(valid, extra=True)], [valid, valid]]:
            with self.subTest(bad=bad), self.assertRaises(InputError):
                validate_allowlist(bad)

    def test_config_rejects_unknown_fields_and_invalid_limits(self):
        for bad in [[], {"unknown": 2}, {"policy": []}, {"policy": {"unknown": "fail"}},
                    {"policy": {"family_removed": "ignore"}}, {"policy": {"type_changed": []}}]:
            with self.subTest(bad=bad), self.assertRaises(InputError):
                Config.from_dict(bad)
        for limit in [-1, float("nan"), float("inf"), 1000001, True, None, "20"]:
            with self.subTest(limit=limit), self.assertRaises(InputError):
                Config.from_dict({"max_series_growth_percent": limit})

    def test_report_deterministic_across_input_order(self):
        before = '# TYPE x gauge\nx{a="1",b="2"} 1\nx{a="3",b="4"} 2\ny 1\n'
        shuffled = 'y 9\n# TYPE x gauge\nx{b="4",a="3"} 8\nx{b="2",a="1"} 7\n'
        self.assertEqual(report(before, before), report(shuffled, shuffled))


if __name__ == "__main__":
    unittest.main()
