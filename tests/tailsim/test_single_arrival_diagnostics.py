"""Single-wait mismatch diagnostics through the real source or installed CLI."""

import copy
import json
import os
from pathlib import Path
import unittest

from . import test_arrivals_evidence as arrival

FIELDS = arrival.FIELDS


class SingleArrivalDiagnostics(unittest.TestCase):
    setUp = arrival.ArrivalsEvidence.setUp
    produce = arrival.ArrivalsEvidence.produce
    save = arrival.ArrivalsEvidence.save

    def command(self, mode='verify-arrivals-evidence', wait='100', *extra):
        result = arrival.ArrivalsEvidence.command(self, mode, wait, *extra)
        if directory := os.environ.get('TAILSIM_TEST_LOG_DIR'):
            log = Path(directory)
            log.mkdir(parents=True, exist_ok=True)
            name = f'{len(list(log.glob("*.json"))):04d}'
            (log / (name + '.stdout')).write_text(result.stdout)
            (log / (name + '.stderr')).write_text(result.stderr)
            (log / (name + '.json')).write_text(json.dumps({'command': result.args,
                'exitCode': result.returncode, 'stdout': str(log / (name + '.stdout')),
                'stderr': str(log / (name + '.stderr'))}, indent=2))
        return result

    def diagnose(self, expected=(), limit=1000, truncated=False, fields=(), wait='100'):
        result = self.command('verify-arrivals-evidence', wait, '--diagnostic-limit', str(limit))
        self.assertEqual(result.returncode, int(bool(fields)), result.stderr)
        self.assertEqual(result.stderr, '')
        base = {'schema_version': 1, 'mode': 'arrival_evidence_verification',
                'matches': not bool(fields), 'checks': {k: {'matches': k not in fields} for k in FIELDS},
                'diagnostics': {'limit': int(limit), 'mismatches': [
                    {'path': p, 'reason': r} for p, r in expected], 'truncated': truncated}}
        self.assertEqual(json.loads(result.stdout), base)
        self.assertEqual(result.stdout, json.dumps(base, ensure_ascii=True, indent=2) + '\n')
        self.assertNotIn(str(self.root), result.stdout)
        self.assertNotIn('SECRET_EVIDENCE_VALUE', result.stdout)
        return result.stdout

    def test_model_regression_and_default_bytes(self):
        arrival.ArrivalsEvidence.assert_oracle(self)
        before = self.command()
        value = copy.deepcopy(self.original)
        value['model']['decision_wait_ms'] = -1
        self.save(value)
        self.diagnose([('/model/decision_wait_ms', 'value_mismatch')], limit=1, fields=('model',))
        result = self.command()
        expected = {'schema_version': 1, 'mode': 'arrival_evidence_verification', 'matches': False,
                    'checks': {k: {'matches': k != 'model'} for k in FIELDS}}
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, json.dumps(expected, ensure_ascii=False, indent=2) + '\n')
        self.save(self.original)
        self.diagnose(limit=1)
        self.assertEqual(self.command().stdout, before.stdout)

    def test_identities_snapshots_explanations_and_global_prefixes(self):
        for key in sorted(self.original['fingerprints'].keys() - {'version', 'algorithm'}):
            value = copy.deepcopy(self.original)
            value['fingerprints'][key] = '0' * 64
            self.save(value)
            with self.subTest(key=key):
                self.diagnose([(f'/fingerprints/{key}', 'value_mismatch')], 1, fields=('fingerprints',))
        value = copy.deepcopy(self.original)
        value['assumptions']['production_savings_estimated'] = True
        value['model']['name'] = 'SECRET_EVIDENCE_VALUE'
        value['summary']['kept_spans'] = -1
        value['traces'][0]['decision_snapshot']['policies'][0]['reason'] = 'SECRET_EVIDENCE_VALUE'
        value['traces'][0]['full_trace']['policies'][0]['children'].append('SECRET_EVIDENCE_VALUE')
        self.save(value)
        expected = [('/assumptions/production_savings_estimated', 'value_mismatch'),
                    ('/model/name', 'value_mismatch'), ('/summary/kept_spans', 'value_mismatch'),
                    ('/traces/0/decision_snapshot/policies/0/reason', 'value_mismatch'),
                    ('/traces/0/full_trace/policies/0/children/0', 'extra_item')]
        for limit in range(1, len(expected) + 1):
            self.diagnose(expected[:limit], limit, limit < len(expected),
                          ('assumptions', 'model', 'summary', 'traces'))

    def test_member_escaping_subtrees_and_mapping_order(self):
        value = copy.deepcopy(self.original)
        del value['summary']['kept_spans']
        keys = ('z', 'λ/~', '', 'a/b~c', '~/', 'a', chr(0xd800) + '/~')
        for key in keys:
            value['summary'][key] = {'SECRET_EVIDENCE_VALUE': [1, 2, 3]}
        self.saved.write_text(json.dumps(value, ensure_ascii=True))
        expected = [('/summary/', 'extra_key'), ('/summary/a', 'extra_key'),
                    ('/summary/a~1b~0c', 'extra_key'), ('/summary/kept_spans', 'missing_key'),
                    ('/summary/z', 'extra_key'), ('/summary/~0~1', 'extra_key'),
                    ('/summary/λ~1~0', 'extra_key'), ('/summary/' + chr(0xd800) + '~1~0', 'extra_key')]
        first = self.diagnose(expected, fields=('summary',))
        def reverse_keys(node):
            if isinstance(node, dict):
                return {k: reverse_keys(node[k]) for k in reversed(node)}
            if isinstance(node, list):
                return [reverse_keys(v) for v in node]
            return node
        self.saved.write_text(json.dumps(reverse_keys(value), ensure_ascii=True))
        self.assertEqual(self.diagnose(expected, fields=('summary',)), first)
        self.saved.write_text(json.dumps(reverse_keys(self.original), ensure_ascii=True))
        self.diagnose()

    def test_strict_types_span_arrays_and_numeric_index_order(self):
        value = copy.deepcopy(self.original)
        value['traces'][0]['on_time_span_ids'].reverse()
        value['traces'][1]['on_time_span_ids'].clear()
        value['traces'][2]['on_time_span_ids'].extend(['SECRET_EVIDENCE_VALUE'] * 12)
        self.save(value)
        expected = [('/traces/0/on_time_span_ids/0', 'value_mismatch'),
                    ('/traces/0/on_time_span_ids/1', 'value_mismatch'),
                    ('/traces/1/on_time_span_ids/0', 'missing_item')]
        expected += [(f'/traces/2/on_time_span_ids/{i}', 'extra_item') for i in range(1, 13)]
        self.diagnose(expected, fields=('traces',))
        for changed in (True, 1.0, '1', None, [], {}, 999):
            value = copy.deepcopy(self.original)
            value['summary']['sampled_traces'] = changed
            self.save(value)
            reason = 'value_mismatch' if type(changed) is int else 'type_mismatch'
            self.diagnose([('/summary/sampled_traces', reason)], fields=('summary',))
        value = copy.deepcopy(self.original)
        value['assumptions']['production_savings_estimated'] = 0
        value['model']['decision_wait_ms'] = 100.0
        value['traces'][0]['decision_snapshot']['error'] = 1
        value['traces'][0]['full_trace']['policies'][0]['matched'] = 1.0
        self.save(value)
        self.diagnose([('/assumptions/production_savings_estimated', 'type_mismatch'),
                       ('/model/decision_wait_ms', 'type_mismatch'),
                       ('/traces/0/decision_snapshot/error', 'type_mismatch'),
                       ('/traces/0/full_trace/policies/0/matched', 'type_mismatch')],
                      fields=('assumptions', 'model', 'traces'))

    def test_total_limit_1000_and_exact_truncation(self):
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
        self.save(self.original)
        self.diagnose(limit='0001000')

    def test_invalid_options_reports_inputs_and_deadlines_are_atomic(self):
        def invalid(wait='100', *flags):
            result = self.command('verify-arrivals-evidence', wait, *flags)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertTrue(result.stderr)
            self.assertNotIn('Traceback', result.stderr)
        for suffix in ([], ['0'], ['-1'], ['1001'], ['1.0'], ['1e2'], ['true'], ['+1'], [' 1'],
                       ['١'], ['１'], ['9' * 5000], ['1', '--diagnostic-limit', '2']):
            with self.subTest(suffix=str(suffix)[:60]):
                invalid('100', '--diagnostic-limit', *suffix)
        for flag in ('--diagnostic', '--diagnostic-limits', '--diagnostics'):
            invalid('100', flag, '1')
        raw = json.dumps(self.original)
        variants = [b'{', b'\xff', b'{"x":1,"x":2}', raw.replace('"model": {', '"model": {"bad":1e999,').encode()]
        for key, changed in [('schema_version', True), ('mode', 'arrival_order_offline_comparison'),
                             ('extra', 1), ('traces', {}), ('model', [])]:
            variants.append(json.dumps({**self.original, key: changed}).encode())
        for change in ({'version': True}, {'algorithm': 'sha512'}, {'corpus_source_sha256': 'A' * 64}):
            variants.append(json.dumps({**self.original, 'fingerprints': {**self.original['fingerprints'], **change}}).encode())
        for key in self.original:
            variants.append(json.dumps({k: v for k, v in self.original.items() if k != key}).encode())
        for value in variants:
            self.saved.write_bytes(value)
            invalid('100', '--diagnostic-limit', '1')
        self.save(self.original)
        for wait in ('0', '-1', 'true', '1.5', str((2**64 - 1) // 1_000_000 + 1)):
            invalid(wait, '--diagnostic-limit', '1')
        manifest = json.loads(self.arrivals.read_bytes())
        manifest['observation_end_unix_nano'] = str(2**64 - 1)
        manifest['arrivals'] = [{**manifest['arrivals'][0], 'at_unix_nano': str(2**64 - 1 - 1_000_000)}]
        self.arrivals.write_text(json.dumps(manifest))
        self.produce(wait='1')
        arrival.ArrivalsEvidence.assert_oracle(self, wait=1)
        self.diagnose(limit=1, wait='1')  # exact uint64 maximum is valid
        manifest['arrivals'][0]['at_unix_nano'] = str(2**64 - 1 - 1_000_000 + 1)
        self.arrivals.write_text(json.dumps(manifest))
        invalid('1', '--diagnostic-limit', '1')
        self.arrivals.write_bytes((arrival.FIXTURE / 'arrivals.json').read_bytes())
        for path, bad in ((self.source, b'{"resourceSpans":[],"extra":1}'),
                          (self.config, b'processors: {tail_sampling: {decision_wait: 1ms}}'),
                          (self.arrivals, b'{"schema_version":1,"arrivals":[]}')):
            original = path.read_bytes()
            path.write_bytes(bad)
            invalid('100', '--diagnostic-limit', '1')
            path.write_bytes(original)
        for path in (self.source, self.config, self.arrivals, self.saved):
            original = path.read_bytes()
            path.unlink()
            invalid('100', '--diagnostic-limit', '1')
            path.write_bytes(original)

    def test_supplied_wait_drives_replay_and_empty_corpus(self):
        # Make saved wait/fingerprint look like 50ms while retaining 100ms rows.
        self.produce(wait='50')
        value = copy.deepcopy(self.original)
        self.produce(wait='100')
        value['traces'] = self.original['traces']
        self.save(value)
        result = self.command('verify-arrivals-evidence', '50', '--diagnostic-limit=1')
        self.assertEqual(result.returncode, 1, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertFalse(parsed['checks']['traces']['matches'])
        self.assertTrue(parsed['checks']['model']['matches'])
        self.assertTrue(parsed['checks']['fingerprints']['matches'])
        self.assertTrue(parsed['diagnostics']['truncated'])
        self.assertTrue(parsed['diagnostics']['mismatches'][0]['path'].startswith('/traces/'))
        self.source.write_bytes(b'{"resourceSpans":[]}')
        self.arrivals.write_text('{"schema_version":1,"observation_end_unix_nano":0,"arrivals":[]}')
        self.produce()
        arrival.ArrivalsEvidence.assert_oracle(self)
        self.diagnose(limit=1)

    def test_provenance_remains_opaque_and_does_not_accept_diagnostics(self):
        value = copy.deepcopy(self.original)
        value.update(model={'untrusted': True}, assumptions={}, summary={}, traces=[None])
        self.save(value)
        result = self.command('verify-arrivals')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['matches'])
        result = self.command('verify-arrivals', '100', '--diagnostic-limit', '1')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
