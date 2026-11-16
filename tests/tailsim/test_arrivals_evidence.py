"""Real CLI regeneration checks with an independent finite arrival oracle."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'examples/tailsim/arrival-order'
FIELDS = ('schema_version', 'mode', 'fingerprints', 'model', 'assumptions', 'summary', 'traces')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class ArrivalsEvidence(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source, self.config, self.arrivals, self.saved = [self.root / n for n in
            ('traces.json', 'policy.yaml', 'arrivals.json', 'report.json')]
        for path in (self.source, self.config, self.arrivals):
            path.write_bytes((FIXTURE / path.name).read_bytes())
        self.produce()

    def command(self, mode='verify-arrivals-evidence', wait='100', *extra):
        cli = [os.environ['TAILSIM_TEST_CLI']] if 'TAILSIM_TEST_CLI' in os.environ else [sys.executable, '-m', 'otelwrench', 'tailsim']
        args = [*cli, mode, str(self.source), '--config', str(self.config),
                '--arrivals', str(self.arrivals), '--decision-wait-ms', str(wait)]
        if mode != 'replay-arrivals':
            args += ['--report', str(self.saved)]
        return subprocess.run([*args, *extra], capture_output=True, text=True)

    def produce(self, wait='100'):
        result = self.command('replay-arrivals', wait)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.original = json.loads(result.stdout)
        self.saved.write_text(result.stdout)

    def save(self, value):
        self.saved.write_text(json.dumps(value, ensure_ascii=False))

    def check(self, mismatches=(), wait='100'):
        result = self.command(wait=wait)
        self.assertEqual(result.returncode, int(bool(mismatches)), result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertEqual(json.loads(result.stdout), {
            'schema_version': 1, 'mode': 'arrival_evidence_verification',
            'matches': not bool(mismatches),
            'checks': {key: {'matches': key not in mismatches} for key in FIELDS}})
        self.assertNotIn(str(self.root), result.stdout)

    def invalid(self, wait='100', *extra):
        result = self.command('verify-arrivals-evidence', wait, *extra)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def assert_oracle(self, wait=100):
        # No production imports or saved assertions drive this oracle. Reference
        # identities come from raw OTLP; expected decisions are ERROR membership.
        spans = {}
        for resource in json.loads(self.source.read_bytes())['resourceSpans']:
            for scope in resource['scopeSpans']:
                for span in scope['spans']:
                    spans.setdefault(span['traceId'].lower(), {})[span['spanId'].lower()] = span
        manifest = json.loads(self.arrivals.read_bytes())
        cutoff = int(manifest['observation_end_unix_nano'])
        events = [{**r, 'trace_id': r['trace_id'].lower(), 'span_id': r['span_id'].lower(),
                   'at_unix_nano': int(r['at_unix_nano'])} for r in manifest['arrivals']]
        def decision(tid, ids):
            selected = [spans[tid][sid] for sid in ids]
            statuses = sorted({{0: 'UNSET', 1: 'OK', 2: 'ERROR'}[s['status']['code']] for s in selected})
            sample = 'ERROR' in statuses
            duration = max(int(s['endTimeUnixNano']) for s in selected) - min(int(s['startTimeUnixNano']) for s in selected)
            return {'trace_id': tid, 'duration_ns': duration, 'duration_ms_floor': duration // 1_000_000,
                    'error': sample, 'statuses': statuses, 'span_count': len(ids),
                    'kept_span_count': len(ids) if sample else 0, 'decision': 'sample' if sample else 'drop',
                    'reason': 'sample_policy_match' if sample else 'no_sample_policy_match',
                    'matched_policy_names': ['errors'] if sample else [],
                    'policies': [{'name': 'errors', 'type': 'status_code', 'matched': sample,
                                  'decision': 'sampled' if sample else 'not_sampled', 'children': [],
                                  'reason': "matching observed statuses: ['ERROR']" if sample else 'matching observed statuses: []'}]}
        rows = []
        for tid, inventory in sorted(spans.items()):
            times = {r['span_id']: r['at_unix_nano'] for r in events if r['trace_id'] == tid}
            first = min(times.values()) if times else None
            deadline = None if first is None else first + wait * 1_000_000
            state = 'unobserved' if first is None else 'pending' if cutoff < deadline else 'decided'
            ontime = sorted(sid for sid, at in times.items() if at < deadline)
            snapshot = decision(tid, ontime) if state == 'decided' else None
            full = decision(tid, sorted(inventory))
            rows.append({'trace_id': tid, 'state': state, 'first_arrival_unix_nano': first,
                         'deadline_unix_nano': deadline, 'on_time_span_ids': ontime,
                         'late_span_ids': sorted(set(times) - set(ontime)),
                         'missing_span_ids': sorted(set(inventory) - set(times)),
                         'unseen_at_decision_span_ids': sorted(set(inventory) - set(ontime)) if snapshot else None,
                         'decision_snapshot': snapshot, 'full_trace': full,
                         'decision_differs_from_full_trace': snapshot['decision'] != full['decision'] if snapshot else None})
        summary = {'reference_traces': len(spans), 'reference_spans': sum(map(len, spans.values())),
                   'arrived_spans': len(events), 'late_spans': sum(len(r['late_span_ids']) for r in rows),
                   'missing_spans': sum(len(r['missing_span_ids']) for r in rows),
                   'decided_traces': sum(r['state'] == 'decided' for r in rows),
                   'pending_traces': sum(r['state'] == 'pending' for r in rows),
                   'unobserved_traces': sum(r['state'] == 'unobserved' for r in rows),
                   'incomplete_decisions': sum(bool(r['unseen_at_decision_span_ids']) for r in rows),
                   'sampled_traces': sum(bool(r['decision_snapshot']) and r['decision_snapshot']['decision'] == 'sample' for r in rows),
                   'kept_spans': sum(r['decision_snapshot']['kept_span_count'] for r in rows if r['decision_snapshot']),
                   'changed_decision_trace_ids': [r['trace_id'] for r in rows if r['decision_differs_from_full_trace']]}
        config = {'processors': {'tail_sampling': {'policies': [
            {'name': 'errors', 'type': 'status_code', 'status_code': {'status_codes': ['ERROR']}}]}}}
        expected = {'schema_version': 1, 'mode': 'arrival_order_offline',
                    'fingerprints': {'version': 1, 'algorithm': 'sha256',
                        'corpus_source_sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                        'effective_config_sha256': digest(config),
                        'arrival_schedule_sha256': digest({'schema_version': 1, 'observation_end_unix_nano': cutoff,
                            'arrivals': sorted(events, key=lambda r: (r['at_unix_nano'], r['trace_id'], r['span_id']))}),
                        'effective_arrival_model_sha256': digest({'name': 'first_arrival_fixed_wait_v1', 'decision_wait_ms': wait})},
                    'model': {'name': 'first_arrival_fixed_wait_v1', 'decision_wait_ms': wait,
                        'observation_end_unix_nano': cutoff,
                        'deadline_boundary': 'Only arrivals strictly before the deadline participate; timers at cutoff fire.',
                        'late_span_handling': 'Discard every late span, including spans of sampled traces; never reopen.',
                        'end_of_input': 'Do not flush pending traces or decide unobserved traces.'},
                    'assumptions': {
                        'corpus_completeness': 'Missing means absent from the schedule relative to the supplied reference corpus only; real completeness is not verified.',
                        'evaluation': 'One policy evaluation per trace at first arrival plus wait, using only spans received strictly before that deadline.',
                        'online_timing_or_cache_equivalence': False, 'production_savings_estimated': False,
                        'collector_equivalence': 'Synthetic timing subset only; no Collector tick scheduling, caches, early matches, eviction, or root acceleration.',
                        'byte_scope': 'No serialized byte or cost estimate is made for arrival replay.'},
                    'summary': summary, 'traces': rows}
        self.assertEqual(set(self.original), set(FIELDS))
        for field in FIELDS:
            with self.subTest(oracle=field):
                self.assertEqual(self.original[field], expected[field])
        return expected

    def test_saved_report_agrees_with_independent_arrival_oracle(self):
        self.assert_oracle()
        self.check()

    def test_deadline_cutoff_ties_and_late_discard_after_sample(self):
        # 50ms: trace 1's ERROR is exactly at the deadline and late; trace 4's
        # timer is exactly at the inclusive cutoff and fires.
        self.produce(wait='50')
        self.assert_oracle(wait=50)
        self.check(wait='50')
        manifest = json.loads(self.arrivals.read_bytes())
        manifest['arrivals'][0]['at_unix_nano'] = '100000000'  # OK late after ERROR sampled
        manifest['arrivals'][1]['at_unix_nano'] = '0'
        manifest['arrivals'][3]['at_unix_nano'] = '100000000'  # ERROR deadline tie
        manifest['arrivals'][4]['at_unix_nano'] = '150000000'  # deadline at cutoff
        manifest['arrivals'][5]['at_unix_nano'] = '250000000'  # arrives at cutoff; pending
        self.arrivals.write_text(json.dumps(manifest))
        self.produce()
        self.assert_oracle()
        self.assertEqual(self.original['traces'][0]['decision_snapshot']['kept_span_count'], 1)
        self.assertEqual(self.original['traces'][0]['late_span_ids'], ['0000000000000001'])
        self.check()

    def test_every_identity_model_and_nested_evidence_individually_and_combined(self):
        changes = []
        for key in self.original['fingerprints'].keys() - {'version', 'algorithm'}:
            changes.append(('fingerprints', lambda r, k=key: r['fingerprints'].update({k: '0' * 64})))
        changes += [
            ('model', lambda r: r['model'].update(name='untrusted_model_v2')),
            ('model', lambda r: r['model'].update(decision_wait_ms=-1)),
            ('model', lambda r: r['model'].update(deadline_boundary='inclusive')),
            ('model', lambda r: r['model'].update(late_span_handling='retain')),
            ('model', lambda r: r['model'].update(end_of_input='flush')),
            ('assumptions', lambda r: r.update(assumptions={})),
            ('summary', lambda r: r['summary'].update(kept_spans=999)),
            ('traces', lambda r: r['traces'][0].update(deadline_unix_nano=0)),
            ('traces', lambda r: r['traces'][0]['on_time_span_ids'].pop()),
            ('traces', lambda r: r['traces'][1].update(late_span_ids=[])),
            ('traces', lambda r: r['traces'][2].update(missing_span_ids=[])),
            ('traces', lambda r: r['traces'][2].update(unseen_at_decision_span_ids=[])),
            ('traces', lambda r: r['traces'][3].update(state='decided')),
            ('traces', lambda r: r['traces'][4].update(decision_snapshot={})),
            ('traces', lambda r: r['traces'][0]['decision_snapshot'].update(decision='invented')),
            ('traces', lambda r: r['traces'][0]['decision_snapshot']['policies'][0].update(reason='invented')),
            ('traces', lambda r: r['traces'][0]['decision_snapshot']['policies'][0].update(children=[None])),
            ('traces', lambda r: r['traces'][0]['full_trace']['policies'][0].update(matched=False)),
            ('traces', lambda r: r['traces'][1].update(decision_differs_from_full_trace=False))]
        for field, mutate in changes:
            value = copy.deepcopy(self.original)
            mutate(value)
            self.assertNotEqual(value, self.original)
            self.save(value)
            with self.subTest(field=field):
                self.check([field])
        value = copy.deepcopy(self.original)
        for _, mutate in changes:
            mutate(value)
        self.save(value)
        self.check(['fingerprints', 'model', 'assumptions', 'summary', 'traces'])

    def test_json_scalar_types_array_order_and_nested_finite_data(self):
        changes = [
            ('traces', lambda r: r['traces'].reverse()),
            ('traces', lambda r: r['traces'][0]['on_time_span_ids'].reverse()),
            ('traces', lambda r: r['traces'][0]['decision_snapshot']['statuses'].reverse()),
            ('traces', lambda r: r['traces'][0]['decision_snapshot'].update(error=1)),
            ('traces', lambda r: r['traces'][0]['decision_snapshot']['policies'][0].update(matched=1)),
            ('traces', lambda r: r.update(traces=[None, True, 1, 'finite', []])),
            ('summary', lambda r: r['summary']['changed_decision_trace_ids'].reverse()),
            ('assumptions', lambda r: r['assumptions'].update(production_savings_estimated=0)),
            ('model', lambda r: r['model'].pop('name')),
            ('summary', lambda r: r['summary'].update(extra={'arbitrary': [True, 1, None, 1.0]}))]
        for value in (True, 1.0, '1', None, []):
            changes.append(('summary', lambda r, v=value: r['summary'].update(sampled_traces=v)))
        for value in (100.0, '100', True, None):
            changes.append(('model', lambda r, v=value: r['model'].update(decision_wait_ms=v)))
        for field, mutate in changes:
            value = copy.deepcopy(self.original)
            mutate(value)
            self.save(value)
            with self.subTest(field=field):
                self.check([field])

    def test_object_order_unicode_and_equivalent_inputs(self):
        def reverse_keys(value):
            if isinstance(value, dict):
                return {k: reverse_keys(value[k]) for k in reversed(value)}
            if isinstance(value, list):
                return [reverse_keys(v) for v in value]
            return value
        # Give case normalization real hexadecimal letters to normalize.
        raw = self.source.read_text().replace('00000000000000000000000000000001', '0000000000000000000000000000000a')
        raw = raw.replace('0000000000000001"', '000000000000000a"')
        self.source.write_text(raw)
        manifest = json.loads(self.arrivals.read_text().replace('00000000000000000000000000000001', '0000000000000000000000000000000a')
                              .replace('0000000000000001"', '000000000000000a"'))
        self.arrivals.write_text(json.dumps(manifest))
        self.config.write_text(self.config.read_text().replace('name: errors', 'name: errors-λ'))
        self.produce()
        self.saved.write_text(json.dumps(reverse_keys(self.original), ensure_ascii=True))
        self.config.write_text('{"processors":{"tail_sampling":{"policies":[{"status_code":{"status_codes":["ERROR","ERROR"]},"type":"status_code","name":"errors-\\u03bb"}]}}}')
        manifest['arrivals'].reverse()
        manifest['observation_end_unix_nano'] = int(manifest['observation_end_unix_nano'])
        for row in manifest['arrivals']:
            row['at_unix_nano'] = '000' + str(row['at_unix_nano'])
            row['trace_id'], row['span_id'] = row['trace_id'].upper(), row['span_id'].upper()
        self.arrivals.write_text(json.dumps(reverse_keys(manifest), indent=4))
        self.check(wait='000100')

    def test_changed_supplied_inputs_drive_regeneration_and_empty_is_valid(self):
        self.check(['fingerprints', 'model', 'summary', 'traces'], wait='50')
        self.source.write_bytes(self.source.read_bytes() + b'\n')
        self.check(['fingerprints'])
        self.source.write_bytes((FIXTURE / 'traces.json').read_bytes())
        self.source.write_bytes(self.source.read_bytes().replace(b'"code": 2', b'"code": 1'))
        self.check(['fingerprints', 'summary', 'traces'])
        self.source.write_bytes((FIXTURE / 'traces.json').read_bytes())
        self.config.write_text('processors: {tail_sampling: {policies: [{name: all, type: always_sample}]}}')
        self.check(['fingerprints', 'summary', 'traces'])
        self.config.write_bytes((FIXTURE / 'policy.yaml').read_bytes())
        manifest = json.loads(self.arrivals.read_bytes())
        manifest['arrivals'][3]['at_unix_nano'] = '50000000'
        self.arrivals.write_text(json.dumps(manifest))
        self.check(['fingerprints', 'summary', 'traces'])
        manifest = json.loads((FIXTURE / 'arrivals.json').read_bytes())
        manifest['observation_end_unix_nano'] = '300000000'
        self.arrivals.write_text(json.dumps(manifest))
        self.check(['fingerprints', 'model', 'summary', 'traces'])
        self.source.write_bytes(b'{"resourceSpans":[]}')
        self.arrivals.write_text('{"schema_version":1,"observation_end_unix_nano":0,"arrivals":[]}')
        self.produce()
        self.assert_oracle()
        self.check()

    def test_malformed_envelope_metadata_and_digests_fail_atomically(self):
        variants = [None, [], {}]
        for key in self.original:
            variants.append({k: v for k, v in self.original.items() if k != key})
        for key, value in [('schema_version', True), ('schema_version', 1.0), ('schema_version', '1'),
                           ('schema_version', 2), ('mode', 'complete_trace_offline'),
                           ('mode', 'arrival_order_offline_comparison'), ('mode', None), ('extra', 1),
                           ('fingerprints', []), ('model', []), ('assumptions', None), ('summary', []), ('traces', {})]:
            variants.append({**self.original, key: value})
        fp = self.original['fingerprints']
        for key in fp:
            variants.append({**self.original, 'fingerprints': {k: v for k, v in fp.items() if k != key}})
        for key, value in [('version', True), ('version', 1.0), ('version', '1'), ('version', 2),
                           ('algorithm', 'SHA256'), ('algorithm', 'sha512'), ('extra', 1)]:
            variants.append({**self.original, 'fingerprints': {**fp, key: value}})
        for key in fp.keys() - {'version', 'algorithm'}:
            for value in ['A' * 64, 'g' * 64, '0' * 63, None, 1, True]:
                variants.append({**self.original, 'fingerprints': {**fp, key: value}})
        for i, variant in enumerate(variants):
            self.save(variant)
            with self.subTest(variant=i):
                self.invalid()

    def test_duplicate_keys_nonfinite_anywhere_and_bad_json_fail_atomically(self):
        raw = json.dumps(self.original)
        variants = [b'\xff', b'{', b'{"schema_version":1,"schema_version":1}']
        for marker in ('"model": {', '"assumptions": {', '"summary": {', '"fingerprints": {',
                       '"traces": [{', '"decision_snapshot": {', '"full_trace": {', '"policies": [{'):
            start = 0
            while (pos := raw.find(marker, start)) >= 0:
                end = pos + len(marker)
                for token in ('NaN', 'Infinity', '-Infinity', '1e999', '-1e999'):
                    variants.append((raw[:end] + '"bad":[{"nested":' + token + '}],' + raw[end:]).encode())
                variants.append((raw[:end] + '"bad":1,"bad":2,' + raw[end:]).encode())
                start = end
        for i, value in enumerate(variants):
            self.saved.write_bytes(value)
            with self.subTest(variant=i):
                self.invalid()

    def test_invalid_sources_schedule_wait_deadline_files_and_arguments(self):
        for path in (self.source, self.config, self.arrivals):
            original = path.read_bytes()
            variants = [b'\xff', b'{']
            if path == self.source:
                variants += [b'{"resourceSpans":[],"resourceSpans":[]}', b'{"resourceSpans":[],"extra":1}',
                             b'{"resourceSpans":[{"scopeSpans":[{"spans":[{}]}]}]}']
            elif path == self.config:
                variants += [b'processors: {}', b'processors: &x {tail_sampling: *x}',
                             b'processors: {tail_sampling: {decision_wait: 1, policies: []}}',
                             b'processors: {tail_sampling: {policies: [{name: x, name: y, type: always_sample}]}}',
                             b'processors: {tail_sampling: {policies: [{name: x, type: latency, latency: {threshold_ms: -1}}]}}']
            else:
                valid = json.loads(original)
                schedules = [None, [], {**valid, 'extra': 1}, {**valid, 'arrivals': {}},
                             {**valid, 'arrivals': valid['arrivals'] + [valid['arrivals'][0]]}]
                for key, value in [('schema_version', True), ('schema_version', '1'), ('schema_version', 2),
                                   ('observation_end_unix_nano', -1), ('observation_end_unix_nano', 2**64)]:
                    schedules.append({**valid, key: value})
                for key, value in [('at_unix_nano', True), ('at_unix_nano', 0.0), ('at_unix_nano', -1),
                                   ('at_unix_nano', '250000001'), ('at_unix_nano', '18446744073709551616'),
                                   ('trace_id', 'f' * 32), ('span_id', 'f' * 16)]:
                    schedules.append({**valid, 'arrivals': [{**valid['arrivals'][0], key: value}]})
                variants += [json.dumps(s).encode() for s in schedules]
                variants += [original.replace(b'"schema_version": 1', b'"schema_version": 1,"schema_version": 1'),
                             original.replace(b'"at_unix_nano": "0"', b'"at_unix_nano": 1e999')]
            for value in variants:
                path.write_bytes(value)
                with self.subTest(path=path.name, data=value[:100]):
                    self.invalid()
            path.write_bytes(original)
        for wait in (0, -1, 'true', '1.5', (2**64 - 1) // 1_000_000 + 1):
            self.invalid(wait)
        manifest = json.loads(self.arrivals.read_bytes())
        manifest['arrivals'] = [{**manifest['arrivals'][0], 'at_unix_nano': str(2**64 - 1)}]
        manifest['observation_end_unix_nano'] = str(2**64 - 1)
        self.arrivals.write_text(json.dumps(manifest))
        self.invalid()
        # Exact maximum deadline is valid; one nanosecond above it is rejected.
        manifest['arrivals'][0]['at_unix_nano'] = str(2**64 - 1 - 1_000_000)
        self.arrivals.write_text(json.dumps(manifest))
        self.produce(wait='1')
        self.assert_oracle(wait=1)
        self.check(wait='1')
        manifest['arrivals'][0]['at_unix_nano'] = str(2**64 - 1 - 1_000_000 + 1)
        self.arrivals.write_text(json.dumps(manifest))
        self.invalid(wait='1')
        self.arrivals.write_bytes((FIXTURE / 'arrivals.json').read_bytes())
        self.produce()
        for path in (self.source, self.config, self.arrivals, self.saved):
            raw = path.read_bytes()
            path.unlink()
            self.invalid()
            path.write_bytes(raw)
        self.invalid('100', '--baseline-wait-ms', '100')
        self.invalid('100', '--report', str(self.root))
        cli = [os.environ['TAILSIM_TEST_CLI']] if 'TAILSIM_TEST_CLI' in os.environ else [sys.executable, '-m', 'otelwrench', 'tailsim']
        for missing in ('--report', '--config', '--arrivals', '--decision-wait-ms'):
            args = [*cli, 'verify-arrivals-evidence', str(self.source)]
            for key, value in [('--report', self.saved), ('--config', self.config),
                               ('--arrivals', self.arrivals), ('--decision-wait-ms', '100')]:
                if key != missing:
                    args += [key, str(value)]
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')

    def test_provenance_stays_opaque(self):
        value = copy.deepcopy(self.original)
        value.update(model={'untrusted': True}, assumptions={}, summary={}, traces=[None])
        self.save(value)
        result = self.command('verify-arrivals')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['matches'])
        self.check(['model', 'assumptions', 'summary', 'traces'])
