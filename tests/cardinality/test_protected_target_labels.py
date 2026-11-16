# SPDX-License-Identifier: Apache-2.0
"""Optional identity protection rejects removals before any candidate projection."""

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
PROTECTED = ['--protect-target-label', 'job', '--protect-target-label', 'instance']


class ProtectedTargetLabelTests(unittest.TestCase):
    def cli(self, *args):
        return subprocess.run([*CLI, *map(str, args)], cwd='/', capture_output=True, timeout=60)

    def invalid(self, proc):
        self.assertEqual((proc.returncode, proc.stdout), (2, b''), proc.stderr)
        return json.loads(proc.stderr)['error']

    def write_set(self, directory, captures):
        entries = []
        for id, text, context in captures:
            path = Path(directory) / f'{id}.prom'
            path.write_text(text, encoding='utf-8')
            entries.append(dict(id=id, source=f'job=api instance={id}', captured_at=TIME,
                                path=path.name, target_labels=context))
        path = Path(directory) / 'set.json'
        path.write_text(json.dumps(dict(schema_version=1, scrapes=entries)), encoding='utf-8')
        return path

    def mixed_set(self, directory):
        return self.write_set(directory, [
            ('a', 'x{instance="a",pod="p1",debug="d"} 1 100\n'
                  'x{instance="a",pod="p2",debug="d"} 2 101\n'
                  'y{instance="a",debug="d"} 3\n', {'job': 'api'}),
            ('b', 'x{job="api",pod="p1",debug="d"} 4 200\n'
                  'x{job="api",pod="p2",debug="d"} 5 201\n'
                  'y{job="api",debug="d"} 6\n', {'instance': 'b'})])

    def test_mixed_coverage_preserves_scoped_scores_rankings_and_all_witnesses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.mixed_set(directory)
            scrapes = load_scrape_set(path)
            plans = [['debug'], ['pod']]
            options = dict(plan_names=['Debug', 'Pods'], plan_metrics=[['x'], ['x']], rank_plans=True)
            baseline = analysis.report_scrapes(scrapes, plans, **options)
            labels = ['job', 'instance']
            result = analysis.report_scrapes(scrapes, plans, protected_target_labels=labels, **options)
            self.assertEqual(labels, ['job', 'instance'])
            self.assertEqual(baseline['protected_target_labels'], [])
            self.assertEqual(result['protected_target_labels'], ['instance', 'job'])
            self.assertEqual(result['required_target_labels'], [])
            self.assertEqual({k: v for k, v in result.items() if k != 'protected_target_labels'},
                             {k: v for k, v in baseline.items() if k != 'protected_target_labels'})
            self.assertEqual(result['summary'], dict(scrape_count=2, sample_observations=6, observed_series=6))
            safe, unsafe = result['plans']
            self.assertEqual((safe['projected_keys_after'], safe['label_pairs_removed'], safe['safe']), (6, 4, True))
            self.assertEqual((unsafe['projected_keys_after'], unsafe['label_pairs_removed'], unsafe['status']),
                             (4, 4, 'unsafe'))
            self.assertEqual([p['name'] for p in result['candidate_ranking']['ranked_safe_plans']], ['Debug'])
            self.assertEqual(unsafe['collision_group_count'], 2)
            witnesses = [w for c in unsafe['collisions'] for w in c['originals']]
            self.assertEqual([(w['scrape_id'], w['line'], w['value'], w['timestamp_ms']) for w in witnesses],
                             [('a', 1, '1', '100'), ('a', 2, '2', '101'),
                              ('b', 1, '4', '200'), ('b', 2, '5', '201')])
            for w in witnesses:
                self.assertEqual(w['original_labels']['instance'], w['scrape_id'])
                self.assertEqual(w['original_labels']['job'], 'api')
                self.assertEqual(w['original_key']['labels'], w['original_labels'])
                self.assertEqual(w['exposition_labels'] | w['target_labels'], w['original_labels'])
            candidates = Path(directory) / 'candidates.json'
            candidates.write_text(json.dumps(dict(schema_version=2, candidates=[
                dict(name=name, drop_labels=drop, metrics=['x'])
                for name, drop in zip(options['plan_names'], plans)])))
            args = ['--scrape-set', path, '--candidate-file', candidates, '--rank-plans', *PROTECTED]
            proc = self.cli(*args, '--format', 'json')
            self.assertEqual((proc.returncode, proc.stderr), (1, b''))
            self.assertEqual(json.loads(proc.stdout), result)
            text = self.cli(*args)
            self.assertEqual(text.returncode, 1, text.stderr)
            self.assertIn(b'Protected target labels (nonempty; removal forbidden): instance, job', text.stdout)

    def test_late_named_v2_and_unnamed_removals_fail_before_any_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.mixed_set(directory)
            scrapes = load_scrape_set(path)
            candidates = Path(directory) / 'candidates.json'
            candidates.write_text(json.dumps(dict(schema_version=2, candidates=[
                dict(name='Debug', drop_labels=['debug'], metrics=['x']),
                dict(name='Late identity', drop_labels=['job', 'instance'], metrics=['y'])])))
            for rank in (False, True):
                with self.subTest(rank=rank):
                    # Even the valid first plan must never reach projection or summarization.
                    with patch.object(analysis, '_analyze_validated', side_effect=AssertionError('projected')), \
                            patch.object(analysis, 'summarize', side_effect=AssertionError('summarized')):
                        with self.assertRaisesRegex(InputError, "candidate 'Late identity'.*protected target label 'instance'"):
                            analysis.report_scrapes(scrapes, [['debug'], ['job', 'instance']],
                                                    plan_names=['Debug', 'Late identity'], plan_metrics=[['x'], ['y']],
                                                    rank_plans=rank, protected_target_labels=['job', 'instance'])
                    args = ['--scrape-set', path, '--candidate-file', candidates, *PROTECTED]
                    if rank:
                        args.append('--rank-plans')
                    error = self.invalid(self.cli(*args, '--format', 'json'))
                    self.assertIn("candidate 'Late identity': cannot remove protected target label 'instance'", error)
                    text = self.cli(*args)
                    self.assertEqual((text.returncode, text.stdout), (2, b''))
                    self.assertIn(b"candidate 'Late identity'", text.stderr)
            for version in (1, 2):
                candidate = dict(name='Identity', drop_labels=['instance'])
                if version == 2:
                    candidate['metrics'] = ['y']
                candidates.write_text(json.dumps(dict(schema_version=version, candidates=[candidate])))
                self.assertIn("candidate 'Identity'", self.invalid(self.cli(
                    '--scrape-set', path, '--candidate-file', candidates, *PROTECTED, '--format', 'json')))
            self.assertIn("candidate 'plan-2'", self.invalid(self.cli(
                '--scrape-set', path, '--drop-labels', 'debug', '--drop-labels', 'instance',
                '--metric', 'y', *PROTECTED, '--format', 'json')))

    def test_protection_implies_per_sample_coverage_even_without_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            good = ('a', 'x{job="api",instance="a",debug="d"} 1\n', {})
            for empty in (False, True):
                for context_value in (False, True):
                    for label in ('job', 'instance'):
                        context = {label: ''} if empty and context_value else {}
                        first = {'job': 'api', 'instance': 'b', 'debug': 'd'}
                        late = {'job': 'api', 'instance': 'b'}
                        late.pop(label)
                        if empty:
                            (context if context_value else late)[label] = ''
                            if context_value:
                                first[label] = ''  # avoid a different context-conflict error
                        def sample(metric, values):
                            return metric + '{' + ','.join(f'{k}="{v}"' for k, v in values.items()) + '} 2\n'
                        path = self.write_set(directory, [good, ('late', '# capture\n' + sample('x', first)
                                                               + sample('unselected', late), context)])
                        line = 2 if empty and context_value else 3
                        for plans in ([], ['--drop-labels', 'debug', '--metric', 'x']):
                            with self.subTest(empty=empty, context=context_value, label=label, plans=plans):
                                error = self.invalid(self.cli('--scrape-set', path, *PROTECTED,
                                                              *plans, '--format', 'json'))
                                self.assertIn(f"scrape 'late', line {line}: required target label {label!r} is absent or empty", error)
                        with patch.object(analysis, '_analyze_validated', side_effect=AssertionError('projected')):
                            with self.assertRaisesRegex(InputError, f"scrape 'late', line {line}"):
                                analysis.report_scrapes(load_scrape_set(path), [['debug']], plan_metrics=[['x']],
                                                        protected_target_labels=['job', 'instance'])
            # Provenance says job/instance, but still supplies no coverage.
            path = self.write_set(directory, [('a', 'x 1\n', {})])
            self.assertIn("'instance' is absent or empty", self.invalid(self.cli(
                '--scrape-set', path, *PROTECTED, '--format', 'json')))

    def test_names_types_and_single_scrape_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.mixed_set(directory)
            scrapes = load_scrape_set(path)
            for labels in [[name] for name in ('', ' job', 'job ', 'job\n', 'job,instance', 'bad-name',
                                               'é', '1x', '__name__', '__meta_job', 'le', 'quantile')] + [['job', 'job']]:
                with self.subTest(labels=labels):
                    with self.assertRaises(InputError):
                        analysis.report_scrapes(scrapes, protected_target_labels=labels)
                    args = [arg for label in labels for arg in ('--protect-target-label', label)]
                    self.invalid(self.cli('--scrape-set', path, *args, '--format', 'json'))
            for labels in (None, True, 1, 'job', {'job'}, {'job': 'api'}, [None], [1], [True], [[]], [{}]):
                with self.subTest(labels=labels), self.assertRaises(InputError):
                    analysis.report_scrapes(scrapes, protected_target_labels=labels)
            for source in (ROOT / 'examples/cardinality/exporter.prom', '-'):
                self.assertIn('--protect-target-label requires --scrape-set', self.invalid(self.cli(
                    source, *PROTECTED, '--format', 'json')))

    def test_required_only_is_droppable_overlap_is_valid_and_default_preserves_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.mixed_set(directory)
            scrapes = load_scrape_set(path)
            base = analysis.report_scrapes(scrapes, [['instance']], required_target_labels=['job', 'instance'])
            self.assertTrue(base['unsafe'])
            self.assertEqual(base['protected_target_labels'], [])
            self.assertEqual(base, analysis.report_scrapes(scrapes, [['instance']],
                                                          required_target_labels=['job', 'instance'],
                                                          protected_target_labels=[]))
            for labels in (['job', 'instance'], ('instance', 'job')):
                result = analysis.report_scrapes(scrapes, required_target_labels=['job'], protected_target_labels=labels)
                self.assertEqual(result['protected_target_labels'], ['instance', 'job'])
                self.assertEqual(result['required_target_labels'], ['job'])
            for context in ({}, {'job': ''}):
                item = IdentifiedScrape('a', 'job=api', TIME, parse('x{job="",debug="d"} 1\n'), context)
                result = analysis.report_scrapes([item], [['job']])
                self.assertTrue(result['plans'][0]['safe'])
                self.assertEqual(result['protected_target_labels'], [])
            # Exact equal overlap, valid ASCII case/underscore names, whitespace is nonempty.
            item = IdentifiedScrape('a', 'api', TIME, parse('x{Job="api",_zone=" "} 1\n'), {'Job': 'api'})
            result = analysis.report_scrapes([item], protected_target_labels=('_zone', 'Job'))
            self.assertEqual(result['protected_target_labels'], ['Job', '_zone'])
            # Coverage uses sorted union of required and protected names.
            with self.assertRaisesRegex(InputError, "required target label 'a'"):
                analysis.report_scrapes([item], required_target_labels=['z'], protected_target_labels=['a'])


if __name__ == '__main__':
    unittest.main()
