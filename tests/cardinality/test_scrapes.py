# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from otelwrench.cardinality import analysis
from otelwrench.cardinality.parser import InputError, parse
from otelwrench.cardinality.scrapes import IdentifiedScrape, load_scrape_set

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "otelwrench", "cardinality"]


class ScrapeSetTests(unittest.TestCase):
    def test_locally_safe_but_union_collision_retains_cross_scrape_witnesses(self):
        # Hand-written oracle: two original identities -> one projected identity.
        first = parse('requests_total{pod="a",zone=""} 7 100\n')
        second = parse('requests_total{pod="b"} 7.0 200\n')
        scrapes = [IdentifiedScrape('morning', 'target-a', '2026-11-11T08:00:00Z', first),
                   IdentifiedScrape('evening', 'target-a', '2026-11-11T18:00:00+01:00', second)]
        result = analysis.report_scrapes(scrapes, [['pod']], rank_plans=True)
        self.assertEqual(result['schema_version'], 2)
        self.assertEqual([s['id'] for s in result['scrapes']], ['morning', 'evening'])
        self.assertEqual([s['plans'][0]['safe'] for s in result['scrapes']], [True, True])
        plan = result['plans'][0]
        self.assertEqual((plan['observed_keys_before'], plan['projected_keys_after']), (2, 1))
        self.assertEqual(plan['status'], 'unsafe_cross_scrape')
        self.assertTrue(plan['all_scrapes_safe'])
        self.assertEqual(plan['cross_scrape_collision_group_count'], 1)
        self.assertEqual(result['candidate_ranking']['ranked_safe_plans'], [])
        self.assertTrue(result['unsafe'])
        collision, = plan['collisions']
        self.assertEqual(collision['classification'], 'cross_scrape_only')
        self.assertEqual(collision['projected_key'], {'metric': 'requests_total', 'labels': {}})
        self.assertEqual(collision['original_series_count'], 2)
        self.assertEqual(collision['sample_value_relation'], 'equal')
        self.assertEqual([(w['scrape_id'], w['source'], w['captured_at'], w['value'], w['timestamp_ms'], w['line'])
                          for w in collision['originals']], [
            ('morning', 'target-a', '2026-11-11T08:00:00Z', '7', '100', 1),
            ('evening', 'target-a', '2026-11-11T18:00:00+01:00', '7.0', '200', 1)])
        self.assertEqual(collision['originals'][0]['original_labels'], {'pod': 'a', 'zone': ''})
        self.assertEqual(first.samples[0].value, '7')
        self.assertEqual(second.samples[0].value, '7.0')
        self.assertTrue(any('no guarantee for future' in w for w in result['warnings']))

    def identified(self, *texts):
        # Deliberately reverse chronological order; order is the caller's choice.
        return [IdentifiedScrape(f's{i}', f'target-{i}', f'2026-11-11T{20-i:02d}:00:00Z', parse(text))
                for i, text in enumerate(texts)]

    def run_cli(self, *args):
        return subprocess.run([*CLI, *map(str, args)], capture_output=True, cwd='/', timeout=60)

    def test_repeated_identity_with_changed_values_timestamps_and_empty_spelling_is_safe(self):
        scrapes = self.identified('x{pod="a",zone=""} 1 100\n', 'x{pod="a"} NaN 200\n')
        result = analysis.report_scrapes(scrapes, [['pod'], ['zone']], rank_plans=True)
        self.assertEqual(result['summary'], {'scrape_count': 2, 'sample_observations': 2, 'observed_series': 1})
        self.assertFalse(result['unsafe'])
        self.assertEqual([p['projected_keys_after'] for p in result['plans']], [1, 1])
        self.assertEqual([p['label_pairs_removed'] for p in result['plans']], [2, 1])
        self.assertTrue(all(not p['collisions'] for p in result['plans']))
        self.assertEqual([s['captured_at'] for s in result['scrapes']],
                         ['2026-11-11T20:00:00Z', '2026-11-11T19:00:00Z'])
        self.assertEqual(result['scrapes'][0]['summary']['families'][0]['labels']['zone']['empty_value_series'], 1)
        self.assertEqual(result['candidate_ranking']['scoring_scope'], 'sample_observations_across_scrapes')
        self.assertEqual([p['label_pairs_removed'] for p in result['candidate_ranking']['ranked_safe_plans']], [2, 1])

    def test_mixed_collision_types_count_identities_and_keep_all_observations(self):
        scrapes = self.identified('x{pod="a"} 1 100\nx{pod="b"} 2 100\ny{pod="a"} 1\n',
                                  'x{pod="a"} 99 200\ny{pod="b"} NaN\n')
        result = analysis.report_scrapes(scrapes, [['pod']])
        plan = result['plans'][0]
        self.assertEqual((plan['observed_keys_before'], plan['projected_keys_after']), (4, 2))
        self.assertEqual(plan['colliding_original_series'], 4)
        self.assertEqual(plan['status'], 'unsafe')
        self.assertFalse(plan['all_scrapes_safe'])
        self.assertEqual(plan['cross_scrape_collision_group_count'], 1)
        x, y = plan['collisions']
        self.assertEqual((x['classification'], x['original_series_count'], x['sample_observation_count']),
                         ('within_scrape', 2, 3))
        self.assertEqual([w['value'] for w in x['originals']], ['1', '2', '99'])
        self.assertEqual((y['classification'], y['sample_value_relation']), ('cross_scrape_only', 'indeterminate_nan'))
        local = result['scrapes'][0]['plans'][0]['collisions'][0]
        self.assertEqual([w['scrape_id'] for w in local['originals']], ['s0', 's0'])
        self.assertEqual([w['source'] for w in local['originals']], ['target-0', 'target-0'])
        # Different sources do not silently inject labels into canonical keys.
        self.assertTrue(result['unsafe'])

    def test_scope_and_labels_validate_over_union_not_each_scrape(self):
        scrapes = self.identified('x{pod="a"} 1\n', 'y{pod="a"} 1\ny{pod="b"} 2\n')
        result = analysis.report_scrapes(scrapes, [['pod']], metrics=['x'])
        self.assertFalse(result['unsafe'])
        self.assertEqual(result['plans'][0]['metric_counts'], {'x': {'before': 1, 'after': 1}, 'y': {'before': 2, 'after': 2}})
        self.assertEqual([s['plans'][0]['label_pairs_removed'] for s in result['scrapes']], [1, 0])
        self.assertEqual([s['plans'][0]['affected_metric_names'] for s in result['scrapes']], [['x'], []])
        for options in ({'metrics': ['absent']}, {'metrics': []}, {'include_config': True}):
            with self.subTest(options=options), self.assertRaises(InputError):
                analysis.report_scrapes(scrapes, [['pod']], **options)
        for labels in (['absent'], ['le'], ['pod', 'pod']):
            with self.subTest(labels=labels), self.assertRaises(InputError):
                analysis.report_scrapes(scrapes, [labels])
        with self.assertRaises(InputError):
            analysis.report_scrapes(scrapes, metrics=['x'])
        with self.assertRaises(InputError):
            analysis.report_scrapes([], [['pod']])

    def test_independent_union_oracle_for_overlapping_scrapes_and_candidate_combinations(self):
        from itertools import combinations
        # An original identity can recur with a new value. Empty labels are absent
        # for the independent key oracle but remain explicit observations.
        records = [[('x', {'a': str(i % 3), 'b': str(i % 2), 'id': str(i), 'empty': ''})
                    for i in range(start, start + 6)] for start in (0, 3, 6)]
        texts = [''.join(metric + '{' + ','.join(f'{k}="{v}"' for k, v in labels.items())
                         + f'}} {index}\n' for metric, labels in batch)
                 for index, batch in enumerate(records)]
        scrapes = self.identified(*texts)
        for size in (1, 2, 3):
            for dropped in combinations(('a', 'b', 'id'), size):
                expected = {}
                for index, batch in enumerate(records):
                    for line, (metric, labels) in enumerate(batch, 1):
                        before = (metric, frozenset((k, v) for k, v in labels.items() if v))
                        after = (metric, frozenset((k, v) for k, v in labels.items() if v and k not in dropped))
                        expected.setdefault(after, {}).setdefault(before, []).append((f's{index}', line, str(index)))
                with self.subTest(dropped=dropped):
                    plan = analysis.report_scrapes(scrapes, [dropped])['plans'][0]
                    self.assertEqual(plan['observed_keys_before'], 12)
                    self.assertEqual(plan['projected_keys_after'], len(expected))
                    collisions = {key: originals for key, originals in expected.items() if len(originals) > 1}
                    self.assertEqual(plan['unsafe'], bool(collisions))
                    self.assertEqual(plan['collision_group_count'], len(collisions))
                    for collision in plan['collisions']:
                        key = collision['projected_key']
                        originals = collisions[(key['metric'], frozenset(key['labels'].items()))]
                        self.assertEqual(collision['original_series_count'], len(originals))
                        self.assertEqual(sorted((w['scrape_id'], w['line'], w['value']) for w in collision['originals']),
                                         sorted(w for values in originals.values() for w in values))

    def test_installed_cli_manifest_relative_paths_json_text_and_ranking(self):
        args = ['--scrape-set', ROOT / 'examples/cardinality/scrape-set.json', '--drop-labels', 'pod']
        first = self.run_cli(*args, '--format', 'json')
        self.assertEqual(first.returncode, 1, first.stderr)
        self.assertFalse(first.stderr)
        self.assertEqual(first.stdout, self.run_cli(*args, '--format', 'json').stdout)
        result = json.loads(first.stdout)
        self.assertEqual(result['plans'][0]['status'], 'unsafe_cross_scrape')
        self.assertEqual([s['id'] for s in result['scrapes']], ['morning', 'evening'])
        text = self.run_cli(*args)
        self.assertEqual(text.returncode, 1, text.stderr)
        for fragment in (b'unsafe_cross_scrape', b'cross_scrape_only', b'"scrape_id": "morning"',
                         b'"timestamp_ms": "200"', b'2026-11-11T18:00:00+01:00'):
            self.assertIn(fragment, text.stdout)
        ranked = self.run_cli(*args, '--rank-plans', '--drop-labels', 'zone', '--format', 'json')
        self.assertEqual(ranked.returncode, 1, ranked.stderr)
        ranking = json.loads(ranked.stdout)['candidate_ranking']
        self.assertEqual([p['drop_labels'] for p in ranking['ranked_safe_plans']], [['zone']])
        text_ranking = self.run_cli(*args, '--rank-plans', '--drop-labels', 'zone')
        self.assertIn(b'Observed-union collision-free ranking', text_ranking.stdout)
        self.assertIn(b'1. plan-2', text_ranking.stdout)
        safe = self.run_cli('--scrape-set', ROOT / 'examples/cardinality/scrape-set.json', '--drop-labels', 'zone', '--format', 'json')
        self.assertEqual(safe.returncode, 0, safe.stderr)
        baseline = self.run_cli('--scrape-set', ROOT / 'examples/cardinality/scrape-set.json', '--format', 'json')
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        self.assertEqual(json.loads(baseline.stdout)['plans'], [])
        self.assertEqual(load_scrape_set(ROOT / 'examples/cardinality/scrape-set.json')[0].source, 'target-a')

    def test_manifest_and_late_scrape_errors_are_atomic(self):
        valid = {'id': 'a', 'source': 'target', 'captured_at': '2026-11-11T08:00:00Z', 'path': 'first.prom'}
        invalid = [[], {}, {'schema_version': True, 'scrapes': [valid]},
                   {'schema_version': 2, 'scrapes': [valid]}, {'schema_version': 1, 'scrapes': []},
                   {'schema_version': 1, 'scrapes': [valid], 'extra': 1}]
        bad_entries = [None, {}, {**valid, 'id': ''}, {**valid, 'source': []}, {**valid, 'path': '-'},
                       {**valid, 'path': 'missing.prom'}, {**valid, 'path': 'bad.prom'},
                       {**valid, 'path': ''}, {**valid, 'path': 'bad\x00path'}, {**valid, 'unknown': 'field'},
                       {**valid, 'captured_at': '2026-11-11'}, {**valid, 'captured_at': '2026-11-11T08:00:00'},
                       {**valid, 'captured_at': '2026-02-30T08:00:00Z'},
                       {**valid, 'captured_at': '2026-11-11T08:00:00+00:60'}]
        invalid += [{'schema_version': 1, 'scrapes': [entry]} for entry in bad_entries]
        invalid += [{'schema_version': 1, 'scrapes': [valid, valid]},
                    {'schema_version': 1, 'scrapes': [valid, {**valid, 'id': 'b', 'path': 'bad.prom'}]}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'first.prom').write_text('x{pod="a"} 1\n')
            (path / 'bad.prom').write_text('x{pod="a"} 1\nx{pod="a"} 2\n')
            manifest = path / 'set.json'
            for data in invalid:
                with self.subTest(data=data):
                    manifest.write_text(json.dumps(data))
                    result = self.run_cli('--scrape-set', manifest, '--drop-labels', 'pod', '--format', 'json')
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertFalse(result.stdout)
                    self.assertIn('error', json.loads(result.stderr))
            for data in (b'{', b'\xff', b'{"schema_version":1,"schema_version":1,"scrapes":[]}'):
                manifest.write_bytes(data)
                result = self.run_cli('--scrape-set', manifest, '--format', 'json')
                self.assertEqual(result.returncode, 2)
                self.assertFalse(result.stdout)
            # Incompatible flags and invalid candidates must also fail atomically.
            manifest.write_text(json.dumps({'schema_version': 1, 'scrapes': [valid]}))
            for extra in (['--emit-config'], ['--rank-plans', '--drop-labels', 'pod'],
                          ['--drop-labels', 'missing'], ['--metric', 'missing'],
                          [str(path / 'first.prom')]):
                result = self.run_cli('--scrape-set', manifest, '--drop-labels', 'pod', '--format', 'json', *extra)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertFalse(result.stdout)
                self.assertIn('error', json.loads(result.stderr))
        missing = self.run_cli('--format', 'json')
        self.assertEqual(missing.returncode, 2)
        self.assertFalse(missing.stdout)


if __name__ == '__main__':
    unittest.main()
