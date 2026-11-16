"""Hand-authored oracle for the public, synthetic before/after matrix.

Votes and evidence strings come from expected.json, never from tailsim's
evaluator. Fixed tree assembly here only expands the fixture's compact format.
"""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from otelwrench.tailsim.corpus import load_corpus, parse_corpus
from otelwrench.tailsim.policy import load_config
from otelwrench.tailsim.replay import compare, replay

FIXTURE = Path(__file__).resolve().parents[2] / 'examples/tailsim' / 'decision-matrix'


def node(name, kind, matched, reason, children=None):
    return {'name': name, 'type': kind, 'matched': matched,
            'decision': ('dropped' if kind == 'drop' else 'sampled') if matched else 'not_sampled',
            'reason': reason, 'children': children or []}


def expected_policies(row):
    v, r = row['candidate']['votes'], row['evidence']
    error = lambda name: node(name, 'status_code', v['errors'], r['errors'])
    health = lambda: node('health', 'string_attribute', v['health'], r['health'])
    always = node('all', 'always_sample', True, 'always_sample')
    negation = 'opposite of single child match (not a drop veto)'
    return [always], [
        error('errors'), node('slow', 'latency', v['slow'], r['slow']),
        node('api', 'string_attribute', v['api'], r['api']),
        node('non-health-errors', 'and', v['non-health-errors'], 'all children must match', [
            error('error'), node('not-health', 'not', v['not-health'], negation, [health()])]),
        node('never', 'not', False, negation, [always]),
        node('health-veto', 'drop', v['health-veto'], 'all children must match; explicit drop veto', [
            health(), node('not-error', 'not', v['not-error'], negation, [error('error')])])]


class ComparisonEvidence(unittest.TestCase):
    def setUp(self):
        self.corpus = load_corpus(FIXTURE / 'traces.json')
        self.baseline = load_config(FIXTURE / 'baseline.yaml')
        self.candidate = load_config(FIXTURE / 'candidate.yaml')
        self.expected = json.loads((FIXTURE / 'expected.json').read_text())

    def assert_matrix(self, report, reverse=False):
        rows = self.expected['traces']
        before, after = ('candidate', 'baseline') if reverse else ('baseline', 'candidate')
        changed = []
        for expected, a, b in zip(rows, report['baseline']['traces'], report['candidate']['traces']):
            tid = expected['trace_id']
            base_tree, cand_tree = expected_policies(expected)
            trees = {'baseline': base_tree, 'candidate': cand_tree}
            for side, actual in [(before, a), (after, b)]:
                with self.subTest(trace_id=tid, side=side):
                    self.assertEqual(actual['trace_id'], tid)
                    self.assertEqual(actual['decision'], expected[side]['decision'])
                    self.assertEqual(actual['reason'], expected[side]['reason'])
                    self.assertEqual(actual['matched_policy_names'], expected[side]['matched_policy_names'])
                    self.assertEqual(actual['policies'], trees[side])
                    self.assertEqual(actual['duration_ns'], expected['duration_ns'])
                    self.assertEqual(actual['statuses'], expected['statuses'])
            if expected[before]['decision'] != expected[after]['decision']:
                changed.append({'trace_id': tid, 'baseline': expected[before]['decision'],
                                'candidate': expected[after]['decision'],
                                'error': expected['error'], 'span_count': expected['span_count'],
                                'baseline_matched_policy_names': expected[before]['matched_policy_names'],
                                'candidate_matched_policy_names': expected[after]['matched_policy_names'],
                                'baseline_reason': expected[before]['reason'],
                                'candidate_reason': expected[after]['reason'],
                                'baseline_policies': trees[before], 'candidate_policies': trees[after]})
        self.assertEqual(len(report['baseline']['traces']), len(rows))
        self.assertEqual(len(report['candidate']['traces']), len(rows))
        self.assertEqual(report['comparison']['changed_traces'], changed)
        self.assertEqual(report['comparison']['changed_trace_count'], 4)

    def test_self_contained_changed_rows_and_all_policy_matrix(self):
        self.assert_matrix(compare(self.corpus, self.baseline, self.candidate))

    def test_reverse_comparison_explains_newly_sampled_traces(self):
        report = compare(self.corpus, self.candidate, self.baseline)
        self.assert_matrix(report, reverse=True)
        self.assertEqual(report['comparison']['newly_kept_traces'], 4)

    def test_reports_label_full_corpus_and_inference_limits(self):
        report = compare(self.corpus, self.baseline, self.candidate)
        for item in [report, report['baseline'], report['candidate'],
                     replay(self.corpus, self.candidate)]:
            self.assertEqual(item['assumptions'], {
                'corpus_completeness': 'Caller must supply all spans of every trace; completeness is not verified.',
                'evaluation': 'All captured spans per trace are evaluated together using final complete-trace duration.',
                'online_timing_or_cache_equivalence': False,
                'production_savings_estimated': False,
                'byte_scope': 'Canonical serialized JSON for this finite corpus only; not transport, storage, or cost.'})

    def test_input_and_policy_order_do_not_change_explanations(self):
        permuted = copy.deepcopy(self.corpus.document)
        permuted['resourceSpans'].reverse()
        for resource in permuted['resourceSpans']:
            resource['scopeSpans'].reverse()
            for scope in resource['scopeSpans']:
                scope['spans'].reverse()
        self.assert_matrix(compare(parse_corpus(json.dumps(permuted)), self.baseline, self.candidate))
        forward = compare(self.corpus, self.baseline, self.candidate)
        reverse = compare(self.corpus, self.baseline, tuple(reversed(self.candidate)))
        for a, b in zip(forward['comparison']['changed_traces'], reverse['comparison']['changed_traces']):
            self.assertEqual(a['candidate_reason'], b['candidate_reason'])
            self.assertEqual(a['candidate'], b['candidate'])
            self.assertEqual(a['candidate_policies'], list(reversed(b['candidate_policies'])))

    def test_cli_matrix_and_atomic_failure(self):
        args = [sys.executable, '-m', 'otelwrench', 'tailsim', 'compare', str(FIXTURE / 'traces.json'),
                '--baseline', str(FIXTURE / 'baseline.yaml'), '--candidate', str(FIXTURE / 'candidate.yaml')]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_matrix(json.loads(result.stdout))
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / 'bad.yaml'
            bad.write_text((FIXTURE / 'candidate.yaml').read_text().replace('threshold_ms: 500', 'threshold_ms: -1'))
            result = subprocess.run(args[:-1] + [str(bad)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertIn('threshold_ms', result.stderr)
