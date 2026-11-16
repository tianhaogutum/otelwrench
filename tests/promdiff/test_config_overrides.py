"""Independent validated overlays retain exact budget decisions and evidence."""
from copy import deepcopy
from decimal import Decimal, Inexact, InvalidOperation, ROUND_DOWN, ROUND_UP, localcontext
import json
import math
import unittest

from otelwrench.promdiff.compare import Config, DEFAULT_POLICY, compare
from otelwrench.promdiff.parser import InputError, parse_snapshot


def wire(n, name='x', key='id'):
    return f'# TYPE {name} gauge\n' + ''.join(f'{name}{{{key}="{i}"}} 1\n' for i in range(n))


class ConfigOverrideTests(unittest.TestCase):
    def test_independent_policy_overlay_defaults_empty_and_successive_overrides(self):
        policy = {'family_removed': 'warn'}
        config = Config(policy=policy, max_series_growth_percent=33)
        overrides = {'policy': {'series_growth': 'warn'}}
        derived = config.with_overrides(overrides)
        self.assertEqual(config.policy, {'family_removed': 'warn'})
        self.assertIs(config.policy, policy)
        self.assertEqual(derived.policy, dict(DEFAULT_POLICY, family_removed='warn', series_growth='warn'))
        self.assertEqual(derived.max_series_growth_percent, 33)
        for data in [{}, {'policy': {}}]:
            duplicate = derived.with_overrides(data)
            self.assertEqual(duplicate, derived)
            self.assertIsNot(duplicate, derived)
            self.assertIsNot(duplicate.policy, derived.policy)
            self.assertEqual(data, {} if not data else {'policy': {}})
        successor = derived.with_overrides({'policy': {'series_growth': 'fail', 'type_changed': 'warn'}})
        self.assertEqual(successor.policy, dict(DEFAULT_POLICY, family_removed='warn', type_changed='warn'))
        successor.policy['family_added'] = 'fail'
        overrides['policy']['series_growth'] = 'fail'
        policy['family_removed'] = 'fail'
        self.assertEqual(derived.policy, dict(DEFAULT_POLICY, family_removed='warn', series_growth='warn'))
        derived.policy['help_changed'] = 'fail'
        self.assertEqual(config.policy, {'family_removed': 'fail'})
        self.assertEqual(overrides, {'policy': {'series_growth': 'fail'}})
        self.assertEqual(DEFAULT_POLICY['help_changed'], 'warn')

    def test_retained_exact_limits_and_stable_round_trips_preserve_decimal_context(self):
        left, right = parse_snapshot(wire(1000)), parse_snapshot(wire(1001))
        cases = [('0.099999999999999999', True, True),
                 ('9.9999999999999999e-2', True, True),
                 ('0.100000000000000001', False, True),
                 ('1e-99999999999999999999', True, True), ('1e-9999', True, True),
                 ('999999.99999999999', False, True),
                 ('0.1', False, False), ('1e-1', False, False),
                 ('-0e-99999999999999999999', True, False),
                 ('0', True, False), ('20.0', False, False), ('1000000', False, False)]
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
                        context_before = str(ctx)
                        config = Config.from_json('{"max_series_growth_percent":' + token +
                                                  ',"policy":{"family_removed":"warn"}}')
                        original_json = config.to_json()
                        derived = config.with_overrides({'policy': {'series_growth': 'warn'}})
                        derived = derived.with_overrides({}).with_overrides({'policy': {}})
                        exported = derived.to_json()
                        report = compare(left, right, derived, include_raw_changes=True)
                        self.assertEqual(report['status'], 'warn')
                        self.assertEqual(report['observed_series']['growth_limit_exceeded'], exceeds)
                        self.assertEqual('max_growth_percent_exact' in report['observed_series'], evidence)
                        if evidence:
                            self.assertEqual(report['observed_series']['max_growth_percent_exact'], token)
                            self.assertIn(':', exported)
                            self.assertIn('"max_series_growth_percent":' + token + ',', exported)
                        restored = Config.from_json(exported)
                        self.assertEqual(restored.to_json(), exported)
                        self.assertEqual(compare(left, right, restored, include_raw_changes=True), report)
                        self.assertEqual(config.to_json(), original_json)
                        self.assertEqual(config.policy['series_growth'], 'fail')
                        json.dumps(report, allow_nan=False)
                        self.assertEqual(str(ctx), context_before)

    def test_limit_replacement_uses_from_dict_numeric_semantics_and_discards_old_evidence(self):
        config = Config.from_json('{"max_series_growth_percent":0.099999999999999999,'
                                  '"policy":{"family_removed":"warn"}}')
        left, right = parse_snapshot(wire(1000)), parse_snapshot(wire(1001))
        for limit in [0, -0.0, 0.0, 0.1, 1e-300, 20, 20.0, 1000000, 1000000.0]:
            with self.subTest(limit=limit):
                data = {'max_series_growth_percent': limit}
                derived = config.with_overrides(data)
                expected = Config.from_dict(dict(data, policy={'family_removed': 'warn'}))
                self.assertEqual(derived.to_json(), expected.to_json())
                self.assertIs(type(derived.max_series_growth_percent), type(limit))
                self.assertEqual(compare(left, right, derived, include_raw_changes=True),
                                 compare(left, right, expected, include_raw_changes=True))
                numeric = json.loads(derived.to_json())['max_series_growth_percent']
                self.assertEqual(math.copysign(1, numeric), math.copysign(1, limit))
                self.assertNotIn('max_growth_percent_exact', compare(left, right, derived)['observed_series'])
        self.assertIn('0.099999999999999999', config.to_json())

    def test_projection_allowlists_and_raw_changes_keep_exact_budget_and_policy(self):
        left, right = parse_snapshot(wire(1000, 'old', 'old_key')), parse_snapshot(wire(1001))
        mapping = {'metrics': {'old': 'x'}, 'labels': {'old': {'old_key': 'id'}}}
        config = Config.from_json('{"max_series_growth_percent":0.099999999999999999,'
                                  '"policy":{"family_removed":"warn"}}')
        derived = config.with_overrides({'policy': {'series_growth': 'warn'}})
        growth = {'kind': 'series_growth', 'family': None, 'before': 1000, 'after': 1001}
        original = compare(left, right, config, rename_map=mapping, include_raw_changes=True)
        self.assertEqual(original['status'], 'fail')
        for allowed in [[], [growth]]:
            report = compare(left, right, derived, allowed, rename_map=mapping, include_raw_changes=True)
            self.assertEqual(report['status'], 'warn')
            self.assertEqual(report['changes'][0], dict(growth, severity='warn', allowed=bool(allowed)))
            self.assertEqual(report['raw_changes'], original['raw_changes'])
            self.assertEqual(report['observed_series'], original['observed_series'])
            self.assertEqual(report['contracts'], original['contracts'])
            self.assertEqual(report['declared_renames'], mapping)
            self.assertEqual([e['kind'] for e in report['changes']], ['series_growth', 'series_added'])
            self.assertEqual([e['kind'] for e in report['raw_changes']],
                             ['series_growth', 'family_removed', 'family_added'])
        replacement = derived.with_overrides({'max_series_growth_percent': 0.1})
        with self.assertRaisesRegex(InputError, 'unused allowlist'):
            compare(left, right, replacement, [growth], rename_map=mapping)
        for before, after in [('', ''), ('', 'x 1\n'), ('x 1\n', ''),
                              ('x{id="a"} 1\n', 'x{id="b"} 1\n')]:
            self.assertEqual(compare(parse_snapshot(before), parse_snapshot(after), derived)['observed_series'],
                             compare(parse_snapshot(before), parse_snapshot(after), config)['observed_series'])

    def test_invalid_overrides_reuse_validation_and_leave_inputs_and_context_unchanged(self):
        class IntSubclass(int):
            pass

        invalid = [None, [], 'config', 20, {'unknown': 1}]
        invalid += [{'policy': value} for value in [None, [], 'warn',
                    {'unknown': 'warn'}, {1: 'warn'}, {'series_growth': 'ignore'},
                    {'series_growth': None}, {'series_growth': []}]]
        invalid += [{'max_series_growth_percent': value} for value in
                    [None, True, '0.1', [], {}, Decimal('0.1'), IntSubclass(1),
                     -1, 1000001, float('nan'), float('inf'), -float('inf')]]
        for data in invalid:
            for trap in [True, False]:
                with self.subTest(data=data, trap=trap), localcontext() as ctx:
                    ctx.prec, ctx.Emin, ctx.Emax = 1, -1, 1
                    for signal in ctx.traps:
                        ctx.traps[signal] = True
                    ctx.traps[InvalidOperation] = trap
                    ctx.clear_flags()
                    ctx.flags[Inexact] = True
                    context_before = str(ctx)
                    config = Config.from_json('{"max_series_growth_percent":0.099999999999999999}')
                    config_before = config.to_json()
                    data_before = repr(data)
                    with self.assertRaises(InputError) as validation:
                        Config.from_dict(data)
                    with self.assertRaises(InputError) as overlay:
                        config.with_overrides(data)
                    self.assertEqual(str(overlay.exception), str(validation.exception))
                    self.assertEqual(config.to_json(), config_before)
                    self.assertEqual(repr(data), data_before)
                    self.assertEqual(str(ctx), context_before)

    def test_invalid_base_cannot_be_repaired_and_is_not_mutated(self):
        for fields in [{'policy': None}, {'policy': {'unknown': 'warn'}},
                       {'policy': {'series_growth': 'ignore'}},
                       {'max_series_growth_percent': True}, {'max_series_growth_percent': -1}]:
            for mutated in [True, False]:
                config = Config() if mutated else Config(**deepcopy(fields))
                if mutated:
                    for key, value in deepcopy(fields).items():
                        setattr(config, key, value)
                base_before = deepcopy(config)
                data = {'policy': {'series_growth': 'warn'}, 'max_series_growth_percent': 20}
                with self.assertRaises(InputError) as validation:
                    Config.from_dict({'policy': config.policy,
                                      'max_series_growth_percent': config.max_series_growth_percent})
                with self.assertRaises(InputError) as overlay:
                    config.with_overrides(data)
                self.assertEqual(str(overlay.exception), str(validation.exception))
                self.assertEqual(config, base_before)
                self.assertEqual(data, {'policy': {'series_growth': 'warn'}, 'max_series_growth_percent': 20})


if __name__ == '__main__':
    unittest.main()
