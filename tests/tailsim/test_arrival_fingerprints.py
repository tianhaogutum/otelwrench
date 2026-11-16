"""Arrival provenance preimages, computed independently of production hashing."""

import copy
import hashlib
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from otelwrench.tailsim.arrivals import replay_arrivals
from otelwrench.tailsim.validation import InputError
from .helpers import corpus, document, policies, policy, record
from .test_arrivals import event, schedule


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


class ArrivalFingerprints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source, self.config, self.arrivals = [root / n for n in ['λ.json', 'policy.yaml', 'arrivals.json']]
        self.source.write_bytes((json.dumps(document([
            record(trace=10, sid=10), record(trace=10, sid=11, status=2),
            record(trace=10, sid=12), record(trace=11, sid=10),
            record(trace=11, sid=11), record(trace=11, sid=13),
            record(trace=12, sid=10)]), indent=2, ensure_ascii=False) + '\r\n').encode('utf-8'))
        self.config.write_text('''processors:
  tail_sampling:
    policies:
      - {name: errors-λ, type: status_code, status_code: {status_codes: [OK, ERROR, ERROR]}}
      - {name: slow, type: latency, latency: {threshold_ms: 500}}
''', encoding='utf-8')
        self.manifest = schedule(event(trace=11, sid=11, at=10),
                                 event(trace=10, sid=12, at=10),
                                 event(trace=11, sid=10),
                                 event(trace=10, sid=11, at=10), end=200_000_000)
        # Hand-authored expected order: numeric time, trace ID, then span ID.
        self.normalized = {'schema_version': 1, 'observation_end_unix_nano': 200_000_000,
                           'arrivals': [
            {'at_unix_nano': 0, 'trace_id': '0000000000000000000000000000000b', 'span_id': '000000000000000a'},
            {'at_unix_nano': 10, 'trace_id': '0000000000000000000000000000000a', 'span_id': '000000000000000b'},
            {'at_unix_nano': 10, 'trace_id': '0000000000000000000000000000000a', 'span_id': '000000000000000c'},
            {'at_unix_nano': 10, 'trace_id': '0000000000000000000000000000000b', 'span_id': '000000000000000b'}]}
        self.arrivals.write_text(json.dumps(self.manifest))

    def command(self, command='replay-arrivals', wait='100', baseline=100, candidate=200):
        args = [sys.executable, '-m', 'otelwrench', 'tailsim', command, str(self.source), '--config', str(self.config)]
        if command == 'replay-arrivals':
            args += ['--arrivals', str(self.arrivals), '--decision-wait-ms', str(wait)]
        elif command == 'compare-arrivals':
            args += ['--arrivals', str(self.arrivals), '--baseline-wait-ms', str(baseline),
                     '--candidate-wait-ms', str(candidate)]
        return subprocess.run(args, capture_output=True, text=True)

    def report(self, **kwargs):
        result = self.command(**kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_saved_comparison_labels_exact_preimages_and_embedded_replays(self):
        result = self.command(command='compare-arrivals', baseline='000100')
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = self.source.parent / 'comparison.json'
        saved.write_text(result.stdout, encoding='utf-8')
        report = json.loads(saved.read_text(encoding='utf-8'))
        fingerprints = report['fingerprints']
        normalized_config = {'processors': {'tail_sampling': {'policies': [
            {'name': 'errors-λ', 'type': 'status_code', 'status_code': {'status_codes': ['ERROR', 'OK']}},
            {'name': 'slow', 'type': 'latency', 'latency': {'threshold_ms': 500, 'upper_threshold_ms': 0}}]}}}
        shared = {'version': 1, 'algorithm': 'sha256',
                  'corpus_source_sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                  'effective_config_sha256': digest(normalized_config),
                  'arrival_schedule_sha256': digest(self.normalized)}
        models = {side + '_effective_arrival_model_sha256': digest({
            'name': 'first_arrival_fixed_wait_v1', 'decision_wait_ms': wait})
            for side, wait in [('baseline', 100), ('candidate', 200)]}
        self.assertEqual(fingerprints, {**shared, **models})
        for side, wait in [('baseline', 100), ('candidate', 200)]:
            self.assertEqual(report[side], self.report(wait=wait))
            self.assertEqual(report[side]['fingerprints'], {
                **shared, 'effective_arrival_model_sha256': models[side + '_effective_arrival_model_sha256']})
        static = self.report(command='replay')['fingerprints']
        self.assertEqual({key: fingerprints[key] for key in static}, static)
        self.assertNotIn(str(self.source.parent), result.stdout)

    def test_comparison_equal_and_reversed_models_even_with_equal_decisions(self):
        forward = self.report(command='compare-arrivals', candidate=101)
        reverse = self.report(command='compare-arrivals', baseline=101, candidate=100)
        equal = self.report(command='compare-arrivals', candidate=100)
        self.assertEqual(forward['comparison']['changed_decision_count'], 0)
        self.assertEqual(forward['comparison']['state_change_count'], 0)
        self.assertEqual(forward['baseline']['traces'][0]['decision_snapshot']['decision'],
                         forward['candidate']['traces'][0]['decision_snapshot']['decision'])
        a, b, c = [r['fingerprints'] for r in [forward, reverse, equal]]
        baseline = 'baseline_effective_arrival_model_sha256'
        candidate = 'candidate_effective_arrival_model_sha256'
        self.assertNotEqual(a[baseline], a[candidate])
        self.assertEqual(a[baseline], b[candidate])
        self.assertEqual(a[candidate], b[baseline])
        self.assertEqual(c[baseline], c[candidate])
        for ids in [b, c]:
            for key in ['corpus_source_sha256', 'effective_config_sha256', 'arrival_schedule_sha256']:
                self.assertEqual(a[key], ids[key])
        self.assertEqual(forward['baseline'], reverse['candidate'])
        self.assertEqual(forward['candidate'], reverse['baseline'])

    def test_comparison_schedule_normalization_and_changed_identities(self):
        original = self.report(command='compare-arrivals')
        encoded = {'arrivals': [
            {'span_id': r['span_id'].upper(), 'trace_id': r['trace_id'].upper(),
             'at_unix_nano': '000' + str(r['at_unix_nano'])}
            for r in reversed(self.normalized['arrivals'])],
            'observation_end_unix_nano': '000200000000', 'schema_version': 1}
        self.arrivals.write_text(json.dumps(encoded, indent=4))
        self.assertEqual(self.report(command='compare-arrivals'), original)
        variants = []
        for field, value in [('at_unix_nano', 11), ('trace_id', f'{10:032x}'), ('span_id', f'{13:016x}')]:
            changed = copy.deepcopy(self.normalized)
            changed['arrivals'][0][field] = value
            variants.append(changed)
        changed = copy.deepcopy(self.normalized)
        changed['observation_end_unix_nano'] += 1
        variants.append(changed)
        for changed in variants:
            self.arrivals.write_text(json.dumps(changed))
            report = self.report(command='compare-arrivals')
            changed['arrivals'].sort(key=lambda r: (r['at_unix_nano'], r['trace_id'], r['span_id']))
            ids = report['fingerprints']
            self.assertEqual(ids['arrival_schedule_sha256'], digest(changed))
            self.assertNotEqual(ids['arrival_schedule_sha256'], original['fingerprints']['arrival_schedule_sha256'])
            for side in ['baseline', 'candidate']:
                self.assertEqual(ids['arrival_schedule_sha256'], report[side]['fingerprints']['arrival_schedule_sha256'])
                self.assertEqual(ids[side + '_effective_arrival_model_sha256'],
                                 original['fingerprints'][side + '_effective_arrival_model_sha256'])

    def test_saved_cli_exact_preimages_and_static_identity(self):
        result = self.command(wait='000100')
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = self.source.parent / 'report.json'
        saved.write_text(result.stdout, encoding='utf-8')
        fingerprints = json.loads(saved.read_text())['fingerprints']
        normalized_config = {'processors': {'tail_sampling': {'policies': [
            {'name': 'errors-λ', 'type': 'status_code', 'status_code': {'status_codes': ['ERROR', 'OK']}},
            {'name': 'slow', 'type': 'latency', 'latency': {'threshold_ms': 500, 'upper_threshold_ms': 0}}]}}}
        self.assertEqual(fingerprints, {
            'version': 1, 'algorithm': 'sha256',
            'corpus_source_sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
            'effective_config_sha256': digest(normalized_config),
            'arrival_schedule_sha256': digest(self.normalized),
            'effective_arrival_model_sha256': digest({'name': 'first_arrival_fixed_wait_v1', 'decision_wait_ms': 100})})
        static = self.report(command='replay')['fingerprints']
        self.assertEqual({k: fingerprints[k] for k in static}, static)
        self.assertNotIn(str(self.source.parent), result.stdout)
        self.source.rename(self.source.with_name('moved.json'))
        self.source = self.source.with_name('moved.json')
        self.assertEqual(self.report()['fingerprints'], fingerprints)

    def test_schedule_format_case_times_and_all_row_permutations(self):
        original = self.report()
        # Exercise all tie orders; the corpus bytes are held fixed.
        for order in itertools.permutations(self.manifest['arrivals']):
            manifest = {'arrivals': [], 'observation_end_unix_nano': 200_000_000, 'schema_version': 1}
            for i, row in enumerate(order):
                manifest['arrivals'].append({'span_id': row['span_id'].upper(),
                    'trace_id': row['trace_id'].upper(),
                    'at_unix_nano': int(row['at_unix_nano']) if i % 2 else '000' + row['at_unix_nano']})
            self.arrivals.write_text(json.dumps(manifest, indent=4))
            with self.subTest(order=order):
                self.assertEqual(self.report(), original)

    def test_changed_schedule_preimages_include_cutoff_and_identities(self):
        original = self.report()['fingerprints']
        variants = []
        for field, value in [('at_unix_nano', 11), ('trace_id', f'{10:032x}'), ('span_id', f'{13:016x}')]:
            changed = copy.deepcopy(self.normalized)
            changed['arrivals'][0][field] = value
            variants.append(changed)
        changed = copy.deepcopy(self.normalized)
        changed['observation_end_unix_nano'] += 1
        variants.append(changed)
        changed = copy.deepcopy(self.normalized)
        changed['arrivals'].pop()
        variants.append(changed)
        hashes = {original['arrival_schedule_sha256']}
        for changed in variants:
            self.arrivals.write_text(json.dumps(changed))
            actual = self.report()['fingerprints']
            ordered = copy.deepcopy(changed)
            ordered['arrivals'].sort(key=lambda r: (r['at_unix_nano'], r['trace_id'], r['span_id']))
            self.assertEqual(actual['arrival_schedule_sha256'], digest(ordered))
            self.assertNotIn(actual['arrival_schedule_sha256'], hashes)
            hashes.add(actual['arrival_schedule_sha256'])
            for field in ['effective_arrival_model_sha256', 'corpus_source_sha256', 'effective_config_sha256']:
                self.assertEqual(actual[field], original[field])

    def test_wait_changes_model_even_when_final_decisions_equal(self):
        before, after = self.report(), self.report(wait=101)
        self.assertEqual(before['summary'], after['summary'])
        self.assertEqual([r['decision_snapshot'] for r in before['traces']],
                         [r['decision_snapshot'] for r in after['traces']])
        a, b = before['fingerprints'], after['fingerprints']
        self.assertNotEqual(a['effective_arrival_model_sha256'], b['effective_arrival_model_sha256'])
        self.assertEqual(b['effective_arrival_model_sha256'],
                         digest({'name': 'first_arrival_fixed_wait_v1', 'decision_wait_ms': 101}))
        for field in a.keys() - {'effective_arrival_model_sha256'}:
            self.assertEqual(a[field], b[field])

    def test_full_schedule_hash_includes_late_and_pending_rows(self):
        source = corpus([record(sid=10), record(sid=11), record(trace=2)])
        parsed = policies(policy('always_sample'))
        manifest = schedule(event(sid=10), event(sid=11, at=100_000_000),
                            event(trace=2, at=200_000_000))
        report = replay_arrivals(source, parsed, manifest, 100)
        self.assertEqual([r['state'] for r in report['traces']], ['decided', 'pending'])
        self.assertEqual(report['summary']['late_spans'], 1)
        expected = copy.deepcopy(manifest)
        expected['observation_end_unix_nano'] = 250_000_000
        for row in expected['arrivals']:
            row['at_unix_nano'] = int(row['at_unix_nano'])
        self.assertEqual(report['fingerprints']['arrival_schedule_sha256'], digest(expected))

    def test_empty_inputs_and_uint64_limits_have_integer_preimages(self):
        parsed = policies(policy('always_sample'))
        for wait in [1, (2**64 - 1) // 1_000_000]:
            empty = replay_arrivals(corpus([]), parsed, schedule(end=0), wait)
            self.assertEqual(empty['fingerprints']['arrival_schedule_sha256'], digest({
                'schema_version': 1, 'observation_end_unix_nano': 0, 'arrivals': []}))
            self.assertEqual(empty['fingerprints']['effective_arrival_model_sha256'], digest({
                'name': 'first_arrival_fixed_wait_v1', 'decision_wait_ms': wait}))
        manifest = schedule(event(at=2**64 - 1 - 1_000_000), end=2**64 - 1)
        maximum = replay_arrivals(corpus([record()]), parsed, manifest, 1)
        self.assertEqual(maximum['traces'][0]['state'], 'decided')
        self.assertEqual(maximum['traces'][0]['deadline_unix_nano'], 2**64 - 1)
        manifest['observation_end_unix_nano'] = 2**64 - 1
        manifest['arrivals'][0]['at_unix_nano'] = 2**64 - 1 - 1_000_000
        self.assertEqual(maximum['fingerprints']['arrival_schedule_sha256'], digest(manifest))
        for wait in [True, False, 0, -1, 1.0, '1', (2**64 - 1) // 1_000_000 + 1]:
            with self.subTest(wait=wait), self.assertRaises(InputError):
                replay_arrivals(corpus([]), parsed, schedule(), wait)

    def test_normalization_keeps_cli_failures_atomic(self):
        valid = copy.deepcopy(self.manifest)
        for manifest in [{**valid, 'schema_version': '1'}, {**valid, 'extra': False},
                         {**valid, 'arrivals': valid['arrivals'] + [valid['arrivals'][0]]},
                         {**valid, 'arrivals': [event(trace=99)]},
                         {**valid, 'arrivals': [{**valid['arrivals'][0], 'at_unix_nano': True}]}]:
            self.arrivals.write_text(json.dumps(manifest))
            result = self.command()
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertNotIn('Traceback', result.stderr)
        for raw in ['{', '{"schema_version":1,"schema_version":1}']:
            self.arrivals.write_text(raw)
            result = self.command()
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
        self.arrivals.write_text(json.dumps(valid))
        for wait in [0, -1, '1.5', 'true', 2**64]:
            result = self.command(wait=wait)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
        self.arrivals.write_text(json.dumps(schedule(event(trace=10, sid=10, at=2**64 - 1), end=2**64 - 1)))
        result = self.command()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn('deadline', result.stderr)
