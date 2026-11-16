"""Public text-input config contract, shared with the CLI's exact budgets."""
from decimal import Inexact, InvalidOperation, ROUND_DOWN, ROUND_UP, localcontext
import json
import unittest

from otelwrench.promdiff.compare import Config, DEFAULT_POLICY, compare
from otelwrench.promdiff.parser import InputError, parse_snapshot


def wire(n, name="x", key="id"):
    return f'# TYPE {name} gauge\n' + ''.join(
        f'{name}{{{key}="{i}"}} 1\n' for i in range(n))


class ConfigJsonTests(unittest.TestCase):
    def test_exact_json_budget_and_finite_report_ignore_caller_decimal_context(self):
        left, right = parse_snapshot(wire(1000)), parse_snapshot(wire(1001))
        cases = [
            ('0.099999999999999999', True, True),
            ('9.9999999999999999e-2', True, True),
            ('0.1', False, False), ('1e-1', False, False),
            ('0.1' + '0' * 5000, False, False),
            ('0.100000000000000001', False, True),
            ('1e-99999999999999999999', True, True),
            ('-0e-99999999999999999999', True, False),
            ('0', True, False), ('1000000', False, False),
            ('1e6', False, False), ('999999.99999999999', False, True),
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
                        result = compare(left, right, config, include_raw_changes=True)
                        self.assertEqual(str(ctx), original)
                        self.assertEqual(result['status'], 'fail' if exceeds else 'warn')
                        stats = result['observed_series']
                        self.assertEqual(stats['net_growth_percent'], 0.1)
                        self.assertEqual(stats['max_growth_percent'], float(token))
                        self.assertEqual(stats['growth_limit_exceeded'], exceeds)
                        if evidence:
                            self.assertEqual(stats['max_growth_percent_exact'], token)
                        else:
                            self.assertNotIn('max_growth_percent_exact', stats)
                        kinds = ['series_growth', 'series_added'] if exceeds else ['series_added']
                        self.assertEqual([e['kind'] for e in result['changes']], kinds)
                        self.assertEqual([e['kind'] for e in result['raw_changes']], kinds)
                        json.dumps(result, allow_nan=False)

    def test_invalid_json_and_config_raise_input_error_without_context_changes(self):
        invalid = [
            None, b'{}', {}, 20, '', '{', '{} {}', '\ufeff{}',
            '[]', 'null', 'true', '1', '"config"',
            '{"policy":{},"policy":{}}',
            '{"policy":{"series_growth":"fail","series_growth":"warn"}}',
            '{"unknown":{"x":1,"x":2}}',
            '{"unknown":1}', '{"policy":null}', '{"policy":[]}',
            '{"policy":{"unknown":"warn"}}', '{"policy":{"series_growth":[]}}',
            '{"policy":{"series_growth":"ignore"}}',
            '[' * 2000 + '0' + ']' * 2000,
        ]
        for value in ['NaN', 'Infinity', '-Infinity', '1e999', '-1e999',
                      'true', 'null', '"0.1"', '[]', '{}', '-1', '1000001',
                      '-1e-9999', '-1e-99999999999999999999',
                      '1000000.00000000001', '1.00000000000000001e6']:
            invalid.append('{"max_series_growth_percent":' + value + '}')
        for text in invalid:
            for trap in [True, False]:
                with self.subTest(text=text, trap=trap), localcontext() as ctx:
                    ctx.prec, ctx.Emin, ctx.Emax = 1, -1, 1
                    for signal in ctx.traps:
                        ctx.traps[signal] = True
                    ctx.traps[InvalidOperation] = trap
                    ctx.clear_flags()
                    ctx.flags[Inexact] = True
                    original = str(ctx)
                    with self.assertRaises(InputError):
                        Config.from_json(text)
                    self.assertEqual(str(ctx), original)

    def test_decode_diagnostics_have_default_or_explicit_source(self):
        for source in ['config', 'reviewed-policy.json']:
            for text, message in [('{', 'invalid JSON:'),
                                  ('{"policy":{},"policy":{}}', "duplicate JSON key 'policy'"),
                                  ('{"max_series_growth_percent":NaN}', 'non-finite JSON constant NaN'),
                                  ('{"max_series_growth_percent":1e999}', 'JSON number overflows finite float range')]:
                with self.subTest(source=source, text=text):
                    kwargs = {} if source == 'config' else {'source': source}
                    with self.assertRaises(InputError) as error:
                        Config.from_json(text, **kwargs)
                    # Hook failures retain the CLI's existing invalid-JSON wrapper.
                    prefix = source + ': ' if text == '{' else source + ': invalid JSON: ' + source + ': '
                    self.assertTrue(str(error.exception).startswith(prefix + message))

    def test_defaults_policy_and_existing_int_float_semantics(self):
        self.assertEqual(Config.from_json('{}'), Config())
        first = Config.from_json('{"policy":{"series_growth":"warn"}}')
        self.assertEqual(first.policy, dict(DEFAULT_POLICY, series_growth='warn'))
        first.policy['family_removed'] = 'warn'
        self.assertEqual(Config.from_json('{}').policy, DEFAULT_POLICY)
        left, right = parse_snapshot(wire(1000)), parse_snapshot(wire(1001))
        for limit in [0, -0.0, 0.1, 20, 1000000]:
            with self.subTest(limit=limit):
                from_text = Config.from_json(json.dumps({'max_series_growth_percent': limit}))
                self.assertEqual(compare(left, right, from_text), compare(
                    left, right, Config.from_dict({'max_series_growth_percent': limit})))
        # Already decoded Python floats keep their documented decimal-string meaning.
        rounded = Config.from_dict(json.loads('{"max_series_growth_percent":0.099999999999999999}'))
        self.assertFalse(compare(left, right, rounded)['observed_series']['growth_limit_exceeded'])

    def test_exact_config_preserves_projection_policy_allowlists_and_raw_evidence(self):
        left = parse_snapshot(wire(1000, 'old', 'old_key'))
        right = parse_snapshot(wire(1001))
        mapping = {'metrics': {'old': 'x'}, 'labels': {'old': {'old_key': 'id'}}}
        growth = {'kind': 'series_growth', 'family': None, 'before': 1000, 'after': 1001}
        for policy, allowed, status in [({}, [], 'fail'),
                ({'series_growth': 'warn'}, [], 'warn'), ({}, [growth], 'warn')]:
            with self.subTest(policy=policy, allowed=allowed):
                config = Config.from_json('{"max_series_growth_percent":0.099999999999999999,'
                                          + '"policy":' + json.dumps(policy) + '}')
                result = compare(left, right, config, allowed, rename_map=mapping, include_raw_changes=True)
                self.assertEqual(result['status'], status)
                self.assertEqual(result['observed_series']['new'], 1)
                self.assertEqual(result['observed_series']['missing'], 0)
                self.assertEqual(result['changes'][0], dict(growth,
                    severity=policy.get('series_growth', 'fail'), allowed=bool(allowed)))
                self.assertEqual(result['raw_changes'][0], growth)
                self.assertEqual([e['kind'] for e in result['raw_changes']],
                                 ['series_growth', 'family_removed', 'family_added'])
        with self.assertRaisesRegex(InputError, 'unused allowlist'):
            compare(left, right, Config.from_json('{"max_series_growth_percent":0.1}'),
                    [growth], rename_map=mapping)

    def test_extreme_positive_underflow_is_net_growth_with_explicit_zero_baseline(self):
        config = Config.from_json('{"max_series_growth_percent":1e-99999999999999999999}')
        for before, after, exceeds, percent in [
                ('', '', False, 0.0), ('', 'x 1\n', True, None),
                ('x 1\n', '', False, 0.0),
                ('x{id="a"} 1\n', 'x{id="b"} 1\n', False, 0.0)]:
            with self.subTest(before=before, after=after):
                result = compare(parse_snapshot(before), parse_snapshot(after), config, include_raw_changes=True)
                self.assertEqual(result['observed_series']['growth_limit_exceeded'], exceeds)
                self.assertEqual(result['observed_series']['net_growth_percent'], percent)
                self.assertEqual('series_growth' in [e['kind'] for e in result['raw_changes']], exceeds)


if __name__ == '__main__':
    unittest.main()
