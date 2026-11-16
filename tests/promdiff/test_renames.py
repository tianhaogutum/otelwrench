"""Explicit declarations are separate from observed compatibility evidence."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from prometheus_client import CollectorRegistry, Counter, Histogram, Summary, generate_latest

from otelwrench.promdiff.compare import compare
from otelwrench.promdiff.parser import InputError, parse_snapshot


BEFORE = '# TYPE old_requests_total counter\nold_requests_total{verb="get"} 1\n'
AFTER = '# TYPE requests_total counter\nrequests_total{method="get"} 2\n'
MAP = {'metrics': {'old_requests_total': 'requests_total'},
       'labels': {'old_requests_total': {'verb': 'method'}}}


def report(before, after, mapping, **kwargs):
    return compare(parse_snapshot(before), parse_snapshot(after), rename_map=mapping, **kwargs)


class RenameTests(unittest.TestCase):
    def test_explicit_rename_matches_identity_without_mutating_inputs(self):
        left, right = parse_snapshot(BEFORE), parse_snapshot(AFTER)
        originals = deepcopy((left, right, MAP))
        result = compare(left, right, rename_map=MAP)
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['changes'], [])
        self.assertEqual(result['summary'], {'failures': 0, 'warnings': 0, 'allowed': 0})
        self.assertEqual(result['observed_series']['new'], 0)
        self.assertEqual(result['observed_series']['missing'], 0)
        self.assertEqual(result['observed_series']['by_family'], [
            {'family': 'requests_total', 'baseline_present': True, 'candidate_present': True,
             'baseline': 1, 'candidate': 1, 'new': 0, 'missing': 0}])
        self.assertEqual(result['declared_renames'], MAP)
        self.assertIn('old_requests_total', result['contracts']['baseline'])
        self.assertEqual(result['contracts']['comparison_baseline'], result['contracts']['candidate'])
        self.assertEqual((left, right, MAP), originals)

    def test_unlisted_names_and_empty_map_keep_existing_behavior(self):
        result = compare(parse_snapshot(BEFORE), parse_snapshot(AFTER))
        self.assertEqual({e['kind'] for e in result['changes']}, {'family_added', 'family_removed'})
        self.assertNotIn('declared_renames', result)
        for mapping in [{}, {'metrics': {}, 'labels': {}}]:
            mapped = report(BEFORE, AFTER, mapping)
            self.assertEqual(mapped['changes'], result['changes'])
        result = report(BEFORE + 'unlisted 1\n', AFTER + 'different 1\n', MAP)
        self.assertEqual({(e['kind'], e['family']) for e in result['changes']},
                         {('family_removed', 'unlisted'), ('family_added', 'different')})

    def test_labels_are_scoped_to_original_family_and_names_are_simultaneous(self):
        before = 'a{x="one",y="two"} 1\nb{x="three"} 2\n'
        after = 'b{y="one",x="two"} 9\na{x="three"} 8\n'
        result = report(before, after, {'metrics': {'a': 'b', 'b': 'a'},
                                        'labels': {'a': {'x': 'y', 'y': 'x'}}})
        self.assertEqual(result['changes'], [])
        labels_only = report('a{x="one"} 1\nb{x="one"} 1\n',
                             'a{y="one"} 1\nb{z="one"} 1\n',
                             {'labels': {'a': {'x': 'y'}}})
        self.assertEqual({e['family'] for e in labels_only['changes']}, {'b'})

    def test_real_pinned_client_composites_counter_and_created_are_explicit(self):
        def generated(prefix, label):
            registry = CollectorRegistry()
            Counter(prefix + '_requests', 'Requests.', [label], registry=registry).labels('a').inc()
            Histogram(prefix + '_latency', 'Latency.', [label], buckets=[1], registry=registry).labels('a').observe(.5)
            Summary(prefix + '_size', 'Size.', [label], registry=registry).labels('a').observe(2)
            return generate_latest(registry).decode()
        before, after = generated('old', 'verb'), generated('new', 'method')
        families = ['requests_total', 'requests_created', 'latency', 'latency_created', 'size', 'size_created']
        mapping = {'metrics': {'old_' + f: 'new_' + f for f in families},
                   'labels': {'old_' + f: {'verb': 'method'} for f in families}}
        result = report(before, after, mapping)
        self.assertEqual(result['changes'], [])
        self.assertEqual(result['observed_series']['new'], 0)
        self.assertEqual(result['candidate']['families'], 6)
        del mapping['metrics']['old_requests_created']
        result = report(before, after, mapping)
        self.assertEqual({e['kind'] for e in result['changes']}, {'family_added', 'family_removed'})

    def test_summary_quantiles_and_metadata_only_families(self):
        before = '# TYPE s summary\ns{verb="a",quantile="0.5"} 1\ns_sum{verb="a"} 1\ns_count{verb="a"} 1\n'
        after = before.replace('# TYPE s summary', '# TYPE t summary').replace('s{', 't{').replace('s_', 't_').replace('verb', 'method')
        self.assertEqual(report(before, after, {'metrics': {'s': 't'}, 'labels': {'s': {'verb': 'method'}}})['changes'], [])
        self.assertEqual(report('# TYPE h histogram\n', '# TYPE j histogram\n',
                                {'metrics': {'h': 'j'}})['changes'], [])

    def test_observed_changes_policy_and_allowlist_use_projected_names(self):
        candidate = AFTER.replace('counter', 'gauge').replace('"get"', '"post"')
        result = report(BEFORE, candidate, MAP)
        self.assertEqual({e['kind'] for e in result['changes']},
                         {'type_changed', 'series_added', 'series_missing'})
        self.assertEqual(result['observed_series']['new'], 1)
        self.assertEqual(result['observed_series']['missing'], 1)
        self.assertTrue(all(e['family'] == 'requests_total' for e in result['changes']))
        allowlist = [{k: e[k] for k in ('kind', 'family', 'before', 'after')}
                     for e in result['changes'] if e['kind'] == 'type_changed']
        allowed = report(BEFORE, candidate, MAP, allowlist=allowlist)
        self.assertEqual(allowed['status'], 'warn')
        self.assertEqual(allowed['summary']['allowed'], 1)
        self.assertEqual(report(BEFORE, '', MAP)['changes'][0]['kind'], 'family_removed')

    def test_declarations_do_not_hide_schema_changes_or_series_growth(self):
        candidate = AFTER.replace('method="get"', 'method="get",region="eu"')
        result = report(BEFORE, candidate, MAP)
        event = next(e for e in result['changes'] if e['kind'] == 'label_schema_changed')
        self.assertEqual(event['before'], [{'name': 'requests_total', 'label_keys': ['method']}])
        self.assertEqual(event['after'], [{'name': 'requests_total', 'label_keys': ['method', 'region']}])
        self.assertEqual(result['status'], 'fail')
        candidate = AFTER + 'requests_total{method="post"} 1\n'
        result = report(BEFORE, candidate, MAP)
        self.assertEqual({e['kind'] for e in result['changes']}, {'series_added', 'series_growth'})
        self.assertEqual(result['observed_series']['new'], 1)
        self.assertEqual(result['observed_series']['missing'], 0)
        self.assertEqual(result['observed_series']['net_growth_percent'], 100)

    def test_many_to_one_and_unmapped_destination_collisions(self):
        for mapping in [{'metrics': {'a': 'c', 'b': 'c'}}, {'metrics': {'a': 'b'}},
                        {'labels': {'a': {'x': 'z', 'y': 'z'}}},
                        {'labels': {'a': {'x': 'y'}}}]:
            with self.subTest(mapping=mapping), self.assertRaisesRegex(InputError, 'one-to-one|collision'):
                report('a{x="1",y="2"} 1\nb 1\n', 'c 1\n', mapping)

    def test_reserved_component_names_even_without_observed_samples(self):
        cases = [
            ('# TYPE h histogram\nx 1\n', '', {'metrics': {'h': 'j', 'x': 'j_count'}}),
            ('# TYPE s summary\nx 1\n', '', {'metrics': {'s': 't', 'x': 't_sum'}}),
            ('x 1\n', '# TYPE h histogram\n', {'metrics': {'x': 'h_bucket'}}),
            ('# TYPE h histogram\n', 'j_count 1\n', {'metrics': {'h': 'j'}}),
        ]
        for before, after, mapping in cases:
            with self.subTest(mapping=mapping), self.assertRaisesRegex(InputError, 'collision'):
                report(before, after, mapping)
        # Literal untyped suffix names remain ordinary families.
        self.assertEqual(report('x_count 1\n', 'y_count 1\n',
                                {'metrics': {'x_count': 'y_count'}})['changes'], [])

    def test_reserved_labels_cannot_reinterpret_composite_components(self):
        h = '# TYPE h histogram\nh_bucket{x="a",le="+Inf"} 1\nh_sum{x="a"} 1\nh_count{x="a"} 1\n'
        s = '# TYPE s summary\ns{x="a",quantile="0.5"} 1\ns_sum{x="a"} 1\ns_count{x="a"} 1\n'
        for text, family, old, new in [(h, 'h', 'le', 'bound'), (h, 'h', 'x', 'le'),
                                       (s, 's', 'quantile', 'q'), (s, 's', 'x', 'quantile')]:
            with self.subTest(old=old, new=new), self.assertRaisesRegex(InputError, 'reserved'):
                report(text, text, {'labels': {family: {old: new}}})
        self.assertEqual(report('x{le="a"} 1\n', 'x{bound="a"} 1\n',
                                {'labels': {'x': {'le': 'bound'}}})['changes'], [])

    def test_strict_map_validation_and_stale_sources(self):
        invalid = [[], {'unknown': {}}, {'metrics': []}, {'labels': []},
                   {'metrics': {'bad*': 'x'}}, {'metrics': {'x': 1}}, {'metrics': {'x': 'x'}},
                   {'metrics': {'missing': 'y'}}, {'labels': {'missing': {'a': 'b'}}},
                   {'labels': {'x': []}}, {'labels': {'x': {'a': '__name__'}}},
                   {'labels': {'x': {'a:b': 'c'}}}, {'labels': {'x': {'missing': 'b'}}}]
        for mapping in invalid:
            with self.subTest(mapping=mapping), self.assertRaises(InputError):
                report('x{a="v"} 1\n', 'y 1\n', mapping)

    def test_determinism_for_map_and_series_order(self):
        before = 'a{x="1",y="2"} 1\na{x="3",y="4"} 1\nb 1\n'
        after = 'c{v="2",u="1"} 1\nc{v="4",u="3"} 1\nd 1\n'
        first = {'metrics': {'a': 'c', 'b': 'd'}, 'labels': {'a': {'x': 'u', 'y': 'v'}}}
        second = {'labels': {'a': {'y': 'v', 'x': 'u'}}, 'metrics': {'b': 'd', 'a': 'c'}}
        self.assertEqual(report(before, after, first), report(before, after, second))

    def test_cli_reports_declarations_and_errors_without_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before, after, mapping = [root / name for name in ('before.prom', 'after.prom', 'map.json')]
            before.write_text(BEFORE)
            after.write_text(AFTER)
            def run(*flags):
                return subprocess.run([sys.executable, '-m', 'otelwrench', 'promdiff', str(before), str(after),
                                       '--rename-map', str(mapping), *flags],
                                      text=True, capture_output=True, timeout=60)
            mapping.write_text(json.dumps(MAP))
            original = {p: p.read_bytes() for p in (before, after, mapping)}
            result = run('--format', 'json')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['declared_renames'], MAP)
            self.assertEqual(json.loads(result.stdout)['changes'], [])
            self.assertIn('DECLARED metric old_requests_total -> requests_total', run().stdout)
            self.assertIn('DECLARED label old_requests_total: verb -> method', run().stdout)
            self.assertEqual(original, {p: p.read_bytes() for p in original})
            for bad in ['null', '{"metrics":{"a":"b","a":"c"}}',
                        '{"metrics":{"old_requests_total":"x","other":"x"}}',
                        '{"labels":{"old_requests_total":{"verb":"__reserved"}}}']:
                mapping.write_text(bad)
                result = run('--format', 'json')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertEqual(json.loads(result.stderr)['status'], 'error')
                self.assertNotIn('Traceback', result.stderr)
            # Data-dependent collisions are also rejected before either report format.
            cases = [('a{x="1",y="2"} 1\n', 'a 1\n', {'labels': {'a': {'x': 'y'}}}),
                     ('# TYPE h histogram\nx 1\n', '', {'metrics': {'x': 'h_count'}}),
                     ('x 1\n', '# TYPE h histogram\n', {'metrics': {'x': 'h_sum'}})]
            for left, right, declared in cases:
                before.write_text(left)
                after.write_text(right)
                mapping.write_text(json.dumps(declared))
                for flags in [(), ('--format', 'json')]:
                    result = run(*flags)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('collision', result.stderr)


if __name__ == '__main__':
    unittest.main()
