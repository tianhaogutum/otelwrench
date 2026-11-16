"""Saved single-wait provenance checks through the real CLI."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'examples/tailsim' / 'arrival-order'
FIELDS = ('corpus_source_sha256', 'effective_config_sha256',
          'arrival_schedule_sha256', 'effective_arrival_model_sha256')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode()).hexdigest()


class ArrivalVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.config, self.arrivals, self.saved = [
            self.root / name for name in ('traces.json', 'policy.yaml', 'arrivals.json', 'report.json')]
        for target in (self.source, self.config, self.arrivals):
            target.write_bytes((FIXTURE / target.name).read_bytes())
        replay = self.command('replay-arrivals')
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.saved.write_text(replay.stdout, encoding='utf-8')
        self.original = json.loads(replay.stdout)

    def command(self, mode='verify-arrivals', wait=100):
        args = [sys.executable, '-m', 'otelwrench', 'tailsim', mode, str(self.source),
                '--config', str(self.config), '--arrivals', str(self.arrivals),
                '--decision-wait-ms', str(wait)]
        if mode == 'verify-arrivals':
            args += ['--report', str(self.saved)]
        return subprocess.run(args, capture_output=True, text=True)

    def report(self, mismatches=(), wait=100):
        result = self.command(wait=wait)
        self.assertEqual(result.returncode, int(bool(mismatches)), result.stderr)
        self.assertEqual(result.stderr, '')
        report = json.loads(result.stdout)
        self.assertEqual(set(report), {'schema_version', 'mode', 'fingerprint_version',
                                      'algorithm', 'matches', 'checks'})
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['mode'], 'arrival_provenance_verification')
        self.assertEqual(report['fingerprint_version'], 1)
        self.assertEqual(report['algorithm'], 'sha256')
        self.assertIs(report['matches'], not bool(mismatches))
        self.assertEqual(set(report['checks']), set(FIELDS))
        for field, check in report['checks'].items():
            self.assertEqual(set(check), {'reported_sha256', 'supplied_sha256', 'matches'})
            self.assertEqual(check['reported_sha256'], self.original['fingerprints'][field])
            self.assertIs(check['matches'], field not in mismatches)
            self.assertEqual(check['reported_sha256'] == check['supplied_sha256'], check['matches'])
        self.assertNotIn(str(self.root), result.stdout)
        return report

    def assert_invalid(self, wait=100):
        result = self.command(wait=wait)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_saved_cli_report_checks_four_independent_identities(self):
        report = self.report()
        schedule = json.loads(self.arrivals.read_text())
        schedule['observation_end_unix_nano'] = int(schedule['observation_end_unix_nano'])
        for row in schedule['arrivals']:
            row['at_unix_nano'] = int(row['at_unix_nano'])
        schedule['arrivals'].sort(key=lambda r: (r['at_unix_nano'], r['trace_id'], r['span_id']))
        expected = {
            'corpus_source_sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
            'effective_config_sha256': digest({'processors': {'tail_sampling': {'policies': [
                {'name': 'errors', 'type': 'status_code', 'status_code': {'status_codes': ['ERROR']}}]}}}),
            'arrival_schedule_sha256': digest(schedule),
            'effective_arrival_model_sha256': digest({'name': 'first_arrival_fixed_wait_v1', 'decision_wait_ms': 100})}
        self.assertEqual({k: v['supplied_sha256'] for k, v in report['checks'].items()}, expected)
        self.assertEqual(json.loads(self.saved.read_text()), self.original)

    def test_changed_inputs_report_only_corresponding_mismatches(self):
        for path, field in [(self.source, FIELDS[0]), (self.config, FIELDS[1]), (self.arrivals, FIELDS[2])]:
            original = path.read_bytes()
            if path == self.source:
                path.write_bytes(original + b'\n')
            elif path == self.config:
                path.write_bytes(original.replace(b'ERROR', b'OK'))
            else:
                data = json.loads(original)
                data['observation_end_unix_nano'] = '250000001'
                path.write_text(json.dumps(data))
            with self.subTest(field=field):
                self.report([field])
            path.write_bytes(original)
        self.report([FIELDS[3]], wait=101)
        self.source.write_bytes(self.source.read_bytes() + b'\n')
        self.report([FIELDS[0], FIELDS[3]], wait=101)

    def test_changed_schedule_timestamp_and_identity_are_mismatches(self):
        original = json.loads(self.arrivals.read_text())
        for field, value in [('at_unix_nano', '1'), ('trace_id', f'{5:032x}')]:
            data = copy.deepcopy(original)
            data['arrivals'][0][field] = value
            self.arrivals.write_text(json.dumps(data))
            with self.subTest(field=field):
                self.report([FIELDS[2]])

    def test_valid_changed_report_digest_is_a_mismatch(self):
        data = copy.deepcopy(self.original)
        data['fingerprints'][FIELDS[0]] = '0' * 64
        self.saved.write_text(json.dumps(data))
        result = self.command()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, '')
        report = json.loads(result.stdout)
        self.assertFalse(report['matches'])
        self.assertEqual(report['checks'][FIELDS[0]], {
            'reported_sha256': '0' * 64,
            'supplied_sha256': self.original['fingerprints'][FIELDS[0]], 'matches': False})
        self.assertTrue(all(report['checks'][key]['matches'] for key in FIELDS[1:]))

    def test_equivalent_config_and_schedule_encodings_match(self):
        self.config.write_text('''processors:
  tail_sampling:
    policies:
      - status_code: {status_codes: [ERROR, ERROR]}
        type: status_code
        name: errors
''')
        data = json.loads(self.arrivals.read_text())
        data['arrivals'].reverse()
        data['observation_end_unix_nano'] = int(data['observation_end_unix_nano'])
        for row in data['arrivals']:
            row['at_unix_nano'] = '000' + str(row['at_unix_nano'])
            row['trace_id'] = row['trace_id'].upper()
            row['span_id'] = row['span_id'].upper()
        self.arrivals.write_text(json.dumps(data, indent=4))
        self.report(wait='000100')

    def test_identity_check_does_not_authenticate_evidence(self):
        data = copy.deepcopy(self.original)
        data.update(model={'untrusted': True}, summary={}, assumptions={}, traces=[])
        self.saved.write_text(json.dumps(data))
        self.report()

    def test_bad_report_envelopes_and_fingerprints_fail_atomically(self):
        variants = []
        for key in self.original:
            data = copy.deepcopy(self.original)
            del data[key]
            variants.append(data)
        for key, value in [('schema_version', True), ('schema_version', '1'), ('schema_version', 2),
                           ('mode', 'arrival_order_offline_comparison'), ('extra', 1),
                           ('model', []), ('assumptions', None), ('summary', []), ('traces', {})]:
            variants.append({**self.original, key: value})
        for key, value in [('version', True), ('version', '1'), ('version', 2),
                           ('algorithm', 'SHA256'), ('extra', 1), (FIELDS[0], 'A' * 64),
                           (FIELDS[0], 'g' * 64), (FIELDS[0], '0' * 63), (FIELDS[0], None)]:
            variants.append({**self.original, 'fingerprints': {**self.original['fingerprints'], key: value}})
        for key in self.original['fingerprints']:
            data = copy.deepcopy(self.original)
            del data['fingerprints'][key]
            variants.append(data)
        variants += [[], None, {**self.original, 'fingerprints': []}]
        for i, data in enumerate(variants):
            self.saved.write_text(json.dumps(data))
            with self.subTest(variant=i):
                self.assert_invalid()
        for raw in [b'\xff', b'{', b'{"schema_version":1,"schema_version":1}', b'{"traces":NaN}']:
            self.saved.write_bytes(raw)
            self.assert_invalid()
        self.saved.unlink()
        self.assert_invalid()

    def test_invalid_supplied_inputs_and_deadlines_emit_no_verification(self):
        for path, raw in [(self.source, b'{'), (self.config, b'processors: {}'),
                          (self.arrivals, b'{"schema_version":2}')]:
            original = path.read_bytes()
            path.write_bytes(raw)
            self.assert_invalid()
            path.write_bytes(original)
        for wait in [0, -1, 'true', '1.5', (2**64 - 1) // 1_000_000 + 1]:
            self.assert_invalid(wait)
        data = json.loads(self.arrivals.read_text())
        data['arrivals'] = [{**data['arrivals'][0], 'at_unix_nano': str(2**64 - 1)}]
        data['observation_end_unix_nano'] = str(2**64 - 1)
        self.arrivals.write_text(json.dumps(data))
        self.assert_invalid()
