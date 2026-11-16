"""Bounded, value-free mismatch paths through actual CLI subprocesses."""

import copy
import json
import unittest

from . import test_compare_arrivals_evidence as pair

FIELDS, SIDES = pair.FIELDS, pair.SIDES


class ArrivalDiagnostics(unittest.TestCase):
    setUp = pair.CompareArrivalsEvidence.setUp
    command = pair.CompareArrivalsEvidence.command
    produce = pair.CompareArrivalsEvidence.produce
    save = pair.CompareArrivalsEvidence.save

    def diagnose(self, expected, limit=1000, truncated=False, fields=()):
        result = self.command('verify-compare-arrivals-evidence', '100', '50',
                              '--diagnostic-limit', str(limit))
        self.assertEqual(result.returncode, int(bool(fields)), result.stderr)
        self.assertEqual(result.stderr, '')
        base = {'schema_version': 1, 'mode': 'arrival_comparison_evidence_verification',
                'matches': not bool(fields),
                'checks': {k: {'matches': k not in fields} for k in FIELDS}}
        records = [{'path': path, 'reason': reason} for path, reason in expected]
        base['diagnostics'] = {'limit': int(limit), 'mismatches': records, 'truncated': truncated}
        self.assertEqual(json.loads(result.stdout), base)
        self.assertEqual(result.stdout, json.dumps(base, ensure_ascii=True, indent=2) + '\n')
        self.assertNotIn(str(self.root), result.stdout)
        self.assertNotIn('SECRET_EVIDENCE_VALUE', result.stdout)
        return result.stdout

    def test_nested_model_regression_and_unchanged_default_bytes(self):
        pair.CompareArrivalsEvidence.assert_oracle(self)
        before = self.command()
        value = copy.deepcopy(self.original)
        value['baseline']['model']['decision_wait_ms'] = -1
        self.save(value)
        self.diagnose([('/baseline/model/decision_wait_ms', 'value_mismatch')],
                      limit=1, fields=('baseline',))
        default = {'schema_version': 1, 'mode': 'arrival_comparison_evidence_verification',
                   'matches': False, 'checks': {k: {'matches': k != 'baseline'} for k in FIELDS}}
        result = self.command()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, json.dumps(default, ensure_ascii=False, indent=2) + '\n')
        self.save(self.original)
        self.diagnose([], limit=1)
        self.assertEqual(self.command().stdout, before.stdout)

    def test_all_labelled_identities_and_nested_snapshots_explanations(self):
        for section in ('fingerprints', *SIDES):
            fp = self.original['fingerprints'] if section == 'fingerprints' else self.original[section]['fingerprints']
            for key in sorted(fp.keys() - {'version', 'algorithm'}):
                value = copy.deepcopy(self.original)
                block = value['fingerprints'] if section == 'fingerprints' else value[section]['fingerprints']
                block[key] = '0' * 64
                self.save(value)
                path = f'/fingerprints/{key}' if section == 'fingerprints' else f'/{section}/fingerprints/{key}'
                with self.subTest(path=path):
                    self.diagnose([(path, 'value_mismatch')], limit=1, fields=(section,))
        paths = [f'/{s}/traces/0/decision_snapshot/policies/0/reason' for s in SIDES]
        paths += [f'/comparison/changed_decisions/0/{s}/full_trace/policies/0/reason' for s in SIDES]
        value = copy.deepcopy(self.original)
        for path in paths:
            node = value
            parts = path.split('/')[1:]
            for part in parts[:-1]:
                node = node[int(part)] if isinstance(node, list) else node[part]
            node[parts[-1]] = 'SECRET_EVIDENCE_VALUE'
        self.save(value)
        expected = [(p, 'value_mismatch') for p in sorted(paths)]
        self.diagnose(expected, fields=(*SIDES, 'comparison'))
        # Every prefix uses a single budget across both sides and comparison.
        for limit in range(1, len(expected) + 1):
            self.diagnose(expected[:limit], limit, limit < len(expected), (*SIDES, 'comparison'))

    def test_key_escaping_missing_extra_subtrees_and_deterministic_order(self):
        value = copy.deepcopy(self.original)
        del value['comparison']['unresolved_traces']  # one missing subtree, not its descendants
        for key in ('z', 'λ/~', '', 'a/b~c', '~/', 'a'):
            value['comparison'][key] = {'SECRET_EVIDENCE_VALUE': [1, 2, 3]}
        self.save(value)
        expected = [(p, r) for p, r in [
            ('/comparison/', 'extra_key'), ('/comparison/a', 'extra_key'),
            ('/comparison/a~1b~0c', 'extra_key'), ('/comparison/unresolved_traces', 'missing_key'),
            ('/comparison/z', 'extra_key'), ('/comparison/~0~1', 'extra_key'),
            ('/comparison/λ~1~0', 'extra_key')]]
        first = self.diagnose(expected, fields=('comparison',))
        def reverse_keys(node):
            if isinstance(node, dict):
                return {k: reverse_keys(node[k]) for k in reversed(node)}
            if isinstance(node, list):
                return [reverse_keys(v) for v in node]
            return node
        self.saved.write_text(json.dumps(reverse_keys(value), ensure_ascii=True))
        self.assertEqual(self.diagnose(expected, fields=('comparison',)), first)
        self.saved.write_text(json.dumps(reverse_keys(self.original), ensure_ascii=True))
        self.diagnose([])

    def test_strict_types_scalar_values_and_ordered_span_arrays(self):
        value = copy.deepcopy(self.original)
        value['baseline']['traces'][0]['on_time_span_ids'].reverse()
        value['candidate']['traces'][0]['on_time_span_ids'].append('SECRET_EVIDENCE_VALUE')
        value['baseline']['traces'][1]['on_time_span_ids'].clear()
        self.save(value)
        self.diagnose([
            ('/baseline/traces/0/on_time_span_ids/0', 'value_mismatch'),
            ('/baseline/traces/0/on_time_span_ids/1', 'value_mismatch'),
            ('/baseline/traces/1/on_time_span_ids/0', 'missing_item'),
            ('/candidate/traces/0/on_time_span_ids/1', 'extra_item')], fields=SIDES)
        for changed in (True, 1.0, '1', None, [], {}, 999):
            value = copy.deepcopy(self.original)
            value['comparison']['changed_decision_count'] = changed
            self.save(value)
            reason = 'value_mismatch' if type(changed) is int else 'type_mismatch'
            self.diagnose([('/comparison/changed_decision_count', reason)], fields=('comparison',))
        value = copy.deepcopy(self.original)
        value['baseline']['traces'][0]['full_trace']['error'] = 1
        value['candidate']['summary']['kept_spans'] = float(value['candidate']['summary']['kept_spans'])
        self.save(value)
        self.diagnose([('/baseline/traces/0/full_trace/error', 'type_mismatch'),
                       ('/candidate/summary/kept_spans', 'type_mismatch')], fields=SIDES)

    def test_total_limit_maximum_and_exact_truncation(self):
        value = copy.deepcopy(self.original)
        for i in range(1001):
            value['assumptions'][f'key{i:04d}'] = 'SECRET_EVIDENCE_VALUE'
        expected = [(f'/assumptions/key{i:04d}', 'extra_key') for i in range(1001)]
        self.save(value)
        self.diagnose(expected[:1], '0001', True, ('assumptions',))
        self.diagnose(expected[:1000], 1000, True, ('assumptions',))
        del value['assumptions']['key1000']
        self.save(value)
        self.diagnose(expected[:1000], 1000, False, ('assumptions',))
        # An equal large subtree consumes no budget.
        self.save(self.original)
        self.diagnose([], 1000)

    def test_invalid_flags_and_inputs_are_atomic(self):
        variants = [[], ['0'], ['-1'], ['1001'], ['1.0'], ['1e2'], ['true'], ['+1'], [' 1'],
                    ['١'], ['１'], ['9' * 5000], ['1', '--diagnostic-limit', '2']]
        for suffix in variants:
            with self.subTest(suffix=str(suffix)[:60]):
                result = self.command('verify-compare-arrivals-evidence', '100', '50', '--diagnostic-limit', *suffix)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertNotIn('Traceback', result.stderr)
        for flag in ('--diagnostic', '--diagnostic-limits', '--diagnostics'):
            result = self.command('verify-compare-arrivals-evidence', '100', '50', flag, '1')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, '')
        for raw in ('{', '{"x":1,"x":2}', json.dumps(self.original).replace('"comparison": {', '"comparison": {"x":1e999,')):
            self.saved.write_text(raw)
            result = self.command('verify-compare-arrivals-evidence', '100', '50', '--diagnostic-limit', '1')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, '')
        self.save(self.original)
        for a, b in (('0', '50'), ('100', '-1'), ('18446744073709551615', '50')):
            result = self.command('verify-compare-arrivals-evidence', a, b, '--diagnostic-limit', '1')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, '')
        self.config.write_text('processors: {tail_sampling: {decision_wait: 1ms}}')
        result = self.command('verify-compare-arrivals-evidence', '100', '50', '--diagnostic-limit', '1')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')

    def test_escaped_surrogate_member_and_numeric_array_index_order(self):
        value = copy.deepcopy(self.original)
        key = chr(0xd800) + '/~'
        value['comparison'][key] = 'SECRET_EVIDENCE_VALUE'
        value['candidate']['traces'][0]['on_time_span_ids'].extend(['SECRET_EVIDENCE_VALUE'] * 12)
        self.saved.write_text(json.dumps(value, ensure_ascii=True))
        result = self.command('verify-compare-arrivals-evidence', '100', '50', '--diagnostic-limit', '13')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, '')
        expected = [{'path': f'/candidate/traces/0/on_time_span_ids/{i}', 'reason': 'extra_item'}
                    for i in range(1, 13)]
        expected.append({'path': '/comparison/' + chr(0xd800) + '~1~0', 'reason': 'extra_key'})
        self.assertEqual(json.loads(result.stdout)['diagnostics'],
                         {'limit': 13, 'mismatches': expected, 'truncated': False})
        self.assertNotIn('SECRET_EVIDENCE_VALUE', result.stdout)
        self.assertNotIn(str(self.root), result.stdout)
