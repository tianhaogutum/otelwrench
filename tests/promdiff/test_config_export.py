"""Deterministic public config export, numeric intent and failure contracts."""
from decimal import Decimal, Inexact, InvalidOperation, ROUND_DOWN, ROUND_UP, localcontext
import json
import math
import unittest

from otelwrench.promdiff.compare import Config, DEFAULT_POLICY, compare
from otelwrench.promdiff.parser import InputError, parse_snapshot


def wire(n, name='x', key='id'):
    return f'# TYPE {name} gauge\n' + ''.join(f'{name}{{{key}="{i}"}} 1\n' for i in range(n))


class ConfigExportTests(unittest.TestCase):
    def test_deterministic_full_effective_policy_without_mutation(self):
        expected = ('{"max_series_growth_percent":20,"policy":{'
                    '"family_added":"warn","family_removed":"fail",'
                    '"help_changed":"warn","label_schema_changed":"fail",'
                    '"series_added":"warn","series_growth":"fail",'
                    '"series_missing":"warn","type_changed":"fail"}}')
        self.assertEqual(Config().to_json(), expected)
        self.assertEqual(Config.from_json('{}').to_json(), expected)
        partial = {'series_growth': 'warn', 'family_removed': 'warn'}
        config = Config(policy=partial)
        exported = config.to_json()
        self.assertIs(config.policy, partial)
        self.assertEqual(partial, {'series_growth': 'warn', 'family_removed': 'warn'})
        full = dict(DEFAULT_POLICY, **partial)
        self.assertEqual(json.loads(exported), {'max_series_growth_percent': 20, 'policy': full})
        self.assertEqual(Config(policy=dict(reversed(list(full.items())))).to_json(), exported)
        self.assertEqual(config.to_json(), exported)
        self.assertEqual(Config.from_json(exported).to_json(), exported)

    def test_exact_round_trip_reports_and_numeric_tokens_preserve_decimal_context(self):
        left, right = parse_snapshot(wire(1000)), parse_snapshot(wire(1001))
        cases = [
            ('0.099999999999999999', True, True),
            ('9.9999999999999999e-2', True, True),
            ('0.100000000000000001', False, True),
            ('0.1', False, False), ('1e-1', False, False),
            ('0.1' + '0' * 5000, False, False),
            ('1e-99999999999999999999', True, True),
            ('1e-9999', True, True), ('5e-324', True, False),
            ('-0e-99999999999999999999', True, False),
            ('-0.0', True, False), ('0', True, False), ('0.0', True, False),
            ('20', False, False), ('20.0', False, False),
            ('1000000', False, False), ('1e6', False, False),
            ('999999.99999999999', False, True),
        ]
        for token, exceeds, evidence in cases:
            for trap in [True, False]:
                for rounding in [ROUND_DOWN, ROUND_UP]:
                    with self.subTest(token=token, trap=trap, rounding=rounding), localcontext() as ctx:
                        ctx.prec, ctx.Emin, ctx.Emax, ctx.rounding = 1, -1, 1, rounding
                        for signal in ctx.traps:
                            ctx.traps[signal] = True
                        ctx.traps[InvalidOperation] = trap
                        ctx.clear_flags()
                        ctx.flags[Inexact] = True
                        original = str(ctx)
                        config = Config.from_json('{"max_series_growth_percent":' + token + '}')
                        exported = config.to_json()
                        self.assertIsInstance(exported, str)
                        numeric = json.loads(exported)['max_series_growth_percent']
                        self.assertIn(type(numeric), (int, float))
                        restored = Config.from_json(exported)
                        self.assertEqual(restored.to_json(), exported)
                        report = compare(left, right, restored, include_raw_changes=True)
                        self.assertEqual(report, compare(left, right, config, include_raw_changes=True))
                        stats = report['observed_series']
                        self.assertEqual(stats['growth_limit_exceeded'], exceeds)
                        if evidence:
                            self.assertIn('"max_series_growth_percent":' + token + ',', exported)
                            self.assertEqual(stats['max_growth_percent_exact'], token)
                        else:
                            self.assertNotIn('max_growth_percent_exact', stats)
                        if token.startswith('-0'):
                            self.assertEqual(math.copysign(1, numeric), -1)
                        json.dumps(report, allow_nan=False)
                        self.assertEqual(str(ctx), original)

    def test_python_numeric_semantics_and_zero_baseline_round_trip(self):
        for limit in [0, -0.0, 0.1, 20, 20.0, 1e-300, 1000000]:
            config = Config.from_dict({'max_series_growth_percent': limit})
            exported = config.to_json()
            numeric = json.loads(exported)['max_series_growth_percent']
            self.assertIs(type(numeric), type(limit))
            self.assertEqual(numeric, limit)
            restored = Config.from_json(exported)
            for before, after in [('', ''), ('', 'x 1\n'), ('x 1\n', ''),
                                  ('x{id="a"} 1\n', 'x{id="b"} 1\n')]:
                with self.subTest(limit=limit, before=before, after=after):
                    left, right = parse_snapshot(before), parse_snapshot(after)
                    self.assertEqual(compare(left, right, restored, include_raw_changes=True),
                                     compare(left, right, config, include_raw_changes=True))

    def test_round_trip_preserves_projection_policy_allowlist_and_raw_evidence(self):
        left, right = parse_snapshot(wire(1000, 'old', 'old_key')), parse_snapshot(wire(1001))
        mapping = {'metrics': {'old': 'x'}, 'labels': {'old': {'old_key': 'id'}}}
        growth = {'kind': 'series_growth', 'family': None, 'before': 1000, 'after': 1001}
        for policy, allowed, status in [({}, [], 'fail'),
                ({'series_growth': 'warn'}, [], 'warn'), ({}, [growth], 'warn')]:
            config = Config.from_json('{"max_series_growth_percent":0.099999999999999999,'
                                      + '"policy":' + json.dumps(policy) + '}')
            restored = Config.from_json(config.to_json())
            result = compare(left, right, restored, allowed, rename_map=mapping, include_raw_changes=True)
            self.assertEqual(result, compare(left, right, config, allowed, rename_map=mapping,
                                             include_raw_changes=True))
            self.assertEqual(result['status'], status)
            self.assertEqual([e['kind'] for e in result['changes']], ['series_growth', 'series_added'])
            self.assertEqual([e['kind'] for e in result['raw_changes']],
                             ['series_growth', 'family_removed', 'family_added'])
        restored = Config.from_json(Config.from_json('{"max_series_growth_percent":0.1}').to_json())
        with self.assertRaisesRegex(InputError, 'unused allowlist'):
            compare(left, right, restored, [growth], rename_map=mapping)

    def test_invalid_constructed_and_mutated_config_reuses_validation_without_context_changes(self):
        invalid = [{'policy': value} for value in [None, [], 'warn',
                   {'unknown': 'warn'}, {'series_growth': 'ignore'}, {'series_growth': []}]]
        invalid += [{'max_series_growth_percent': value} for value in
                    [None, True, '0.1', [], {}, Decimal('0.1'), -1, 1000001,
                     float('nan'), float('inf'), -float('inf')]]
        for fields in invalid:
            for mutated in [True, False]:
                for trap in [True, False]:
                    with self.subTest(fields=fields, mutated=mutated, trap=trap), localcontext() as ctx:
                        ctx.prec, ctx.Emin, ctx.Emax = 1, -1, 1
                        for signal in ctx.traps:
                            ctx.traps[signal] = True
                        ctx.traps[InvalidOperation] = trap
                        ctx.clear_flags()
                        ctx.flags[Inexact] = True
                        original = str(ctx)
                        config = Config() if mutated else Config(**fields)
                        if mutated:
                            for key, value in fields.items():
                                setattr(config, key, value)
                        with self.assertRaises(InputError) as validation:
                            Config.from_dict({'policy': config.policy,
                                              'max_series_growth_percent': config.max_series_growth_percent})
                        with self.assertRaises(InputError) as export:
                            config.to_json()
                        self.assertEqual(str(export.exception), str(validation.exception))
                        self.assertEqual(str(ctx), original)


if __name__ == '__main__':
    unittest.main()
