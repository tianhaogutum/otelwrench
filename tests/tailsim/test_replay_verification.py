"""Saved static provenance through real CLI subprocesses and independent hashes."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from .test_fingerprints import config, digest

ROOT = Path(__file__).resolve().parents[2]
FIELDS = ('corpus_source_sha256', 'effective_config_sha256')


class ReplayVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.config, self.saved = [self.root / name for name in
                                               ('traces.json', 'policy.yaml', 'report.json')]
        self.source.write_bytes((ROOT / 'examples/tailsim/traces.json').read_bytes())
        self.config.write_text('''processors: {tail_sampling: {policies: [
          {name: all-λ, type: always_sample},
          {name: slow, type: latency, latency: {threshold_ms: 500}},
          {name: errors, type: status_code, status_code: {status_codes: [OK, ERROR, ERROR]}},
          {name: service, type: string_attribute, string_attribute: {key: service, values: [λ, api, api]}}
        ]}}''', encoding='utf-8')
        self.expected_config = config([
            {'name': 'all-λ', 'type': 'always_sample', 'always_sample': {}},
            {'name': 'slow', 'type': 'latency', 'latency': {'threshold_ms': 500, 'upper_threshold_ms': 0}},
            {'name': 'errors', 'type': 'status_code', 'status_code': {'status_codes': ['ERROR', 'OK']}},
            {'name': 'service', 'type': 'string_attribute', 'string_attribute': {'key': 'service', 'values': ['api', 'λ']}}])
        result = self.command('replay')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.original = json.loads(result.stdout)
        self.saved.write_text(result.stdout, encoding='utf-8')

    def command(self, mode='verify-replay', *extra):
        args = [sys.executable, '-m', 'otelwrench', 'tailsim', mode, str(self.source), '--config', str(self.config)]
        if mode == 'verify-replay':
            args += ['--report', str(self.saved)]
        return subprocess.run([*args, *extra], capture_output=True, text=True)

    def report(self, mismatches=()):
        result = self.command()
        self.assertEqual(result.returncode, int(bool(mismatches)), result.stderr)
        self.assertEqual(result.stderr, '')
        expected = {'corpus_source_sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                    'effective_config_sha256': digest(self.expected_config)}
        checks = {key: {'reported_sha256': self.original['fingerprints'][key],
                        'supplied_sha256': expected[key], 'matches': key not in mismatches}
                  for key in FIELDS}
        self.assertEqual(json.loads(result.stdout), {
            'schema_version': 1, 'mode': 'replay_provenance_verification',
            'fingerprint_version': 1, 'algorithm': 'sha256',
            'matches': not bool(mismatches), 'checks': checks})
        for check in checks.values():
            self.assertEqual(check['reported_sha256'] == check['supplied_sha256'], check['matches'])
        self.assertNotIn(str(self.root), result.stdout)

    def assert_invalid(self, *extra):
        result = self.command('verify-replay', *extra)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_saved_cli_report_checks_two_independent_identities(self):
        self.report()
        self.assertEqual(json.loads(self.saved.read_text()), self.original)

    def test_source_config_and_valid_saved_digest_mismatches(self):
        self.source.write_bytes(self.source.read_bytes() + b'\n')
        self.report([FIELDS[0]])
        self.config.write_text(self.config.read_text().replace('500', '501'), encoding='utf-8')
        self.expected_config['processors']['tail_sampling']['policies'][1]['latency']['threshold_ms'] = 501
        self.report(FIELDS)
        self.source.write_bytes((ROOT / 'examples/tailsim/traces.json').read_bytes())
        self.report([FIELDS[1]])
        self.config.write_text(self.config.read_text().replace('501', '500'), encoding='utf-8')
        self.expected_config['processors']['tail_sampling']['policies'][1]['latency']['threshold_ms'] = 500
        original = copy.deepcopy(self.original)
        for changed in [(FIELDS[0],), (FIELDS[1],), FIELDS]:
            self.original = copy.deepcopy(original)
            for field in changed:
                self.original['fingerprints'][field] = '0' * 64
            self.saved.write_text(json.dumps(self.original))
            with self.subTest(changed=changed):
                self.report(changed)

    def test_equivalent_config_and_moved_inputs_keep_identity(self):
        # JSON is supported YAML; normalized defaults/sets and escaped Unicode.
        self.config.write_text(json.dumps(self.expected_config, sort_keys=True, indent=4))
        for name in ('source', 'config'):
            old = getattr(self, name)
            moved = old.with_name('moved-' + old.name)
            old.rename(moved)
            setattr(self, name, moved)
        self.report()

    def test_empty_corpus_and_opaque_unauthenticated_evidence(self):
        self.original.update(assumptions={}, byte_measure='untrusted', summary={}, traces=[None])
        self.saved.write_text(json.dumps(self.original))
        self.report()
        self.source.write_bytes(b'{"resourceSpans":[]}')
        result = self.command('replay')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.original = json.loads(result.stdout)
        self.saved.write_text(result.stdout)
        self.report()

    def test_malformed_envelopes_fingerprints_and_json_fail_atomically(self):
        variants = [None, [], {}]
        for key in self.original:
            changed = copy.deepcopy(self.original)
            del changed[key]
            variants.append(changed)
        for key, value in [('schema_version', True), ('schema_version', '1'), ('schema_version', 2),
                           ('mode', 'complete_trace_offline_comparison'), ('mode', 'arrival_order_offline'),
                           ('extra', 1), ('assumptions', None), ('summary', []),
                           ('traces', {}), ('byte_measure', {}), ('fingerprints', [])]:
            variants.append({**self.original, key: value})
        for key in self.original['fingerprints']:
            changed = copy.deepcopy(self.original)
            del changed['fingerprints'][key]
            variants.append(changed)
        for key, value in [('version', True), ('version', '1'), ('version', 2),
                           ('algorithm', 'SHA256'), ('algorithm', 'sha512'), ('extra', 1)]:
            variants.append({**self.original, 'fingerprints': {**self.original['fingerprints'], key: value}})
        for field in FIELDS:
            for value in ['A' * 64, 'g' * 64, '0' * 63, '0' * 65, None, 0, True]:
                variants.append({**self.original, 'fingerprints': {**self.original['fingerprints'], field: value}})
        for i, changed in enumerate(variants):
            self.saved.write_text(json.dumps(changed))
            with self.subTest(variant=i):
                self.assert_invalid()
        raw = json.dumps(self.original)
        for data in [b'\xff', b'{', b'{"schema_version":1,"schema_version":1}',
                     raw.replace('"summary": {', '"summary": {"x":NaN,').encode(),
                     raw.replace('"summary": {', '"summary": {"x":1,"x":2,').encode(),
                     raw.replace('"version": 1', '"version":1,"version":1').encode()]:
            self.saved.write_bytes(data)
            self.assert_invalid()

    def test_invalid_source_config_files_and_arguments_fail_atomically(self):
        for path, variants in [
                (self.source, [b'\xff', b'{', b'{"resourceSpans":[],"resourceSpans":[]}',
                               b'{"resourceSpans":[],"unsupported":false}',
                               b'{"resourceSpans":[{"scopeSpans":[{"spans":[{}]}]}]}']),
                (self.config, [b'\xff', b'processors: [broken', b'processors: {}',
                               b'processors: &x {tail_sampling: *x}',
                               self.config.read_bytes().replace(b'threshold_ms: 500', b'threshold_ms: -1'),
                               self.config.read_bytes().replace(b'tail_sampling: {', b'tail_sampling: {decision_wait: 1,'),
                               self.config.read_bytes().replace(b'name: slow', b'name: slow, name: duplicate')])]:
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
        result = subprocess.run([sys.executable, '-m', 'otelwrench', 'tailsim', 'verify-replay', str(self.source),
                                 '--config', str(self.config)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
