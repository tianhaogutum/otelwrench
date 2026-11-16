# SPDX-License-Identifier: Apache-2.0
"""Opt-in nonempty identity-label coverage checks every effective sample."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from otelwrench.cardinality import analysis
from otelwrench.cardinality.parser import InputError, parse
from otelwrench.cardinality.scrapes import IdentifiedScrape, load_scrape_set

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "otelwrench", "cardinality"]
TIME = '2026-11-11T08:00:00Z'
REQUIRED = ['--require-target-label', 'job', '--require-target-label', 'instance']


class RequiredTargetLabelTests(unittest.TestCase):
    def write_set(self, directory, captures):
        entries = []
        for id, text, context in captures:
            path = Path(directory) / f'{id}.prom'
            path.write_text(text, encoding='utf-8')
            entry = dict(id=id, source=f'job=api instance={id}', captured_at=TIME, path=path.name)
            if context is not None:
                entry['target_labels'] = context
            entries.append(entry)
        path = Path(directory) / 'set.json'
        path.write_text(json.dumps(dict(schema_version=1, scrapes=entries)), encoding='utf-8')
        return path

    def cli(self, *args):
        return subprocess.run([*CLI, *map(str, args)], cwd='/', capture_output=True, timeout=60)

    def assert_invalid(self, proc):
        self.assertEqual(proc.returncode, 2, (proc.stdout, proc.stderr))
        self.assertEqual(proc.stdout, b'')
        return json.loads(proc.stderr)['error']

    def test_mixed_context_exposition_preserves_scoped_projection_scores_and_witnesses(self):
        with tempfile.TemporaryDirectory() as directory:
            # Each target supplies one required label in exposition and one in context.
            path = self.write_set(directory, [
                ('a', 'x{instance="a",debug="d"} 7 100\n', {'job': 'api'}),
                ('b', 'x{job="api",debug="d"} 7.0 200\n', {'instance': 'b'})])
            scrapes = load_scrape_set(path)
            options = dict(plan_names=['Identity', 'Debug'], plan_metrics=[['x'], ['x']], rank_plans=True)
            baseline = analysis.report_scrapes(scrapes, [['instance'], ['debug']], **options)
            required = ['job', 'instance']
            result = analysis.report_scrapes(scrapes, [['instance'], ['debug']],
                                            required_target_labels=required, **options)
            self.assertEqual(required, ['job', 'instance'])
            self.assertEqual(result['required_target_labels'], ['instance', 'job'])
            self.assertEqual(baseline['required_target_labels'], [])
            self.assertEqual({k: v for k, v in result.items() if k != 'required_target_labels'},
                             {k: v for k, v in baseline.items() if k != 'required_target_labels'})
            self.assertEqual(result['summary'], dict(scrape_count=2, sample_observations=2, observed_series=2))
            unsafe, safe = result['plans']
            self.assertEqual((unsafe['projected_keys_after'], unsafe['label_pairs_removed'], unsafe['status']),
                             (1, 2, 'unsafe_cross_scrape'))
            self.assertEqual((safe['projected_keys_after'], safe['label_pairs_removed'], safe['safe']), (2, 2, True))
            self.assertEqual([p['name'] for p in result['candidate_ranking']['ranked_safe_plans']], ['Debug'])
            self.assertTrue(all(p['safe'] for s in result['scrapes'] for p in s['plans']))
            originals = unsafe['collisions'][0]['originals']
            for id, raw, context, value, stamp, witness in zip(
                    ('a', 'b'), ({'instance': 'a', 'debug': 'd'}, {'job': 'api', 'debug': 'd'}),
                    ({'job': 'api'}, {'instance': 'b'}), ('7', '7.0'), ('100', '200'), originals):
                labels = dict(job='api', instance=id, debug='d')
                self.assertEqual(witness, dict(original_key=dict(metric='x', labels=labels), original_labels=labels,
                                              target_labels=context, exposition_labels=raw, value=value,
                                              timestamp_ms=stamp, line=1, scrape_id=id,
                                              source=f'job=api instance={id}', captured_at=TIME))
            candidates = Path(directory) / 'candidates.json'
            candidates.write_text(json.dumps(dict(schema_version=2, candidates=[
                dict(name=name, drop_labels=[label], metrics=['x'])
                for name, label in [('Identity', 'instance'), ('Debug', 'debug')]])))
            args = ['--scrape-set', path, '--candidate-file', candidates, '--rank-plans', *REQUIRED]
            proc = self.cli(*args, '--format', 'json')
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertEqual(proc.stderr, b'')
            self.assertEqual(json.loads(proc.stdout), result)
            text = self.cli(*args)
            self.assertEqual(text.returncode, 1, text.stderr)
            self.assertIn(b'Required target labels (nonempty on every sample): instance, job', text.stdout)

    def test_coverage_precedes_projection_for_late_and_unselected_samples(self):
        good = ('a', 'x{instance="a",debug="d"} 1\n', {'job': 'api'})
        with tempfile.TemporaryDirectory() as directory:
            for metric in ('x', 'unselected'):
                for missing in ('job', 'instance'):
                    for empty in (False, True):
                        # The first sample in the late capture has both labels:
                        # union presence must not hide the uncovered second sample.
                        for in_context in (False, True):
                            labels = {'job': 'api', 'instance': 'b', 'debug': 'd'}
                            labels.pop(missing)
                            context = {}
                            if empty:
                                (context if in_context else labels)[missing] = ''
                            first = {**labels, 'debug': 'first', missing: '' if empty and in_context else 'present'}
                            def sample(name, values):
                                return name + '{' + ','.join(f'{k}="{v}"' for k, v in values.items()) + '} 2\n'
                            text = '# exporter\n' + sample('x', first) + sample(metric, labels)
                            path = self.write_set(directory, [good, ('late', text, context)])
                            line = 2 if empty and in_context else 3
                            with self.subTest(metric=metric, missing=missing, empty=empty, context=in_context):
                                # Check baseline and scoped plans, in both CLI formats.
                                for options in ([], ['--drop-labels', 'debug', '--metric', 'x']):
                                    proc = self.cli('--scrape-set', path, *REQUIRED, *options, '--format', 'json')
                                    error = self.assert_invalid(proc)
                                    for fragment in ("scrape 'late'", f'line {line}', repr(missing), 'absent or empty'):
                                        self.assertIn(fragment, error)
                                text_proc = self.cli('--scrape-set', path, *REQUIRED)
                                self.assertEqual((text_proc.returncode, text_proc.stdout), (2, b''))
                                self.assertIn(f'line {line}'.encode(), text_proc.stderr)
                                with patch.object(analysis, '_analyze_validated', side_effect=AssertionError('projected')):
                                    with self.assertRaisesRegex(InputError, f"scrape 'late', line {line}"):
                                        analysis.report_scrapes(load_scrape_set(path), [['debug']], metrics=['x'],
                                                                required_target_labels=['job', 'instance'])

    def test_v2_unselected_late_metric_fails_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_set(directory, [
                ('a', 'x{job="api",instance="a",debug="d"} 1\n', None),
                ('late', 'x{job="api",instance="b",debug="d"} 2\ny{job="api"} 3\n', {})])
            candidates = Path(directory) / 'candidates.json'
            candidates.write_text(json.dumps(dict(schema_version=2, candidates=[
                dict(name='Only x', drop_labels=['debug'], metrics=['x'])])))
            error = self.assert_invalid(self.cli('--scrape-set', path, '--candidate-file', candidates,
                                                '--rank-plans', *REQUIRED, '--format', 'json'))
            self.assertIn("scrape 'late', line 2", error)
            self.assertIn("'instance'", error)

    def test_invalid_protected_duplicate_names_and_library_types(self):
        scrapes = [IdentifiedScrape('a', 'api', TIME, parse('x{job="api",instance="a"} 1\n'))]
        invalid_names = ['', ' job', 'job ', 'job\n', 'job,instance', 'bad-name', 'é', '1x',
                         '__name__', '__meta_job', 'le', 'quantile']
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_set(directory, [('a', 'x{job="api",instance="a"} 1\n', None)])
            for labels in [[name] for name in invalid_names] + [['job', 'job']]:
                with self.subTest(labels=labels):
                    with self.assertRaises(InputError):
                        analysis.report_scrapes(scrapes, required_target_labels=labels)
                    options = [option for label in labels for option in ('--require-target-label', label)]
                    self.assert_invalid(self.cli('--scrape-set', path, *options, '--format', 'json'))
        for labels in (None, True, 1, 'job', {'job'}, {'job': 'api'}, [None], [1], [True], [[]], [{}]):
            with self.subTest(labels=labels), self.assertRaises(InputError):
                analysis.report_scrapes(scrapes, required_target_labels=labels)

    def test_single_scrape_file_and_stdin_are_incompatible(self):
        for source in (ROOT / 'examples/cardinality/exporter.prom', '-'):
            error = self.assert_invalid(self.cli(source, *REQUIRED, '--format', 'json'))
            self.assertIn('--require-target-label requires --scrape-set', error)

    def test_default_omitted_empty_context_and_labels_keep_existing_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            results = []
            for context in (None, {}, {'job': '', 'instance': ''}):
                path = self.write_set(directory, [('a', 'x{debug="d",job="",instance=""} 1\n', context)])
                scrapes = load_scrape_set(path)
                baseline = analysis.report_scrapes(scrapes, [['debug']])
                self.assertEqual(baseline, analysis.report_scrapes(scrapes, [['debug']], required_target_labels=[]))
                self.assertEqual(baseline['required_target_labels'], [])
                self.assertEqual(baseline['summary']['observed_series'], 1)
                self.assertEqual(baseline['plans'][0]['label_pairs_removed'], 1)
                self.assertTrue(baseline['plans'][0]['safe'])
                results.append(baseline)
                # Provenance literally contains job and instance, but supplies neither.
                self.assert_invalid(self.cli('--scrape-set', path, *REQUIRED, '--format', 'json'))
            self.assertEqual(results[0], results[1])

    def test_exposition_only_and_equal_overlap_valid_names_and_sorted_errors(self):
        item = IdentifiedScrape('a', 'exporter', TIME,
                                parse('x{job="api",instance="a",_zone=" ",Job="UPPER"} 1\n'))
        required = ('job', '_zone', 'instance', 'Job')
        result = analysis.report_scrapes([item], required_target_labels=required)
        self.assertEqual(result['required_target_labels'], ['Job', '_zone', 'instance', 'job'])
        self.assertEqual(result['plans'], [])
        overlap = IdentifiedScrape('a', 'exporter', TIME, item.scrape, {'job': 'api'})
        self.assertEqual(analysis.report_scrapes([overlap], required_target_labels=required)['summary'], result['summary'])
        with self.assertRaisesRegex(InputError, "required target label 'a'"):
            analysis.report_scrapes([item], required_target_labels=['z', 'a'])


if __name__ == '__main__':
    unittest.main()
