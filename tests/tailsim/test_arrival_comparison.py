"""Paired waits: finalized flips stay separate from unresolved state changes."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from .helpers import document, policy, record
from .test_arrivals import event, schedule

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'examples/tailsim' / 'arrival-order'


def tid(n):
    return f'{n:032x}'


class ArrivalComparison(unittest.TestCase):
    def command(self, baseline=100, candidate=200, corpus=FIXTURE / 'traces.json',
                arrivals=FIXTURE / 'arrivals.json', config=FIXTURE / 'policy.yaml'):
        return subprocess.run([sys.executable, '-m', 'otelwrench', 'tailsim', 'compare-arrivals', str(corpus),
                               '--arrivals', str(arrivals), '--config', str(config),
                               '--baseline-wait-ms', str(baseline), '--candidate-wait-ms', str(candidate)],
                              capture_output=True, text=True)

    def report(self, *args, **kwargs):
        result = self.command(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_failure(self, result, message=None):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        if message:
            self.assertIn(message, result.stderr)

    def test_public_pair_and_exact_embedded_replays(self):
        report = self.report()
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['mode'], 'arrival_order_offline_comparison')
        self.assertFalse(report['assumptions']['online_timing_or_cache_equivalence'])
        self.assertFalse(report['assumptions']['production_savings_estimated'])
        pair = report['comparison']
        self.assertEqual(pair['both_decided_trace_count'], 3)
        self.assertEqual(pair['changed_decision_count'], 1)
        self.assertEqual(pair['newly_sampled_traces'], 1)
        self.assertEqual(pair['newly_dropped_traces'], 0)
        self.assertEqual(pair['state_change_count'], 0)
        self.assertEqual(pair['state_changes'], [])
        self.assertEqual(pair['kept_span_delta'], 2)
        self.assertEqual(pair['unresolved_trace_count'], 2)
        self.assertEqual(pair['unresolved_traces'], [
            {'trace_id': tid(4), 'baseline_state': 'pending', 'candidate_state': 'pending',
             'baseline_decision': None, 'candidate_decision': None},
            {'trace_id': tid(5), 'baseline_state': 'unobserved', 'candidate_state': 'unobserved',
             'baseline_decision': None, 'candidate_decision': None}])
        changed, = pair['changed_decisions']
        self.assertEqual(changed['trace_id'], tid(2))
        for side, wait, decision, on_time, late in [
                ('baseline', 100, 'drop', [1], [2]), ('candidate', 200, 'sample', [1, 2], [])]:
            result = subprocess.run([sys.executable, '-m', 'otelwrench', 'tailsim', 'replay-arrivals',
                str(FIXTURE / 'traces.json'), '--arrivals', str(FIXTURE / 'arrivals.json'),
                '--config', str(FIXTURE / 'policy.yaml'), '--decision-wait-ms', str(wait)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(report[side], json.loads(result.stdout))
            row = changed[side]
            self.assertEqual(row, report[side]['traces'][1])
            self.assertEqual(row['deadline_unix_nano'], wait * 1_000_000)
            self.assertEqual(row['decision_snapshot']['decision'], decision)
            self.assertEqual(row['on_time_span_ids'], [f'{s:016x}' for s in on_time])
            self.assertEqual(row['late_span_ids'], [f'{s:016x}' for s in late])
            self.assertEqual(row['missing_span_ids'], [])
            self.assertEqual(row['decision_snapshot']['policies'][0]['matched'], decision == 'sample')

    def test_deadline_tie_and_pending_resolution_are_distinct(self):
        report = self.report(candidate=50)
        pair = report['comparison']
        self.assertEqual(pair['both_decided_trace_count'], 3)
        self.assertEqual(pair['changed_decision_count'], 1)
        self.assertEqual(pair['newly_sampled_traces'], 0)  # pending -> sample is excluded
        self.assertEqual(pair['newly_dropped_traces'], 1)
        self.assertEqual(pair['kept_span_delta'], -1)
        changed, = pair['changed_decisions']
        self.assertEqual(changed['trace_id'], tid(1))
        self.assertEqual(changed['baseline']['decision_snapshot']['decision'], 'sample')
        self.assertEqual(changed['candidate']['decision_snapshot']['decision'], 'drop')
        self.assertEqual(changed['candidate']['late_span_ids'], [f'{2:016x}'])
        self.assertEqual(pair['state_change_count'], 1)
        state, = pair['state_changes']
        self.assertEqual(state['trace_id'], tid(4))
        self.assertEqual(state['baseline'], report['baseline']['traces'][3])
        self.assertIsNone(state['baseline']['decision_snapshot'])
        self.assertEqual(state['candidate'], report['candidate']['traces'][3])
        self.assertEqual(state['candidate']['deadline_unix_nano'], 250_000_000)
        self.assertEqual(state['candidate']['decision_snapshot']['decision'], 'sample')
        self.assertEqual(pair['unresolved_trace_count'], 2)
        self.assertEqual(pair['unresolved_traces'][0], {
            'trace_id': tid(4), 'baseline_state': 'pending', 'candidate_state': 'decided',
            'baseline_decision': None, 'candidate_decision': 'sample'})

    def test_longer_wait_can_remove_all_finalized_decisions(self):
        report = self.report(candidate=300)
        pair = report['comparison']
        self.assertEqual(pair['both_decided_trace_count'], 0)
        self.assertEqual(pair['changed_decisions'], [])
        self.assertEqual(pair['changed_decision_count'], 0)
        self.assertEqual(pair['newly_sampled_traces'], 0)
        self.assertEqual(pair['newly_dropped_traces'], 0)
        self.assertEqual(pair['state_change_count'], 3)
        self.assertEqual([r['trace_id'] for r in pair['state_changes']], [tid(i) for i in (1, 2, 3)])
        self.assertTrue(all(r['candidate']['state'] == 'pending' for r in pair['state_changes']))
        self.assertEqual(pair['unresolved_trace_count'], 5)
        self.assertEqual(pair['kept_span_delta'], -2)

    def test_reversing_and_equal_waits(self):
        forward = self.report(candidate=50)
        reverse = self.report(baseline=50, candidate=100)
        self.assertEqual(forward['baseline'], reverse['candidate'])
        self.assertEqual(forward['candidate'], reverse['baseline'])
        a, b = forward['comparison'], reverse['comparison']
        self.assertEqual(a['newly_sampled_traces'], b['newly_dropped_traces'])
        self.assertEqual(a['newly_dropped_traces'], b['newly_sampled_traces'])
        self.assertEqual(a['kept_span_delta'], -b['kept_span_delta'])
        for key in ['changed_decisions', 'state_changes']:
            self.assertEqual(b[key], [{'trace_id': r['trace_id'], 'baseline': r['candidate'],
                                      'candidate': r['baseline']} for r in a[key]])
        equal = self.report(candidate=100)
        self.assertEqual(equal['baseline'], equal['candidate'])
        pair = equal['comparison']
        for key in ['changed_decision_count', 'state_change_count', 'kept_span_delta',
                    'newly_sampled_traces', 'newly_dropped_traces']:
            self.assertEqual(pair[key], 0)
        self.assertEqual(pair['changed_decisions'], [])
        self.assertEqual(pair['state_changes'], [])
        self.assertEqual(pair['unresolved_trace_count'], 2)

    def test_same_decision_can_keep_more_spans_and_longer_wait_can_veto(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus, arrivals, config = [root / n for n in ['traces.json', 'arrivals.json', 'config.yaml']]
            corpus.write_text(json.dumps(document([record(sid=1, status=2),
                record(sid=2, duration=600_000_000)])))
            arrivals.write_text(json.dumps(schedule(event(), event(sid=2, at=150_000_000))))
            items = [policy('always_sample')]
            for veto in [False, True]:
                if veto:
                    items.append(policy('drop', {'drop_sub_policy': [policy('latency', {'threshold_ms': 500})]}))
                config.write_text(yaml.safe_dump({'processors': {'tail_sampling': {'policies': items}}}))
                report = self.report(corpus=corpus, arrivals=arrivals, config=config)
                pair = report['comparison']
                self.assertEqual(pair['state_changes'], [])
                self.assertEqual(pair['unresolved_traces'], [])
                self.assertEqual(pair['changed_decision_count'], int(veto))
                self.assertEqual(pair['kept_span_delta'], -1 if veto else 1)
                if veto:
                    changed, = pair['changed_decisions']
                    snapshot = changed['candidate']['decision_snapshot']
                    self.assertEqual(snapshot['decision'], 'drop')
                    self.assertEqual(snapshot['reason'], 'drop_policy_precedence')
                    self.assertEqual(snapshot['matched_policy_names'], ['always_sample', 'drop'])
                    self.assertTrue(snapshot['policies'][1]['children'][0]['matched'])

    def test_empty_and_unscheduled_corpora(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus, arrivals = root / 'corpus.json', root / 'arrivals.json'
            arrivals.write_text(json.dumps(schedule()))
            for data, count in [({'resourceSpans': []}, 0), (document([record()]), 1)]:
                corpus.write_text(json.dumps(data))
                report = self.report(corpus=corpus, arrivals=arrivals)
                pair = report['comparison']
                self.assertEqual(pair['both_decided_trace_count'], 0)
                self.assertEqual(pair['changed_decisions'], [])
                self.assertEqual(pair['state_changes'], [])
                self.assertEqual(pair['kept_span_delta'], 0)
                self.assertEqual(pair['unresolved_trace_count'], count)
                self.assertTrue(all(r['baseline_state'] == r['candidate_state'] == 'unobserved'
                                    for r in pair['unresolved_traces']))

    def test_permutation_id_case_and_integer_time_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus, arrivals = root / 'corpus.json', root / 'arrivals.json'
            data = document([record(trace=10, sid=10), record(trace=10, sid=11, status=2)])
            manifest = schedule(event(trace=10, sid=10), event(trace=10, sid=11, at=150_000_000))
            corpus.write_text(json.dumps(data))
            arrivals.write_text(json.dumps(manifest))
            before = self.report(corpus=corpus, arrivals=arrivals)
            data['resourceSpans'][0]['scopeSpans'][0]['spans'].reverse()
            manifest['arrivals'].reverse()
            manifest['observation_end_unix_nano'] = int(manifest['observation_end_unix_nano'])
            for item in manifest['arrivals']:
                item['trace_id'] = item['trace_id'].upper()
                item['span_id'] = item['span_id'].upper()
                item['at_unix_nano'] = int(item['at_unix_nano'])
            corpus.write_text(json.dumps(data))
            arrivals.write_text(json.dumps(manifest))
            after = self.report(corpus=corpus, arrivals=arrivals)
            for a, b in [(before, after), (before['baseline'], after['baseline']),
                         (before['candidate'], after['candidate'])]:
                before_ids = a.pop('fingerprints')
                after_ids = b.pop('fingerprints')
                self.assertNotEqual(before_ids.pop('corpus_source_sha256'), after_ids.pop('corpus_source_sha256'))
                self.assertEqual(before_ids, after_ids)
            self.assertEqual(before, after)

    def test_either_invalid_wait_and_deadline_fail_atomically(self):
        for side in ['baseline', 'candidate']:
            for value in ['0', '-1', '1.5', 'true', str(2**64), str((2**64 - 1) // 1_000_000 + 1)]:
                with self.subTest(side=side, wait=value):
                    self.assert_failure(self.command(**{side: value}))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus, arrivals = root / 'corpus.json', root / 'arrivals.json'
            corpus.write_text(json.dumps(document([record()])))
            # One 1ms deadline fits, the other 2ms deadline overflows uint64.
            arrivals.write_text(json.dumps(schedule(event(at=2**64 - 1 - 1_000_000), end=2**64 - 1)))
            for a, b in [(1, 2), (2, 1)]:
                self.assert_failure(self.command(a, b, corpus, arrivals), 'deadline')
            # Maximum wait is supported when its deadline fits (pending here).
            arrivals.write_text(json.dumps(schedule(event(), end=0)))
            report = self.report(1, (2**64 - 1) // 1_000_000, corpus, arrivals)
            self.assertEqual(report['candidate']['traces'][0]['state'], 'pending')

    def test_bad_inputs_have_no_partial_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.json'
            valid = json.loads((FIXTURE / 'arrivals.json').read_text())
            for data in [{**valid, 'extra': 1}, {**valid, 'schema_version': 2},
                         {**valid, 'arrivals': [event(), event()]},
                         {**valid, 'arrivals': [event(trace=99)]},
                         {**valid, 'arrivals': [event(at=250_000_001)]}]:
                path.write_text(json.dumps(data))
                self.assert_failure(self.command(arrivals=path))
            for raw in [b'\xff', b'{', b'{"schema_version":1,"schema_version":1}', b'{"arrivals":NaN}']:
                path.write_bytes(raw)
                self.assert_failure(self.command(arrivals=path))
            path.write_text(json.dumps(document([{}])))
            self.assert_failure(self.command(corpus=path))
            config = yaml.safe_load((FIXTURE / 'policy.yaml').read_text())
            for key, value in [('decision_wait', '100ms'), ('sample_on_first_match', False),
                               ('decision_cache', {}), ('num_traces', 0)]:
                bad = copy.deepcopy(config)
                bad['processors']['tail_sampling'][key] = value
                path.write_text(yaml.safe_dump(bad))
                self.assert_failure(self.command(config=path), key)
            self.assert_failure(self.command(arrivals=Path(tmp) / 'absent'))

    def test_missing_required_arguments(self):
        args = ['compare-arrivals', str(FIXTURE / 'traces.json'),
                '--config', str(FIXTURE / 'policy.yaml'), '--arrivals', str(FIXTURE / 'arrivals.json'),
                '--baseline-wait-ms', '100', '--candidate-wait-ms', '200']
        for option in ['--config', '--arrivals', '--baseline-wait-ms', '--candidate-wait-ms']:
            omitted = args.copy()
            at = omitted.index(option)
            del omitted[at:at + 2]
            result = subprocess.run([sys.executable, '-m', 'otelwrench', 'tailsim', *omitted], capture_output=True, text=True)
            self.assert_failure(result, option)
