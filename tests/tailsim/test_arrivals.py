"""Synthetic arrival schedules with hand-authored decisions and missing spans."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from .helpers import attr, document, policy, record

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'examples/tailsim' / 'arrival-order'


def event(trace=1, sid=1, at=0):
    return {'trace_id': f'{trace:032x}', 'span_id': f'{sid:016x}', 'at_unix_nano': str(at)}


def schedule(*events, end=250_000_000):
    return {'schema_version': 1, 'observation_end_unix_nano': str(end), 'arrivals': list(events)}


class ArrivalReplay(unittest.TestCase):
    def command(self, corpus=FIXTURE / 'traces.json', arrivals=FIXTURE / 'arrivals.json',
                config=FIXTURE / 'policy.yaml', wait='100'):
        return subprocess.run([sys.executable, '-m', 'otelwrench', 'tailsim', 'replay-arrivals', str(corpus),
                               '--arrivals', str(arrivals), '--config', str(config),
                               '--decision-wait-ms', str(wait)], capture_output=True, text=True)

    def run_case(self, data, manifest, items=None, wait='100'):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / name for name in ['corpus.json', 'arrivals.json', 'policy.yaml']]
            paths[0].write_text(json.dumps(data))
            paths[1].write_text(json.dumps(manifest))
            paths[2].write_text(yaml.safe_dump({'processors': {'tail_sampling': {
                'policies': items or [policy('status_code', {'status_codes': ['ERROR']})]}}}))
            return self.command(*paths, wait=wait)

    def report(self, data, manifest, items=None, wait='100'):
        result = self.run_case(data, manifest, items, wait)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_failure(self, result, message=None):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertNotIn('Traceback', result.stderr)
        if message:
            self.assertIn(message, result.stderr)

    def test_public_fixture_hand_authored_oracle(self):
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        expected = json.loads((FIXTURE / 'expected.json').read_text())
        self.assertEqual(report['mode'], 'arrival_order_offline')
        self.assertEqual(report['summary'], expected['summary'])
        self.assertEqual(report['model']['decision_wait_ms'], expected['decision_wait_ms'])
        self.assertFalse(report['assumptions']['online_timing_or_cache_equivalence'])
        self.assertFalse(report['assumptions']['production_savings_estimated'])
        self.assertEqual(len(report['traces']), len(expected['traces']))
        for row, oracle in zip(report['traces'], expected['traces']):
            with self.subTest(trace=oracle['suffix']):
                self.assertEqual(row['trace_id'], f"{oracle['suffix']:032x}")
                self.assertEqual(row['state'], oracle['state'])
                self.assertEqual(row['deadline_unix_nano'], oracle['deadline'])
                snapshot = row['decision_snapshot']
                self.assertEqual(snapshot['decision'] if snapshot else None, oracle['decision'])
                self.assertEqual(row['full_trace']['decision'], oracle['full_trace_decision'])
                for actual, key in [('on_time_span_ids', 'on_time'), ('late_span_ids', 'late'),
                                    ('missing_span_ids', 'missing'), ('unseen_at_decision_span_ids', 'unseen_at_decision')]:
                    ids = oracle[key]
                    self.assertEqual(row[actual], None if ids is None else [f'{sid:016x}' for sid in ids])
                differs = None if oracle['decision'] is None else oracle['decision'] != oracle['full_trace_decision']
                self.assertEqual(row['decision_differs_from_full_trace'], differs)
                if snapshot:
                    self.assertEqual(snapshot['span_count'], len(oracle['on_time']))
                    self.assertEqual(snapshot['kept_span_count'], len(oracle['on_time']) if oracle['decision'] == 'sample' else 0)
                    self.assertEqual(snapshot['policies'][0]['matched'], oracle['decision'] == 'sample')

    def test_deadline_is_exclusive_and_cutoff_is_inclusive(self):
        data = document([record(sid=1), record(sid=2, status=2)])
        simultaneous = self.report(data, schedule(event(), event(sid=2)))
        self.assertEqual(simultaneous['traces'][0]['decision_snapshot']['decision'], 'sample')
        self.assertEqual(simultaneous['traces'][0]['on_time_span_ids'], [f'{sid:016x}' for sid in (1, 2)])
        for at, expected in [(99_999_999, 'sample'), (100_000_000, 'drop'), (100_000_001, 'drop')]:
            report = self.report(data, schedule(event(), event(sid=2, at=at), end=at + 100_000_000))
            row = report['traces'][0]
            self.assertEqual(row['decision_snapshot']['decision'], expected)
            self.assertEqual(len(row['late_span_ids']), int(at >= 100_000_000))
        at_deadline = self.report(data, schedule(event(), event(sid=2, at=100_000_000), end=100_000_000))
        self.assertEqual(at_deadline['traces'][0]['state'], 'decided')
        pending = self.report(data, schedule(event(), end=99_999_999))
        self.assertEqual(pending['traces'][0]['state'], 'pending')
        self.assertIsNone(pending['traces'][0]['decision_snapshot'])
        self.assertEqual(pending['summary']['changed_decision_trace_ids'], [])

    def test_deadline_uses_first_arrival_not_span_time_and_never_resets(self):
        data = document([record(sid=1, start=9_000_000_000), record(sid=2, status=2)])
        report = self.report(data, schedule(event(at=10), event(sid=2, at=100_000_010), end=200_000_000))
        row = report['traces'][0]
        self.assertEqual(row['first_arrival_unix_nano'], 10)
        self.assertEqual(row['deadline_unix_nano'], 100_000_010)
        self.assertEqual(row['decision_snapshot']['decision'], 'drop')
        self.assertEqual(row['late_span_ids'], [f'{2:016x}'])

    def test_late_spans_discarded_even_for_sampled_traces(self):
        data = document([record(sid=1, status=2), record(sid=2, status=2)])
        report = self.report(data, schedule(event(), event(sid=2, at=150_000_000)))
        self.assertEqual(report['summary']['kept_spans'], 1)
        row = report['traces'][0]
        self.assertEqual(row['decision_snapshot']['kept_span_count'], 1)
        self.assertEqual(row['full_trace']['kept_span_count'], 2)
        self.assertFalse(row['decision_differs_from_full_trace'])

    def test_input_order_and_id_case_do_not_affect_report(self):
        data = json.loads((FIXTURE / 'traces.json').read_text())
        manifest = json.loads((FIXTURE / 'arrivals.json').read_text())
        # Use alphabetic hex to exercise ID normalization too.
        for span in data['resourceSpans'][0]['scopeSpans'][0]['spans']:
            span['traceId'] = span['traceId'][:-1] + format(int(span['traceId'][-1]) + 9, 'x')
        for item in manifest['arrivals']:
            item['trace_id'] = item['trace_id'][:-1] + format(int(item['trace_id'][-1]) + 9, 'X')
        before = self.report(data, manifest)
        data['resourceSpans'][0]['scopeSpans'][0]['spans'].reverse()
        manifest['arrivals'].reverse()
        after = self.report(data, manifest)
        before_ids, after_ids = before.pop('fingerprints'), after.pop('fingerprints')
        # Decisions/evidence normalize, while source identity preserves corpus bytes.
        self.assertNotEqual(before_ids.pop('corpus_source_sha256'), after_ids.pop('corpus_source_sha256'))
        self.assertEqual(before_ids, after_ids)
        self.assertEqual(before, after)

    def test_subset_preserves_context_and_uses_observed_duration_for_drop(self):
        first = record(sid=1, status=2, duration=10_000_000)
        second = record(sid=2, duration=600_000_000)
        data = document([first], [attr('service.name', 'api')])
        data['resourceSpans'].extend(document([second], [attr('service.name', 'other')])['resourceSpans'])
        items = [policy('and', {'and_sub_policy': [
            policy('status_code', {'status_codes': ['ERROR']}),
            policy('string_attribute', {'key': 'service.name', 'values': ['api']})]}),
            policy('drop', {'drop_sub_policy': [policy('latency', {'threshold_ms': 500})]})]
        row = self.report(data, schedule(event(), event(sid=2, at=150_000_000)), items)['traces'][0]
        self.assertEqual(row['decision_snapshot']['decision'], 'sample')
        self.assertEqual(row['decision_snapshot']['duration_ms_floor'], 10)
        self.assertEqual(row['decision_snapshot']['matched_policy_names'], ['and'])
        self.assertEqual(row['full_trace']['decision'], 'drop')
        self.assertEqual(row['full_trace']['reason'], 'drop_policy_precedence')

    def test_empty_schedule_empty_corpus_and_remote_parent(self):
        empty = self.report({'resourceSpans': []}, schedule())
        self.assertEqual(empty['traces'], [])
        self.assertEqual(empty['summary']['reference_spans'], 0)
        span = record()
        span['parentSpanId'] = f'{99:016x}'
        report = self.report(document([span]), schedule())
        self.assertEqual(report['summary']['unobserved_traces'], 1)
        self.assertEqual(report['summary']['missing_spans'], 1)
        report = self.report(document([span]), schedule(event()))
        self.assertEqual(report['summary']['missing_spans'], 0)
        self.assertEqual(report['summary']['incomplete_decisions'], 0)

    def test_bad_schedule_is_atomic(self):
        data = document([record()])
        valid = schedule(event())
        cases = [[], {}, {**valid, 'schema_version': 2}, {**valid, 'schema_version': True},
                 {**valid, 'unknown': 1}, {**valid, 'arrivals': None},
                 {**valid, 'observation_end_unix_nano': -1},
                 {**valid, 'arrivals': [event(), event()]},
                 {**valid, 'arrivals': [event(trace=2)]},
                 {**valid, 'arrivals': [event(sid=2)]},
                 {**valid, 'arrivals': [event(at=250_000_001)]},
                 {**valid, 'arrivals': [{**event(), 'span_id': '0'*16}]},
                 {**valid, 'arrivals': [{**event(), 'at_unix_nano': True}]},
                 {**valid, 'arrivals': [{**event(), 'at_unix_nano': 0.5}]},
                 {**valid, 'arrivals': [{**event(), 'at_unix_nano': str(2**64)}]},
                 {**valid, 'arrivals': [{**event(), 'extra': 1}]},
                 {**valid, 'arrivals': [{'trace_id': event()['trace_id']}]}]
        for manifest in cases:
            with self.subTest(manifest=manifest):
                self.assert_failure(self.run_case(data, manifest))

    def test_malformed_json_and_duplicate_keys_are_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.json'
            for raw in [b'\xff', b'{', b'{"schema_version":1,"schema_version":1}',
                        b'{"schema_version":NaN}']:
                path.write_bytes(raw)
                self.assert_failure(self.command(arrivals=path))

    def test_invalid_wait_and_deadline_overflow_are_atomic(self):
        for wait in ['0', '-1', '0.1', str(2**64)]:
            with self.subTest(wait=wait):
                self.assert_failure(self.command(wait=wait))
        self.assert_failure(self.run_case(document([record()]),
            schedule(event(at=2**64 - 1), end=2**64 - 1)), 'deadline')

    def test_collector_runtime_options_are_rejected_even_when_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.yaml'
            base = yaml.safe_load((FIXTURE / 'policy.yaml').read_text())
            for key, value in [('decision_wait', '100ms'), ('num_traces', 10),
                               ('expected_new_traces_per_sec', 10), ('decision_cache', {}),
                               ('sample_on_first_match', False), ('block_on_overflow', False),
                               ('sampling_strategy', 'trace-complete'), ('decision_wait_after_root_received', '0s')]:
                data = copy.deepcopy(base)
                data['processors']['tail_sampling'][key] = value
                path.write_text(yaml.safe_dump(data))
                with self.subTest(key=key):
                    self.assert_failure(self.command(config=path), key)
