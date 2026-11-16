import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from otelwrench.tailsim.replay import compare, replay
from .helpers import corpus, policies, policy, record

ROOT = Path(__file__).resolve().parents[2]


class EndToEnd(unittest.TestCase):
    def command(self, *args):
        return subprocess.run([sys.executable, '-m', 'otelwrench', 'tailsim', *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True)

    def test_real_cli_example_independently_checked(self):
        result = self.command('replay', 'examples/tailsim/traces.json', '--config', 'examples/tailsim/policy.yaml')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['summary']['kept_traces'], 1)
        self.assertEqual(report['summary']['error_retention'], {'total': 1, 'kept': 1, 'ratio': 1.0})
        self.assertEqual(report['traces'][1]['matched_policy_names'], ['errors', 'slow'])
        # Independent standard-library OTLP filtering and byte measurement.
        raw = json.loads((ROOT / 'examples/tailsim/traces.json').read_text())
        kept = copy.deepcopy(raw)
        kept['resourceSpans'][0]['scopeSpans'][0]['spans'] = [raw['resourceSpans'][0]['scopeSpans'][0]['spans'][1]]
        def size(value):
            return len(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
        self.assertEqual(report['summary']['serialized_total_bytes'], size(raw))
        self.assertEqual(report['summary']['serialized_kept_bytes'], size(kept))

    def test_compare_cli_baseline_always_sample(self):
        result = self.command('compare', 'examples/tailsim/traces.json', '--baseline', 'examples/tailsim/baseline.yaml', '--candidate', 'examples/tailsim/policy.yaml')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['baseline']['summary']['kept_traces'], 2)
        self.assertEqual(report['candidate']['summary']['kept_traces'], 1)
        self.assertEqual(report['comparison']['changed_trace_count'], 1)
        self.assertEqual(report['comparison']['serialized_kept_byte_delta'], -273)
        self.assertEqual(report['comparison']['error_traces_lost'], [])

    def test_error_retention_regression_and_gains(self):
        c = corpus([record(trace=1, status=2), record(trace=2, status=2, duration=600_000_000)])
        all_p = policies(policy('always_sample'))
        slow = policies(policy('latency', {'threshold_ms': 500}))
        result = compare(c, all_p, slow)
        self.assertEqual(result['candidate']['summary']['error_retention']['ratio'], 0.5)
        self.assertEqual(result['comparison']['error_traces_lost'], [f'{1:032x}'])
        reverse = compare(c, slow, all_p)
        self.assertEqual(reverse['comparison']['error_traces_gained'], [f'{1:032x}'])
        self.assertEqual(reverse['comparison']['newly_kept_traces'], 1)

    def test_no_errors_and_empty_corpus_no_nan(self):
        result = replay(corpus([]), policies(policy('always_sample')))
        self.assertEqual(result['summary']['error_retention']['ratio'], None)
        self.assertEqual(result['summary']['serialized_kept_bytes'], len(b'{"resourceSpans":[]}'))
        self.assertEqual(result['summary']['total_traces'], 0)
        json.dumps(result, allow_nan=False)

    def test_failures_have_no_stdout(self):
        configs = ['processors: {tail_sampling: {policies: [{name: x, type: probabilistic}]}}',
                   'processors: {tail_sampling: {policies: [{name: x, type: always_sample}], decision_wait: 10s}}',
                   'processors: {tail_sampling: {policies: [{name: x, type: string_attribute, string_attribute: {key: x, values: [a], enabled_regex_matching: true}}]}}',
                   'processors: {}']
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.yaml'
            for text in configs:
                path.write_text(text)
                result = self.command('replay', 'examples/tailsim/traces.json', '--config', path)
                with self.subTest(text=text):
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('tailsim:', result.stderr)
            bad_corpus = Path(tmp) / 'bad.json'
            bad_corpus.write_text('{"resourceSpans":[{"scopeSpans":[{"spans":[{}]}]}]}')
            result = self.command('replay', bad_corpus, '--config', 'examples/tailsim/policy.yaml')
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            result = self.command('compare', 'examples/tailsim/traces.json', '--baseline', 'examples/tailsim/baseline.yaml', '--candidate', path)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')

    def test_missing_files_and_arguments(self):
        for args in [('replay', '/nonexistent/tailsim-fixture', '--config', 'examples/tailsim/policy.yaml'),
                     ('replay', 'examples/tailsim/traces.json'), ()]:
            with self.subTest(args=args):
                result = self.command(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertTrue(result.stderr)

    def test_local_example_and_not_and_drop_configs(self):
        result = self.command('replay', 'examples/tailsim/traces.json', '--config', 'examples/tailsim/and-not.yaml')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['summary']['kept_traces'], 1)
        result = self.command('replay', 'benchmarks/tailsim/traces.json', '--config', 'examples/tailsim/drop.yaml')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['summary']['kept_traces'], 28)
        vetoed = next(t for t in data['traces'] if t['trace_id'] == f'{100:032x}')
        self.assertEqual(vetoed['matched_policy_names'], ['errors', 'slow', 'health-veto'])
        self.assertEqual(vetoed['reason'], 'drop_policy_precedence')

    def test_malformed_corpus_and_yaml_are_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'bad.json'
            for contents in [b'\xff', b'{', b'{"resourceSpans":NaN}']:
                p.write_bytes(contents)
                result = self.command('replay', p, '--config', 'examples/tailsim/policy.yaml')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertNotIn('Traceback', result.stderr)
            p = Path(tmp) / 'bad.yaml'
            p.write_text('processors: [broken')
            result = self.command('replay', 'examples/tailsim/traces.json', '--config', p)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertNotIn('Traceback', result.stderr)

    def test_all_unsupported_policy_types_fail_without_report(self):
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.yaml'
            for kind in ['probabilistic', 'composite', 'rate_limiting', 'bytes_limiting',
                         'numeric_attribute', 'boolean_attribute', 'span_count',
                         'trace_state', 'trace_flags', 'ottl_condition', 'unknown']:
                path.write_text(yaml.safe_dump({'processors': {'tail_sampling': {'policies': [policy(kind)]}}}))
                result = self.command('replay', 'examples/tailsim/traces.json', '--config', path)
                with self.subTest(kind=kind):
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('unsupported policy', result.stderr)
