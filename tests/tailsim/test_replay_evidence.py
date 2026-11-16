"""Local regeneration via real CLI; saved evidence is data, never replay input."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
FIELDS = ('schema_version', 'mode', 'fingerprints', 'assumptions', 'byte_measure', 'summary', 'traces')


class ReplayEvidence(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source, self.config, self.saved = [self.root / name for name in
                                               ('traces.json', 'policy.yaml', 'report.json')]
        self.source.write_bytes((ROOT / 'examples/tailsim/traces.json').read_bytes())
        self.config.write_bytes((ROOT / 'examples/tailsim/policy.yaml').read_bytes())
        result = self.command('replay')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.original = json.loads(result.stdout)
        self.saved.write_text(result.stdout)

    def command(self, mode='verify-replay-evidence', *extra):
        args = [sys.executable, '-m', 'otelwrench', 'tailsim', mode, str(self.source), '--config', str(self.config)]
        if mode != 'replay':
            args += ['--report', str(self.saved)]
        return subprocess.run([*args, *extra], capture_output=True, text=True)

    def check(self, mismatches=()):
        result = self.command()
        self.assertEqual(result.returncode, int(bool(mismatches)), result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertEqual(json.loads(result.stdout), {
            'schema_version': 1, 'mode': 'replay_evidence_verification',
            'matches': not bool(mismatches),
            'checks': {field: {'matches': field not in mismatches} for field in FIELDS}})
        self.assertNotIn(str(self.root), result.stdout)

    def test_saved_replay_agrees_with_independent_decisions_and_bytes(self):
        # Independent fixture facts and OTLP filtering; no engine helpers.
        self.assertEqual([t['decision'] for t in self.original['traces']], ['drop', 'sample'])
        self.assertEqual([t['matched_policy_names'] for t in self.original['traces']], [[], ['errors', 'slow']])
        raw = json.loads(self.source.read_bytes())
        kept = copy.deepcopy(raw)
        kept['resourceSpans'][0]['scopeSpans'][0]['spans'] = [raw['resourceSpans'][0]['scopeSpans'][0]['spans'][1]]
        def size(value):
            return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
        summary = self.original['summary']
        self.assertEqual(summary['serialized_total_bytes'], size(raw))
        self.assertEqual(summary['serialized_kept_bytes'], size(kept))
        self.assertEqual(summary['serialized_removed_bytes'], size(raw) - size(kept))
        self.assertEqual(summary['source_file_bytes'], len(self.source.read_bytes()))
        self.assertEqual(self.original['fingerprints']['corpus_source_sha256'], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.check()

    def save(self, value):
        self.saved.write_text(json.dumps(value, ensure_ascii=False))

    def assert_invalid(self, *extra):
        result = self.command('verify-replay-evidence', *extra)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_each_evidence_field_and_both_fingerprints_are_compared(self):
        mutations = [
            ('assumptions', lambda r: r['assumptions'].update(production_savings_estimated=True)),
            ('byte_measure', lambda r: r.update(byte_measure='claimed transport bytes')),
            ('summary', lambda r: r['summary'].update(serialized_kept_bytes=0)),
            ('summary', lambda r: r['summary'].update(kept_traces=2)),
            ('traces', lambda r: r['traces'][0].update(decision='sample')),
            ('traces', lambda r: r['traces'][1]['policies'][0].update(reason='invented explanation')),
            ('traces', lambda r: r['traces'][1]['matched_policy_names'].reverse()),
        ]
        for key in ('corpus_source_sha256', 'effective_config_sha256'):
            mutations.append(('fingerprints', lambda r, key=key: r['fingerprints'].update({key: '0' * 64})))
        for field, mutate in mutations:
            changed = copy.deepcopy(self.original)
            mutate(changed)
            self.assertNotEqual(changed, self.original)
            self.save(changed)
            with self.subTest(field=field, changed=changed[field]):
                self.check([field])
        # No short-circuit: all evidence and identity fields must be listed.
        changed = copy.deepcopy(self.original)
        for _, mutate in mutations:
            mutate(changed)
        self.save(changed)
        self.check(['fingerprints', 'assumptions', 'byte_measure', 'summary', 'traces'])

    def test_nested_types_keys_and_array_order_are_significant(self):
        changes = [
            ('summary', lambda r: r['summary'].update(kept_traces=True)),
            ('summary', lambda r: r['summary'].update(kept_traces=1.0)),
            ('summary', lambda r: r['summary'].update(kept_traces='1')),
            ('summary', lambda r: r['summary'].update(kept_traces=None)),
            ('traces', lambda r: r['traces'][1].update(error=1)),
            ('traces', lambda r: r['traces'][1]['policies'][0].update(matched=1)),
            ('assumptions', lambda r: r['assumptions'].update(production_savings_estimated=0)),
            ('traces', lambda r: r['traces'].reverse()),
            ('traces', lambda r: r['traces'].append(None)),
            ('traces', lambda r: r.update(traces=[None])),
            ('traces', lambda r: r['traces'][0].pop('reason')),
            ('summary', lambda r: r['summary'].update(extra={'invented': [True, 1, None]})),
            ('summary', lambda r: r['summary'].update(error_retention=[])),
        ]
        for field, mutate in changes:
            changed = copy.deepcopy(self.original)
            mutate(changed)
            self.save(changed)
            with self.subTest(field=field, changed=changed[field]):
                self.check([field])

    def test_json_object_order_escaping_and_equivalent_config_normalize(self):
        def reverse_keys(value):
            if isinstance(value, dict):
                return {key: reverse_keys(value[key]) for key in reversed(value)}
            if isinstance(value, list):
                return [reverse_keys(item) for item in value]
            return value
        # Put Unicode in an explanation so its alternate escaping is exercised.
        self.config.write_text(self.config.read_text().replace('name: errors', 'name: errors-λ'))
        result = self.command('replay')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = reverse_keys(json.loads(result.stdout))
        self.saved.write_text(json.dumps(report, ensure_ascii=True, separators=(',', ':')))
        self.config.write_text('''{"processors":{"tail_sampling":{"policies":[
          {"status_code":{"status_codes":["ERROR","ERROR"]},"type":"status_code","name":"errors-λ"},
          {"latency":{"upper_threshold_ms":0,"threshold_ms":500},"type":"latency","name":"slow"}
        ]}}}''')
        for path_name in ('source', 'config', 'saved'):
            path = getattr(self, path_name)
            moved = path.with_name('moved-' + path.name)
            path.rename(moved)
            setattr(self, path_name, moved)
        self.check()

    def test_supplied_inputs_drive_regeneration_and_empty_corpus(self):
        self.source.write_bytes(self.source.read_bytes() + b'\n')
        self.check(['fingerprints', 'summary'])
        self.source.write_bytes((ROOT / 'examples/tailsim/traces.json').read_bytes())
        self.config.write_text('processors: {tail_sampling: {policies: [{name: all, type: always_sample}]}}')
        self.check(['fingerprints', 'summary', 'traces'])
        self.source.write_bytes(b'{"resourceSpans":[]}')
        result = self.command('replay')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.saved.write_text(result.stdout)
        self.check()

    def test_malformed_envelope_and_metadata_fail_atomically(self):
        variants = [None, [], {}]
        for key in FIELDS:
            changed = copy.deepcopy(self.original)
            del changed[key]
            variants.append(changed)
        for key, value in [('schema_version', True), ('schema_version', 1.0), ('schema_version', '1'),
                           ('schema_version', 2), ('mode', 'complete_trace_offline_comparison'),
                           ('mode', 'arrival_order_offline'), ('extra', 1), ('fingerprints', []),
                           ('assumptions', None), ('summary', []), ('traces', {}), ('byte_measure', {})]:
            variants.append({**self.original, key: value})
        fingerprints = self.original['fingerprints']
        for key in fingerprints:
            variants.append({**self.original, 'fingerprints': {k: v for k, v in fingerprints.items() if k != key}})
        for key, value in [('version', True), ('version', 1.0), ('version', '1'), ('version', 2),
                           ('algorithm', 'SHA256'), ('algorithm', 'sha512'), ('extra', 1)]:
            variants.append({**self.original, 'fingerprints': {**fingerprints, key: value}})
        for key in ('corpus_source_sha256', 'effective_config_sha256'):
            for value in ['A' * 64, 'g' * 64, '0' * 63, None, 1, True]:
                variants.append({**self.original, 'fingerprints': {**fingerprints, key: value}})
        for i, value in enumerate(variants):
            self.save(value)
            with self.subTest(variant=i):
                self.assert_invalid()

    def test_duplicate_keys_nonfinite_numbers_and_bad_json_fail_atomically(self):
        raw = json.dumps(self.original)
        variants = [b'\xff', b'{', b'{"schema_version":1,"schema_version":1}']
        for token in ('NaN', 'Infinity', '-Infinity', '1e999', '-1e999'):
            for marker in ('"summary": {', '"assumptions": {', '"traces": [{'):
                variants.append(raw.replace(marker, marker + '"bad":' + token + ',').encode())
        for marker in ('"summary": {', '"assumptions": {', '"traces": [{', '"fingerprints": {'):
            variants.append(raw.replace(marker, marker + '"bad":1,"bad":2,').encode())
        for data in variants:
            self.saved.write_bytes(data)
            with self.subTest(data=data[:120]):
                self.assert_invalid()

    def test_invalid_sources_files_and_arguments_fail_atomically(self):
        for path, variants in [
            (self.source, [b'\xff', b'{', b'{"resourceSpans":[],"resourceSpans":[]}',
                           b'{"resourceSpans":[],"extra":false}',
                           b'{"resourceSpans":[{"scopeSpans":[{"spans":[{}]}]}]}']),
            (self.config, [b'\xff', b'processors: [broken', b'processors: {}',
                           b'processors: &x {tail_sampling: *x}',
                           self.config.read_bytes().replace(b'threshold_ms: 500', b'threshold_ms: -1'),
                           self.config.read_bytes().replace(b'tail_sampling:', b'tail_sampling:\n    decision_wait: 1'),
                           self.config.read_bytes().replace(b'name: slow', b'name: slow\n        name: duplicate')])]:
            original = path.read_bytes()
            for data in variants:
                path.write_bytes(data)
                with self.subTest(path=path.name, data=data):
                    self.assert_invalid()
            path.write_bytes(original)
        for path in (self.source, self.config, self.saved):
            raw = path.read_bytes()
            path.unlink()
            self.assert_invalid()
            path.write_bytes(raw)
        self.assert_invalid('--decision-wait-ms', '100')
        result = subprocess.run([sys.executable, '-m', 'otelwrench', 'tailsim', 'verify-replay-evidence', str(self.source),
                                 '--config', str(self.config)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')

    def test_provenance_remains_opaque_when_evidence_differs(self):
        changed = copy.deepcopy(self.original)
        changed.update(assumptions={}, byte_measure='untrusted', summary={}, traces=[None])
        self.save(changed)
        result = self.command('verify-replay')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['matches'])
        self.check(['assumptions', 'byte_measure', 'summary', 'traces'])
