"""Bounded static replay mismatch paths through a real source or installed CLI."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from . import test_replay_evidence as replay

FIELDS = replay.FIELDS


class StaticDiagnostics(unittest.TestCase):
    setUp = replay.ReplayEvidence.setUp
    check = replay.ReplayEvidence.check

    def command(self, mode='verify-replay-evidence', *extra):
        executable = [os.environ['TAILSIM_TEST_CLI']] if os.environ.get('TAILSIM_TEST_CLI') else [sys.executable, '-m', 'otelwrench', 'tailsim']
        args = [*executable, mode, str(self.source), '--config', str(self.config)]
        if mode != 'replay':
            args += ['--report', str(self.saved)]
        result = subprocess.run([*args, *extra], capture_output=True, text=True)
        if directory := os.environ.get('TAILSIM_TEST_LOG_DIR'):
            log = Path(directory)
            log.mkdir(parents=True, exist_ok=True)
            name = f'{len(list(log.glob("*.json"))):04d}'
            (log / (name + '.stdout')).write_text(result.stdout)
            (log / (name + '.stderr')).write_text(result.stderr)
            (log / (name + '.json')).write_text(json.dumps({'command': result.args,
                'cwd': str(Path.cwd()), 'exitCode': result.returncode,
                'stdoutPath': str(log / (name + '.stdout')),
                'stderrPath': str(log / (name + '.stderr'))}, indent=2))
        return result

    def save(self, value):
        self.saved.write_text(json.dumps(value, ensure_ascii=True))

    def produce(self):
        result = self.command('replay')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.original = json.loads(result.stdout)
        self.saved.write_text(result.stdout)

    def diagnose(self, expected=(), limit=1000, truncated=False, fields=()):
        result = self.command('verify-replay-evidence', '--diagnostic-limit', str(limit))
        self.assertEqual(result.returncode, int(bool(fields)), result.stderr)
        self.assertEqual(result.stderr, '')
        expected = {'schema_version': 1, 'mode': 'replay_evidence_verification',
                    'matches': not bool(fields), 'checks': {k: {'matches': k not in fields} for k in FIELDS},
                    'diagnostics': {'limit': int(str(limit).lstrip('0') or '0'), 'mismatches': [
                        {'path': p, 'reason': r} for p, r in expected], 'truncated': truncated}}
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual(result.stdout, json.dumps(expected, ensure_ascii=True, indent=2) + '\n')
        self.assertNotIn(str(self.root), result.stdout)
        self.assertNotIn('SECRET_EVIDENCE_VALUE', result.stdout)
        return result.stdout

    def test_summary_regression_and_default_bytes(self):
        # Independent fixture decisions, raw OTLP byte filtering and source hash.
        replay.ReplayEvidence.test_saved_replay_agrees_with_independent_decisions_and_bytes(self)
        before = self.command()
        value = copy.deepcopy(self.original)
        value['summary']['kept_traces'] = -1
        self.save(value)
        self.diagnose([('/summary/kept_traces', 'value_mismatch')], 1, fields=('summary',))
        result = self.command()
        expected = {'schema_version': 1, 'mode': 'replay_evidence_verification', 'matches': False,
                    'checks': {k: {'matches': k != 'summary'} for k in FIELDS}}
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, json.dumps(expected, ensure_ascii=False, indent=2) + '\n')
        self.save(self.original)
        self.diagnose(limit=1)
        self.assertEqual(self.command().stdout, before.stdout)

    def test_identities_all_evidence_fields_and_global_prefixes(self):
        for key in ('corpus_source_sha256', 'effective_config_sha256'):
            value = copy.deepcopy(self.original)
            value['fingerprints'][key] = '0' * 64
            self.save(value)
            self.diagnose([(f'/fingerprints/{key}', 'value_mismatch')], 1, fields=('fingerprints',))
        value = copy.deepcopy(self.original)
        value['assumptions']['production_savings_estimated'] = True
        value['byte_measure'] = 'SECRET_EVIDENCE_VALUE'
        value['fingerprints']['corpus_source_sha256'] = '0' * 64
        value['summary']['kept_spans'] = -1
        value['traces'][0]['decision'] = 'SECRET_EVIDENCE_VALUE'
        value['traces'][1]['policies'][0]['reason'] = 'SECRET_EVIDENCE_VALUE'
        self.save(value)
        expected = [('/assumptions/production_savings_estimated', 'value_mismatch'),
                    ('/byte_measure', 'value_mismatch'), ('/fingerprints/corpus_source_sha256', 'value_mismatch'),
                    ('/summary/kept_spans', 'value_mismatch'), ('/traces/0/decision', 'value_mismatch'),
                    ('/traces/1/policies/0/reason', 'value_mismatch')]
        for limit in range(1, len(expected) + 1):
            self.diagnose(expected[:limit], limit, limit < len(expected),
                          ('assumptions', 'byte_measure', 'fingerprints', 'summary', 'traces'))

    def test_member_escaping_subtrees_and_mapping_order(self):
        value = copy.deepcopy(self.original)
        del value['summary']['error_retention']
        for key in ('z', 'λ/~', '', 'a/b~c', '~/', 'a', chr(0xd800) + '/~'):
            value['summary'][key] = {'SECRET_EVIDENCE_VALUE': [1, 2, 3]}
        self.save(value)
        expected = [('/summary/', 'extra_key'), ('/summary/a', 'extra_key'),
                    ('/summary/a~1b~0c', 'extra_key'), ('/summary/error_retention', 'missing_key'),
                    ('/summary/z', 'extra_key'), ('/summary/~0~1', 'extra_key'),
                    ('/summary/λ~1~0', 'extra_key'), ('/summary/' + chr(0xd800) + '~1~0', 'extra_key')]
        first = self.diagnose(expected, fields=('summary',))
        def reverse_keys(node):
            if isinstance(node, dict):
                return {k: reverse_keys(node[k]) for k in reversed(node)}
            if isinstance(node, list):
                return [reverse_keys(v) for v in node]
            return node
        self.save(reverse_keys(value))
        self.assertEqual(self.diagnose(expected, fields=('summary',)), first)
        self.save(reverse_keys(self.original))
        self.diagnose()

    def test_strict_types_arrays_and_numeric_index_order(self):
        value = copy.deepcopy(self.original)
        value['traces'][1]['matched_policy_names'].reverse()
        value['traces'][0]['policies'].clear()
        value['traces'][1]['policies'].extend(['SECRET_EVIDENCE_VALUE'] * 12)
        self.save(value)
        expected = [(f'/traces/0/policies/{i}', 'missing_item') for i in range(2)]
        expected += [(f'/traces/1/matched_policy_names/{i}', 'value_mismatch') for i in range(2)]
        expected += [(f'/traces/1/policies/{i}', 'extra_item') for i in range(2, 14)]
        self.diagnose(expected, fields=('traces',))
        for changed in (True, 1.0, '1', None, [], {}, 999):
            value = copy.deepcopy(self.original)
            value['summary']['kept_traces'] = changed
            self.save(value)
            reason = 'value_mismatch' if type(changed) is int else 'type_mismatch'
            self.diagnose([('/summary/kept_traces', reason)], fields=('summary',))
        value = copy.deepcopy(self.original)
        value['assumptions']['production_savings_estimated'] = 0
        value['traces'][0]['error'] = 0
        value['traces'][1]['policies'][0]['matched'] = 1.0
        self.save(value)
        self.diagnose([('/assumptions/production_savings_estimated', 'type_mismatch'),
                       ('/traces/0/error', 'type_mismatch'), ('/traces/1/policies/0/matched', 'type_mismatch')],
                      fields=('assumptions', 'traces'))
        self.save({**self.original, 'traces': [None]})
        self.diagnose([('/traces/0', 'type_mismatch'), ('/traces/1', 'missing_item')], fields=('traces',))

    def test_nested_policy_explanations(self):
        self.config.write_bytes((replay.ROOT / 'examples/tailsim/and-not.yaml').read_bytes())
        self.produce()
        self.assertEqual([t['decision'] for t in self.original['traces']], ['drop', 'sample'])
        tree = self.original['traces'][1]['policies'][0]
        self.assertEqual(tree['type'], 'and')
        self.assertEqual(tree['children'][1]['type'], 'not')
        value = copy.deepcopy(self.original)
        value['traces'][1]['policies'][0]['children'][1]['children'][0]['reason'] = 'SECRET_EVIDENCE_VALUE'
        self.save(value)
        self.diagnose([('/traces/1/policies/0/children/1/children/0/reason', 'value_mismatch')], 1, fields=('traces',))

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
        self.diagnose(limit='0' * 5000 + '1000')

    def test_invalid_options_reports_sources_and_configs_are_atomic(self):
        def invalid(*flags):
            result = self.command('verify-replay-evidence', *flags)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertTrue(result.stderr)
            self.assertNotIn('Traceback', result.stderr)
        for suffix in ([], ['0'], ['-1'], ['1001'], ['1.0'], ['1e2'], ['true'], ['+1'], [' 1'], ['1 '],
                       ['١'], ['１'], ['9' * 5000], ['1', '--diagnostic-limit', '2']):
            with self.subTest(suffix=str(suffix)[:60]):
                invalid('--diagnostic-limit', *suffix)
        for flag in ('--diagnostic', '--diagnostic-limits', '--diagnostics'):
            invalid(flag, '1')
        raw = json.dumps(self.original)
        variants = [b'{', b'\xff', raw.replace('"summary": {', '"summary": {"bad":1,"bad":2,').encode()]
        for token in ('NaN', 'Infinity', '-Infinity', '1e999', '-1e999'):
            variants.append(raw.replace('"summary": {', '"summary": {"bad":' + token + ',').encode())
        for key, changed in [('schema_version', True), ('schema_version', 1.0), ('mode', 'arrival_order_offline'),
                             ('extra', 1), ('traces', {}), ('summary', []), ('byte_measure', {})]:
            variants.append(json.dumps({**self.original, key: changed}).encode())
        for change in ({'version': True}, {'algorithm': 'sha512'}, {'corpus_source_sha256': 'A' * 64}):
            variants.append(json.dumps({**self.original, 'fingerprints': {**self.original['fingerprints'], **change}}).encode())
        for key in self.original:
            variants.append(json.dumps({k: v for k, v in self.original.items() if k != key}).encode())
        for value in variants:
            self.saved.write_bytes(value)
            invalid('--diagnostic-limit', '1')
        self.save(self.original)
        for path, variants in ((self.source, [b'\xff', b'{', b'{"resourceSpans":[],"extra":1}']),
                               (self.config, [b'\xff', b'processors: [broken',
                                b'processors: {tail_sampling: {decision_wait: 1ms}}'])):
            original = path.read_bytes()
            for bad in variants:
                path.write_bytes(bad)
                invalid('--diagnostic-limit', '1')
            path.write_bytes(original)
        for path in (self.source, self.config, self.saved):
            original = path.read_bytes()
            path.unlink()
            invalid('--diagnostic-limit', '1')
            path.write_bytes(original)

    def test_supplied_config_drives_replay_empty_corpus_and_opaque_provenance(self):
        value = copy.deepcopy(self.original)
        self.config.write_text('processors: {tail_sampling: {policies: [{name: all, type: always_sample}]}}')
        self.produce()
        # Saved identity matches supplied config, but decisions come from the old config.
        value['fingerprints'] = self.original['fingerprints']
        self.save(value)
        result = self.command('verify-replay-evidence', '--diagnostic-limit=1')
        self.assertEqual(result.returncode, 1, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertTrue(parsed['checks']['fingerprints']['matches'])
        self.assertFalse(parsed['checks']['traces']['matches'])
        self.assertTrue(parsed['diagnostics']['truncated'])
        self.save({**self.original, 'assumptions': {}, 'byte_measure': 'untrusted', 'summary': {}, 'traces': [None]})
        result = self.command('verify-replay')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.command('verify-replay', '--diagnostic-limit', '1')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.source.write_bytes(b'{"resourceSpans":[]}')
        self.produce()
        self.diagnose(limit=1)
