"""Strict text overlays replace exact budgets while keeping independent base policy."""
from copy import deepcopy
from decimal import Inexact, InvalidOperation, ROUND_DOWN, ROUND_UP, localcontext
import json
import math
import unittest

from otelwrench.promdiff.compare import Config, DEFAULT_POLICY, compare
from otelwrench.promdiff.parser import InputError, parse_snapshot


def wire(n, name='x', key='id'):
    return f'# TYPE {name} gauge\n' + ''.join(f'{name}{{{key}="{i}"}} 1\n' for i in range(n))


class ConfigJsonOverrideTests(unittest.TestCase):
    def test_concrete_replacement_and_independent_empty_successive_overlays(self):
        base = Config.from_json('{"max_series_growth_percent":20,"policy":{"family_removed":"warn"}}')
        text = '{"max_series_growth_percent":0.099999999999999999,"policy":{"series_growth":"warn"}}'
        original = base.to_json()
        derived = base.with_overrides_json(text)
        self.assertEqual(derived.policy, dict(DEFAULT_POLICY, family_removed='warn', series_growth='warn'))
        report = compare(parse_snapshot(wire(1000)), parse_snapshot(wire(1001)), derived, include_raw_changes=True)
        self.assertEqual(report['status'], 'warn')
        self.assertTrue(report['observed_series']['growth_limit_exceeded'])
        self.assertEqual(report['observed_series']['max_growth_percent_exact'], '0.099999999999999999')
        self.assertEqual([e['kind'] for e in report['changes']], ['series_growth', 'series_added'])
        self.assertEqual(base.to_json(), original)
        self.assertEqual(base.max_series_growth_percent, 20)
        self.assertEqual(base.policy['series_growth'], 'fail')
        for empty in ['{}', '{"policy":{}}']:
            copy = derived.with_overrides_json(empty)
            self.assertIsNot(copy, derived)
            self.assertIsNot(copy.policy, derived.policy)
            self.assertEqual(copy.to_json(), derived.to_json())
        successor = derived.with_overrides_json('{"policy":{"series_growth":"fail"}}')
        successor.policy['family_removed'] = 'fail'
        base.policy['family_added'] = 'fail'
        self.assertEqual(derived.policy, dict(DEFAULT_POLICY, family_removed='warn', series_growth='warn'))
        self.assertEqual(DEFAULT_POLICY['family_added'], 'warn')
        partial = Config(policy={'family_removed': 'warn'}, max_series_growth_percent=33)
        normalized = partial.with_overrides_json('{"policy":{"series_growth":"warn"}}')
        self.assertEqual(normalized.policy, derived.policy)
        self.assertEqual(normalized.max_series_growth_percent, 33)
        self.assertEqual(partial.policy, {'family_removed': 'warn'})
        with self.assertRaises(InputError):
            base.with_overrides(text)  # Existing dictionary API still rejects text.
        rounded = base.with_overrides(json.loads(text))
        self.assertFalse(compare(parse_snapshot(wire(1000)), parse_snapshot(wire(1001)), rounded)
                         ['observed_series']['growth_limit_exceeded'])

    def test_replaced_and_retained_exact_limits_round_trip_without_decimal_context_changes(self):
        left, right = parse_snapshot(wire(1000)), parse_snapshot(wire(1001))
        cases = [('0.099999999999999999', True, True), ('9.9999999999999999e-2', True, True),
                 ('0.100000000000000001', False, True), ('1e-9999', True, True),
                 ('1e-99999999999999999999', True, True), ('999999.99999999999', False, True),
                 ('0.1', False, False), ('1e-1', False, False),
                 ('0.1' + '0' * 5000, False, False), ('0', True, False), ('-0.0', True, False),
                 ('-0e-99999999999999999999', True, False), ('20.0', False, False),
                 ('1000000', False, False), ('1e6', False, False)]
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
                        saved_context = str(ctx)
                        base = Config.from_json('{"max_series_growth_percent":0.099999999999999998,'
                                                '"policy":{"family_removed":"warn"}}')
                        saved_base = base.to_json()
                        derived = base.with_overrides_json('{"max_series_growth_percent":' + token + '}')
                        # Policy-only and empty text overlays retain the replacement exactly.
                        derived = derived.with_overrides_json('{"policy":{"series_growth":"warn"}}')
                        derived = derived.with_overrides_json('{}').with_overrides_json('{"policy":{}}')
                        report = compare(left, right, derived, include_raw_changes=True)
                        stats = report['observed_series']
                        self.assertEqual(report['status'], 'warn')
                        self.assertEqual(stats['growth_limit_exceeded'], exceeds)
                        self.assertEqual('max_growth_percent_exact' in stats, evidence)
                        if evidence:
                            self.assertEqual(stats['max_growth_percent_exact'], token)
                        exported = derived.to_json()
                        if evidence:
                            self.assertIn('"max_series_growth_percent":' + token + ',', exported)
                        restored = Config.from_json(exported)
                        self.assertEqual(restored.to_json(), exported)
                        self.assertEqual(compare(left, right, restored, include_raw_changes=True), report)
                        if float(token) == 0:
                            self.assertEqual(math.copysign(1, json.loads(exported)['max_series_growth_percent']),
                                             math.copysign(1, float(token)))
                        json.dumps(report, allow_nan=False)
                        self.assertEqual(base.to_json(), saved_base)
                        self.assertEqual(str(ctx), saved_context)

    def test_invalid_text_schema_and_exact_ranges_reuse_loader_validation(self):
        invalid = [None, b'{}', {}, 20, '', '{', '{} {}', '\ufeff{}', '[]', 'null', 'true', '1', '"config"',
                   '{"policy":{},"policy":{}}',
                   '{"policy":{"series_growth":"fail","series_growth":"warn"}}',
                   '{"unknown":{"x":1,"x":2}}', '{"unknown":1}', '{"policy":null}', '{"policy":[]}',
                   '{"policy":{"unknown":"warn"}}', '{"policy":{"series_growth":[]}}',
                   '{"policy":{"series_growth":"ignore"}}', '{"policy":{"series_growth":true}}',
                   '[' * 10000 + '0' + ']' * 10000]
        invalid += ['{"max_series_growth_percent":' + value + '}' for value in
                    ['NaN', 'Infinity', '-Infinity', '1e999', '-1e999', 'true', 'null', '"0.1"', '[]', '{}',
                     '-1', '1000001', '-1e-9999', '-1e-99999999999999999999',
                     '1000000.00000000001', '1.00000000000000001e6', '1' * 5000]]
        for text in invalid:
            for trap in [True, False]:
                with self.subTest(text=text, trap=trap), localcontext() as ctx:
                    ctx.prec, ctx.Emin, ctx.Emax = 1, -1, 1
                    for signal in ctx.traps:
                        ctx.traps[signal] = True
                    ctx.traps[InvalidOperation] = trap
                    ctx.clear_flags()
                    ctx.flags[Inexact] = True
                    saved_context = str(ctx)
                    base = Config.from_json('{"max_series_growth_percent":0.099999999999999999}')
                    saved_base = base.to_json()
                    saved_input = repr(text)
                    with self.assertRaises(InputError) as loader:
                        Config.from_json(text, source='overlay.json')
                    with self.assertRaises(InputError) as overlay:
                        base.with_overrides_json(text, source='overlay.json')
                    self.assertEqual(str(overlay.exception), str(loader.exception))
                    self.assertEqual(base.to_json(), saved_base)
                    self.assertEqual(repr(text), saved_input)
                    self.assertEqual(str(ctx), saved_context)

    def test_decode_diagnostics_label_default_or_explicit_source(self):
        for source in ['config overrides', 'overlay.json']:
            for text, message in [(None, 'JSON text must be a string'), ('{', 'invalid JSON:'),
                                  ('{"policy":{},"policy":{}}', 'duplicate JSON key'),
                                  ('{"max_series_growth_percent":NaN}', 'non-finite JSON constant'),
                                  ('{"max_series_growth_percent":1e999}', 'overflows finite float range'),
                                  ('{"max_series_growth_percent":-1e-9999}', 'within 0..1000000'),
                                  ('[' * 10000 + '0' + ']' * 10000, 'invalid JSON:')]:
                with self.subTest(source=source, text=text):
                    kwargs = {} if source == 'config overrides' else {'source': source}
                    with self.assertRaises(InputError) as error:
                        Config().with_overrides_json(text, **kwargs)
                    self.assertTrue(str(error.exception).startswith(source + ': '))
                    self.assertIn(message, str(error.exception))

    def test_invalid_base_cannot_be_repaired_decode_precedes_existing_base_validation(self):
        for fields in [{'policy': None}, {'policy': {'unknown': 'warn'}},
                       {'policy': {'series_growth': 'ignore'}}, {'max_series_growth_percent': True},
                       {'max_series_growth_percent': -1}]:
            for mutated in [True, False]:
                with self.subTest(fields=fields, mutated=mutated):
                    base = Config() if mutated else Config(**deepcopy(fields))
                    if mutated:
                        for key, value in deepcopy(fields).items():
                            setattr(base, key, value)
                    saved = deepcopy(base)
                    data = {'policy': dict(DEFAULT_POLICY), 'max_series_growth_percent': 20}
                    with self.assertRaises(InputError) as dictionary:
                        base.with_overrides(data)
                    with self.assertRaises(InputError) as overlay:
                        base.with_overrides_json(json.dumps(data))
                    self.assertEqual(str(overlay.exception), str(dictionary.exception))
                    # Valid JSON with invalid schema still validates the base first.
                    with self.assertRaises(InputError) as invalid_schema:
                        base.with_overrides_json('[]')
                    self.assertEqual(str(invalid_schema.exception), str(dictionary.exception))
                    with self.assertRaises(InputError) as decoding:
                        base.with_overrides_json('{', source='overlay.json')
                    self.assertTrue(str(decoding.exception).startswith('overlay.json: invalid JSON:'))
                    self.assertEqual(base, saved)

    def test_projection_allowlists_raw_and_zero_baseline_keep_derived_semantics(self):
        left, right = parse_snapshot(wire(1000, 'old', 'old_key')), parse_snapshot(wire(1001))
        mapping = {'metrics': {'old': 'x'}, 'labels': {'old': {'old_key': 'id'}}}
        base = Config.from_json('{"policy":{"family_removed":"warn"}}')
        derived = base.with_overrides_json('{"max_series_growth_percent":0.099999999999999999,'
                                          '"policy":{"series_growth":"warn"}}')
        equivalent = Config.from_json(derived.to_json())
        growth = {'kind': 'series_growth', 'family': None, 'before': 1000, 'after': 1001}
        for allowed in [[], [growth]]:
            report = compare(left, right, derived, allowed, rename_map=mapping, include_raw_changes=True)
            self.assertEqual(report, compare(left, right, equivalent, allowed,
                                            rename_map=mapping, include_raw_changes=True))
            self.assertEqual(report['status'], 'warn')
            self.assertEqual(report['changes'][0], dict(growth, severity='warn', allowed=bool(allowed)))
            self.assertEqual([e['kind'] for e in report['raw_changes']],
                             ['series_growth', 'family_removed', 'family_added'])
        rounded = derived.with_overrides_json('{"max_series_growth_percent":0.1}')
        with self.assertRaisesRegex(InputError, 'unused allowlist'):
            compare(left, right, rounded, [growth], rename_map=mapping)
        for before, after, exceeds in [('', '', False), ('', 'x 1\n', True), ('x 1\n', '', False),
                                      ('x{id="a"} 1\n', 'x{id="b"} 1\n', False)]:
            underflow = derived.with_overrides_json('{"max_series_growth_percent":1e-99999999999999999999}')
            report = compare(parse_snapshot(before), parse_snapshot(after), underflow, include_raw_changes=True)
            self.assertEqual(report['observed_series']['growth_limit_exceeded'], exceeds)
            self.assertEqual('series_growth' in [e['kind'] for e in report['raw_changes']], exceeds)


if __name__ == '__main__':
    unittest.main()
