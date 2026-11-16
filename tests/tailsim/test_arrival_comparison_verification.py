"""Saved paired-wait provenance, checked through real CLI subprocesses."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from .test_arrival_verification import FIELDS, FIXTURE, digest

WRAPPER = (*FIELDS[:3], 'baseline_effective_arrival_model_sha256',
           'candidate_effective_arrival_model_sha256')


class ArrivalComparisonVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.config, self.arrivals, self.saved = [
            self.root / name for name in ('traces.json', 'policy.yaml', 'arrivals.json', 'report.json')]
        for target in (self.source, self.config, self.arrivals):
            target.write_bytes((FIXTURE / target.name).read_bytes())
        result = self.command('compare-arrivals')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.saved.write_text(result.stdout, encoding='utf-8')
        self.original = json.loads(result.stdout)

    def command(self, mode='verify-compare-arrivals', baseline=100, candidate=200):
        args = [sys.executable, '-m', 'otelwrench', 'tailsim', mode, str(self.source),
                '--config', str(self.config), '--arrivals', str(self.arrivals),
                '--baseline-wait-ms', str(baseline), '--candidate-wait-ms', str(candidate)]
        if mode == 'verify-compare-arrivals':
            args += ['--report', str(self.saved)]
        return subprocess.run(args, capture_output=True, text=True)

    def report(self, mismatches=(), baseline=100, candidate=200):
        result = self.command(baseline=baseline, candidate=candidate)
        self.assertEqual(result.returncode, int(bool(mismatches)), result.stderr)
        self.assertEqual(result.stderr, '')
        report = json.loads(result.stdout)
        self.assertEqual(set(report), {'schema_version', 'mode', 'fingerprint_version',
                                      'algorithm', 'matches', 'checks'})
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['mode'], 'arrival_comparison_provenance_verification')
        self.assertEqual(report['fingerprint_version'], 1)
        self.assertEqual(report['algorithm'], 'sha256')
        self.assertIs(report['matches'], not bool(mismatches))
        self.assertEqual(set(report['checks']), {'fingerprints', 'baseline', 'candidate'})
        for section, checks in report['checks'].items():
            self.assertEqual(set(checks), set(WRAPPER if section == 'fingerprints' else FIELDS))
            original = self.original[section] if section == 'fingerprints' else self.original[section]['fingerprints']
            for field, check in checks.items():
                self.assertEqual(set(check), {'reported_sha256', 'supplied_sha256', 'matches'})
                self.assertEqual(check['reported_sha256'], original[field])
                self.assertIs(check['matches'], (section, field) not in mismatches)
                self.assertEqual(check['reported_sha256'] == check['supplied_sha256'], check['matches'])
        self.assertNotIn(str(self.root), result.stdout)
        return report

    def assert_invalid(self, **waits):
        result = self.command(**waits)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_saved_comparison_checks_thirteen_independent_identities(self):
        report = self.report()
        schedule = json.loads(self.arrivals.read_text())
        schedule['observation_end_unix_nano'] = int(schedule['observation_end_unix_nano'])
        for row in schedule['arrivals']:
            row['at_unix_nano'] = int(row['at_unix_nano'])
        schedule['arrivals'].sort(key=lambda r: (r['at_unix_nano'], r['trace_id'], r['span_id']))
        shared = dict(zip(FIELDS[:3], [hashlib.sha256(self.source.read_bytes()).hexdigest(),
            digest({'processors': {'tail_sampling': {'policies': [
                {'name': 'errors', 'type': 'status_code', 'status_code': {'status_codes': ['ERROR']}}]}}}),
            digest(schedule)]))
        model = lambda wait: digest({'name': 'first_arrival_fixed_wait_v1', 'decision_wait_ms': wait})
        expected = {'fingerprints': {**shared, WRAPPER[3]: model(100), WRAPPER[4]: model(200)},
                    'baseline': {**shared, FIELDS[3]: model(100)},
                    'candidate': {**shared, FIELDS[3]: model(200)}}
        self.assertEqual({s: {k: c['supplied_sha256'] for k, c in checks.items()}
                          for s, checks in report['checks'].items()}, expected)
        self.assertEqual(json.loads(self.saved.read_text()), self.original)

    def test_each_supplied_input_changes_corresponding_checks(self):
        for path, field in [(self.source, FIELDS[0]), (self.config, FIELDS[1]), (self.arrivals, FIELDS[2])]:
            raw = path.read_bytes()
            if path == self.source:
                variants = [raw + b'\n']
            elif path == self.config:
                variants = [raw.replace(b'ERROR', b'OK')]
            else:
                data = json.loads(raw)
                variants = []
                changed = copy.deepcopy(data)
                changed['observation_end_unix_nano'] = '250000001'
                variants.append(json.dumps(changed).encode())
                for key, value in [('at_unix_nano', '1'), ('trace_id', f'{5:032x}')]:
                    changed = copy.deepcopy(data)
                    changed['arrivals'][0][key] = value
                    variants.append(json.dumps(changed).encode())
            for variant in variants:
                path.write_bytes(variant)
                with self.subTest(field=field, variant=variant):
                    self.report([(s, field) for s in ('fingerprints', 'baseline', 'candidate')])
            path.write_bytes(raw)
        self.report([('fingerprints', WRAPPER[3]), ('baseline', FIELDS[3])], baseline=101)
        self.report([('fingerprints', WRAPPER[4]), ('candidate', FIELDS[3])], candidate=201)

    def test_every_wrapper_and_embedded_digest_is_checked(self):
        original = copy.deepcopy(self.original)
        locations = [(s, k) for s in ('fingerprints', 'baseline', 'candidate')
                     for k in (WRAPPER if s == 'fingerprints' else FIELDS)]
        for section, field in locations:
            self.original = copy.deepcopy(original)
            block = self.original[section] if section == 'fingerprints' else self.original[section]['fingerprints']
            block[field] = '0' * 64
            self.saved.write_text(json.dumps(self.original))
            with self.subTest(section=section, field=field):
                self.report([(section, field)])
        self.original = copy.deepcopy(original)
        for section, field in locations:
            block = self.original[section] if section == 'fingerprints' else self.original[section]['fingerprints']
            block[field] = '0' * 64
        self.saved.write_text(json.dumps(self.original))
        self.report(locations)

    def test_equivalent_encodings_and_equal_reversed_waits(self):
        self.config.write_text('''processors:
  tail_sampling:
    policies:
      - status_code: {status_codes: [ERROR, ERROR]}
        type: status_code
        name: errors
''')
        schedule = json.loads(self.arrivals.read_text())
        schedule['observation_end_unix_nano'] = int(schedule['observation_end_unix_nano'])
        schedule['arrivals'].reverse()
        for row in schedule['arrivals']:
            row.update(at_unix_nano=int(row['at_unix_nano']),
                       trace_id=row['trace_id'].upper(), span_id=row['span_id'].upper())
        self.arrivals.write_text(json.dumps(schedule, indent=4))
        self.report(baseline='000100', candidate='000200')
        self.report([('fingerprints', WRAPPER[3]), ('baseline', FIELDS[3]),
                     ('fingerprints', WRAPPER[4]), ('candidate', FIELDS[3])], baseline=200, candidate=100)
        for baseline, candidate in [(200, 100), (100, 100)]:
            result = self.command('compare-arrivals', baseline, candidate)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.original = json.loads(result.stdout)
            self.saved.write_text(result.stdout)
            self.report(baseline=baseline, candidate=candidate)

    def test_evidence_is_opaque_and_never_used_as_input(self):
        self.original['assumptions'] = {}
        self.original['comparison'] = {'untrusted': True}
        for side in ('baseline', 'candidate'):
            self.original[side].update(model={'decision_wait_ms': -1}, assumptions={}, summary={}, traces=[])
        self.saved.write_text(json.dumps(self.original))
        self.report()

    def test_malformed_wrapper_and_embedded_blocks_fail_atomically(self):
        variants = [None, [], self.original['baseline']]
        for section in (None, 'baseline', 'candidate'):
            block = self.original if section is None else self.original[section]
            mutations = []
            for key in block:
                changed = copy.deepcopy(block)
                del changed[key]
                mutations.append(changed)
            for key, value in [('schema_version', True), ('schema_version', '1'), ('schema_version', 2),
                               ('mode', 'unsupported'), ('extra', 1), ('assumptions', []),
                               ('comparison', []), ('fingerprints', []), ('fingerprints', None)]:
                mutations.append({**block, key: value})
            fields = WRAPPER if section is None else FIELDS
            for key in block['fingerprints']:
                changed = copy.deepcopy(block)
                del changed['fingerprints'][key]
                mutations.append(changed)
            for key, value in [('version', True), ('version', '1'), ('version', 2),
                               ('algorithm', 'SHA256'), ('algorithm', 'sha512'), ('extra', 1)]:
                mutations.append({**block, 'fingerprints': {**block['fingerprints'], key: value}})
            for field in fields:
                for value in ['A' * 64, 'g' * 64, '0' * 63, None]:
                    mutations.append({**block, 'fingerprints': {**block['fingerprints'], field: value}})
            for mutation in mutations:
                variants.append(mutation if section is None else {**self.original, section: mutation})
        for section in ('baseline', 'candidate'):
            variants += [{**self.original, section: value} for value in ([], None, {})]
        for i, data in enumerate(variants):
            self.saved.write_text(json.dumps(data))
            with self.subTest(variant=i):
                self.assert_invalid()
        for raw in [b'\xff', b'{', b'{"schema_version":1,"schema_version":1}',
                    json.dumps(self.original).replace('"comparison": {', '"comparison": {"bad":NaN,').encode()]:
            self.saved.write_bytes(raw)
            self.assert_invalid()
        self.saved.unlink()
        self.assert_invalid()

    def test_invalid_supplied_inputs_waits_and_overflow_on_either_side(self):
        for path, raw in [(self.source, b'{'), (self.config, b'processors: {}'),
                          (self.arrivals, b'{"schema_version":2}')]:
            original = path.read_bytes()
            path.write_bytes(raw)
            self.assert_invalid()
            path.write_bytes(original)
        for side in ('baseline', 'candidate'):
            for wait in [0, -1, 'true', '1.5', (2**64 - 1) // 1_000_000 + 1]:
                with self.subTest(side=side, wait=wait):
                    self.assert_invalid(**{side: wait})
        data = json.loads(self.arrivals.read_text())
        data['observation_end_unix_nano'] = str(2**64 - 1)
        data['arrivals'] = [{**data['arrivals'][0], 'at_unix_nano': str(2**64 - 1 - 200_000_000)}]
        self.arrivals.write_text(json.dumps(data))
        # A deadline equal to uint64 max is valid (identities simply mismatch).
        result = self.command()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(result.stdout)
        self.assert_invalid(candidate=201)
        self.assert_invalid(baseline=201)
        self.source.unlink()
        self.assert_invalid()
