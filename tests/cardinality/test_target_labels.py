# SPDX-License-Identifier: Apache-2.0
"""Explicit target context: effective identities, exact conflicts and raw evidence."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from otelwrench.cardinality import analysis
from otelwrench.cardinality.parser import InputError, parse
from otelwrench.cardinality.scrapes import IdentifiedScrape, load_scrape_set

CLI = [sys.executable, "-m", "otelwrench", "cardinality"]


class TargetLabelTests(unittest.TestCase):
    def entry(self, id='a', **extra):
        return dict(id=id, source='exporter', captured_at='2026-11-11T08:00:00Z',
                    path='exporter.prom', **extra)

    def write_set(self, directory, entries, text='requests_total{debug="x"} 7 100\n'):
        directory = Path(directory)
        (directory / 'exporter.prom').write_text(text, encoding='utf-8')
        path = directory / 'set.json'
        path.write_text(json.dumps(dict(schema_version=1, scrapes=entries)), encoding='utf-8')
        return path

    def cli(self, path, *options):
        return subprocess.run([*CLI, '--scrape-set', str(path), *options],
                              cwd='/', capture_output=True, timeout=60)

    def assert_invalid(self, proc):
        self.assertEqual(proc.returncode, 2, (proc.stdout, proc.stderr))
        self.assertEqual(proc.stdout, b'')
        self.assertEqual(json.loads(proc.stderr)['error_type'], 'invalid_or_unsupported_input')

    def test_distinct_targets_have_two_identities_and_complete_collision_evidence(self):
        entries = [self.entry(id, target_labels={'job': 'api', 'instance': id}) for id in ('a', 'b')]
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_set(directory, entries)
            inputs = {p: p.read_bytes() for p in Path(directory).iterdir()}
            loaded = load_scrape_set(path)
            self.assertEqual([dict(item.scrape.samples[0].labels) for item in loaded], [{'debug': 'x'}] * 2)
            self.assertEqual([dict(item.target_labels) for item in loaded], [e['target_labels'] for e in entries])
            proc = self.cli(path, '--drop-labels', 'instance', '--drop-labels', 'debug',
                            '--rank-plans', '--format', 'json')
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertFalse(proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result['schema_version'], 2)
            self.assertEqual(result['summary'], dict(scrape_count=2, sample_observations=2, observed_series=2))
            unsafe, safe = result['plans']
            self.assertEqual((unsafe['observed_keys_before'], unsafe['projected_keys_after'], unsafe['status']),
                             (2, 1, 'unsafe_cross_scrape'))
            self.assertEqual((unsafe['label_pairs_before'], unsafe['label_pairs_after']), (6, 4))
            self.assertTrue(unsafe['all_scrapes_safe'])
            self.assertEqual((safe['projected_keys_after'], safe['safe'], safe['label_pairs_removed']), (2, True, 2))
            self.assertEqual([p['name'] for p in result['candidate_ranking']['ranked_safe_plans']], ['plan-2'])
            collision, = unsafe['collisions']
            self.assertEqual((collision['classification'], collision['sample_value_relation']), ('cross_scrape_only', 'equal'))
            self.assertEqual((collision['original_series_count'], collision['sample_observation_count']), (2, 2))
            self.assertEqual(collision['projected_key'], dict(metric='requests_total', labels={'debug': 'x', 'job': 'api'}))
            for entry, scrape, witness in zip(entries, result['scrapes'], collision['originals']):
                context = entry['target_labels']
                self.assertEqual(scrape['target_labels'], context)
                self.assertEqual(witness, dict(
                    original_key=dict(metric='requests_total', labels={**context, 'debug': 'x'}),
                    original_labels={**context, 'debug': 'x'}, exposition_labels={'debug': 'x'},
                    target_labels=context, value='7', timestamp_ms='100', line=1,
                    scrape_id=entry['id'], source=entry['source'], captured_at=entry['captured_at']))
                self.assertTrue(all(p['safe'] for p in scrape['plans']))
                stats = scrape['summary']['families'][0]['labels']
                self.assertEqual(stats['instance']['values'], [entry['id']])
            text = self.cli(path, '--drop-labels', 'instance')
            self.assertEqual(text.returncode, 1, text.stderr)
            for fragment in (b'"target_labels":', b'"exposition_labels":', b'unsafe_cross_scrape'):
                self.assertIn(fragment, text.stdout)
            baseline = self.cli(path)
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            self.assertIn(b'"target_labels":', baseline.stdout)
            self.assertEqual(inputs, {p: p.read_bytes() for p in Path(directory).iterdir()})

    def test_equal_conflicts_count_once_empty_and_unicode_values_are_preserved(self):
        context = {'job': 'api', 'instance': 'a', 'zone': '', 'note': 'région\n"\\'}
        original = parse('x{job="api",debug="d"} 1\ny{job="api",zone=""} 2\n')
        item = IdentifiedScrape('a', 'exporter', '2026-11-11T08:00:00Z', original, target_labels=context)
        result = analysis.report_scrapes([item], [['job'], ['zone']], metrics=['x'], rank_plans=True)
        self.assertEqual(result['summary']['observed_series'], 2)
        self.assertEqual([p['label_pairs_before'] for p in result['plans']], [9, 9])
        self.assertEqual([p['label_pairs_removed'] for p in result['plans']], [1, 1])
        self.assertEqual(result['plans'][0]['metric_counts'], {'x': {'before': 1, 'after': 1}, 'y': {'before': 1, 'after': 1}})
        stats = result['scrapes'][0]['summary']['families'][0]['labels']
        self.assertEqual(stats['zone']['empty_value_series'], 1)
        self.assertEqual(stats['note']['values'], [context['note']])
        self.assertIs(item.scrape, original)
        self.assertEqual(dict(original.samples[0].labels), {'debug': 'd', 'job': 'api'})
        self.assertEqual(context, dict(item.target_labels))
        # Same identity supplied by context or exposition is a repeated observation.
        other = IdentifiedScrape('b', 'elsewhere', '2026-11-11T09:00:00Z',
                                 parse('x{job="api",instance="a",zone="",note="région\\n\\"\\\\",debug="d"} 9\n'))
        merged = analysis.report_scrapes([item, other], [['debug']])
        self.assertEqual(merged['summary']['observed_series'], 2)  # x and y
        self.assertFalse(merged['unsafe'])

    def test_within_scrape_witnesses_keep_effective_and_exposition_labels(self):
        item = IdentifiedScrape('s', 'target', '2026-11-11T08:00:00Z',
                                 parse('x{pod="a"} 1 10\nx{pod="b"} 2 20\n'),
                                 target_labels={'instance': 'a'})
        result = analysis.report_scrapes([item], [['pod']])
        local = result['scrapes'][0]['plans'][0]['collisions'][0]
        union = result['plans'][0]['collisions'][0]
        self.assertEqual(local['originals'], union['originals'])
        self.assertEqual(union['classification'], 'within_scrape')
        self.assertEqual([w['exposition_labels'] for w in local['originals']], [{'pod': 'a'}, {'pod': 'b'}])
        self.assertEqual([w['original_labels']['instance'] for w in local['originals']], ['a', 'a'])

    def test_omitted_empty_and_reordered_context_do_not_infer_provenance_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            entries = [self.entry(id) for id in ('a', 'b')]
            path = self.write_set(directory, entries)
            omitted = analysis.report_scrapes(load_scrape_set(path), [['debug']])
            for entry in entries:
                entry['target_labels'] = {}
            path = self.write_set(directory, entries)
            empty = analysis.report_scrapes(load_scrape_set(path), [['debug']])
            self.assertEqual(omitted, empty)
            self.assertEqual(empty['summary']['observed_series'], 1)
            self.assertFalse(empty['unsafe'])
            for entry in entries:
                entry['target_labels'] = {'zone': '', 'instance': 'a'}
            path = self.write_set(directory, entries)
            first = self.cli(path, '--drop-labels', 'zone', '--format', 'json')
            for entry in entries:
                entry['target_labels'] = dict(reversed(list(entry['target_labels'].items())))
            path = self.write_set(directory, entries)
            second = self.cli(path, '--drop-labels', 'zone', '--format', 'json')
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual(json.loads(first.stdout)['summary']['observed_series'], 1)

    def test_invalid_context_and_late_conflicts_fail_atomically_even_outside_scope(self):
        invalid = [None, [], '', 1, True, {'': 'v'}, {'bad-name': 'v'}, {'é': 'v'}, {'1x': 'v'},
                   {' job': 'v'}, {'__name__': 'x'}, {'__meta_job': 'v'}, {'le': '1'}, {'quantile': '0.5'},
                   *[{'job': v} for v in (None, [], {}, 1, True, 'a\x00b', 'a\rb', '\ud800')],
                   {'job': 'different'}, {'zone': 'nonempty'}]
        text = 'x{debug="x",job="api",zone=""} 1\ny{job="api",zone=""} 2\n'
        with tempfile.TemporaryDirectory() as directory:
            for context in invalid:
                path = self.write_set(directory, [self.entry(), self.entry('b', target_labels=context)], text)
                with self.subTest(context=context):
                    for options in ([], ['--drop-labels', 'debug', '--metric', 'x']):
                        proc = self.cli(path, *options, '--format', 'json')
                        self.assert_invalid(proc)
                        self.assertIn(b'target', proc.stderr)
            # Conflict is checked on every sample, even outside selected metric x.
            path = self.write_set(directory, [self.entry(target_labels={'job': 'api'})],
                                  'x{debug="x",job="api"} 1\ny{job="other"} 2\n')
            self.assert_invalid(self.cli(path, '--drop-labels', 'debug', '--metric', 'x', '--format', 'json'))
            # Duplicate JSON label keys must not be silently overwritten.
            path.write_text('{"schema_version":1,"scrapes":[' + json.dumps(self.entry(target_labels={'job': 'api'})).replace(
                '"job": "api"', '"job": "api", "job": "api"') + ']}')
            proc = self.cli(path, '--format', 'json')
            self.assert_invalid(proc)
            self.assertIn(b'duplicate JSON field', proc.stderr)

    def test_context_induced_duplicate_identities_are_invalid_input_not_drop_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            for text, context in [('x{instance="a"} 1 10\nx 2 20\n', {'instance': 'a'}),
                                  ('x{instance="a",zone=""} 1\nx{zone=""} 2\n', {'instance': 'a'})]:
                path = self.write_set(directory, [self.entry(target_labels=context)], text)
                proc = self.cli(path, '--format', 'json')
                self.assert_invalid(proc)
                self.assertIn(b'duplicate', proc.stderr)
                self.assertIn(b'line 2', proc.stderr)
                item = IdentifiedScrape('a', 'target', '2026-11-11T08:00:00Z', parse(text), target_labels=context)
                with self.assertRaisesRegex(InputError, 'duplicate'):
                    analysis.report_scrapes([item])
        for context in ([], {'__name__': 'x'}, {'job': 1}, {1: 'x'}):
            item = IdentifiedScrape('a', 'target', '2026-11-11T08:00:00Z', parse('x 1\n'), target_labels=context)
            with self.subTest(context=context), self.assertRaises(InputError):
                analysis.report_scrapes([item])


if __name__ == '__main__':
    unittest.main()
