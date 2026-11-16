"""Wire evidence remains inspectable without changing the projected gate."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from otelwrench.promdiff.cli import text_report
from otelwrench.promdiff.compare import Config, compare
from otelwrench.promdiff.parser import InputError, parse_snapshot


BEFORE = '# TYPE old_requests_total counter\nold_requests_total{verb="private-value"} 1 1000\n'
AFTER = '# TYPE requests_total counter\nrequests_total{method="private-value"} 99 2000\n'
MAP = {'metrics': {'old_requests_total': 'requests_total'},
       'labels': {'old_requests_total': {'verb': 'method'}}}


def event(kind, family, before, after):
    return dict(kind=kind, family=family, before=before, after=after)


def contract(name, key):
    return {'type': 'counter', 'help': None, 'unit': None,
            'samples': [{'name': name, 'label_keys': [key]}]}


def result(before=BEFORE, after=AFTER, mapping=MAP, **kwargs):
    return compare(parse_snapshot(before), parse_snapshot(after), rename_map=mapping, **kwargs)


class RawChangesTests(unittest.TestCase):
    def test_metric_rename_exposes_exact_wire_events_without_changing_gate(self):
        ordinary = result()
        self.assertNotIn('raw_changes', ordinary)
        self.assertEqual(result(include_raw_changes=False), ordinary)
        report = result(include_raw_changes=True)
        raw = report.pop('raw_changes')
        self.assertEqual(report, ordinary)
        self.assertEqual(report['status'], 'pass')
        self.assertEqual(raw, [
            event('family_removed', 'old_requests_total', contract('old_requests_total', 'verb'), None),
            event('family_added', 'requests_total', None, contract('requests_total', 'method'))])

    def test_label_only_rename_exposes_schema_and_churn_in_wire_naming(self):
        before = 'queue{old="private-value"} 1\n'
        after = 'queue{new="private-value"} 2\n'
        report = result(before, after, {'labels': {'queue': {'old': 'new'}}}, include_raw_changes=True)
        self.assertEqual(report['changes'], [])
        self.assertEqual(report['raw_changes'], [
            event('label_schema_changed', 'queue', [{'name': 'queue', 'label_keys': ['old']}],
                  [{'name': 'queue', 'label_keys': ['new']}]),
            event('series_added', 'queue', 0, 1), event('series_missing', 'queue', 1, 0)])
        self.assertNotIn('private-value', json.dumps(report))

    def test_raw_evidence_includes_unlisted_type_help_and_growth_events(self):
        before = BEFORE + '# HELP x Old\n# TYPE x gauge\nx 1\n'
        after = AFTER + '# HELP x New\n# TYPE x counter\nx 2\ny 1\n'
        report = result(before, after, include_raw_changes=True)
        self.assertEqual(report['status'], 'fail')
        self.assertEqual(report['raw_changes'], [
            event('series_growth', None, 2, 3),
            event('family_removed', 'old_requests_total', contract('old_requests_total', 'verb'), None),
            event('family_added', 'requests_total', None, contract('requests_total', 'method')),
            event('help_changed', 'x', 'Old', 'New'), event('type_changed', 'x', 'gauge', 'counter'),
            event('family_added', 'y', None, {'type': 'untyped', 'help': None, 'unit': None,
                                          'samples': [{'name': 'y', 'label_keys': []}]})])
        relaxed = result(before, after, include_raw_changes=True,
                         config=Config.from_dict({'max_series_growth_percent': 50,
                                                  'policy': {'family_removed': 'warn'}}))
        self.assertNotIn('series_growth', [e['kind'] for e in relaxed['raw_changes']])
        self.assertTrue(all(set(e) == {'kind', 'family', 'before', 'after'}
                            for e in relaxed['raw_changes']))

    def test_allowlist_remains_projected_and_never_marks_raw_events_allowed(self):
        after = AFTER.replace('counter', 'gauge')
        expected = event('type_changed', 'requests_total', 'counter', 'gauge')
        ordinary = result(after=after, allowlist=[expected])
        report = result(after=after, allowlist=[expected], include_raw_changes=True)
        raw = report.pop('raw_changes')
        self.assertEqual(report, ordinary)
        self.assertEqual(report['status'], 'pass')
        self.assertEqual(report['summary']['allowed'], 1)
        self.assertEqual([e['kind'] for e in raw], ['family_removed', 'family_added'])
        raw_only = event('family_removed', 'old_requests_total', contract('old_requests_total', 'verb'), None)
        with self.assertRaisesRegex(InputError, 'unused'):
            result(allowlist=[raw_only], include_raw_changes=True)

    def test_opt_in_works_without_declarations_and_for_empty_diffs(self):
        for mapping in (None, {}, {'metrics': {}, 'labels': {}}):
            with self.subTest(mapping=mapping):
                report = result(mapping=mapping, include_raw_changes=True)
                self.assertEqual(report['raw_changes'], [
                    event('family_removed', 'old_requests_total', contract('old_requests_total', 'verb'), None),
                    event('family_added', 'requests_total', None, contract('requests_total', 'method'))])
        for before, after in [('', ''), ('x 1 1\n', 'x 99 999\n')]:
            report = result(before, after, None, include_raw_changes=True)
            self.assertEqual(report['raw_changes'], [])
            self.assertEqual(report['status'], 'pass')
            self.assertIn('RAW CHANGES (wire, informational):\n  (none)', text_report(report))

    def test_composite_and_metadata_only_wire_names_are_preserved(self):
        before = '# TYPE h histogram\nh_bucket{old="a",le="+Inf"} 1\nh_sum{old="a"} 1\nh_count{old="a"} 1\n'
        after = before.replace('TYPE h ', 'TYPE j ').replace('\nh_', '\nj_').replace('old=', 'new=')
        report = result(before, after, {'metrics': {'h': 'j'}, 'labels': {'h': {'old': 'new'}}},
                        include_raw_changes=True)
        self.assertEqual(report['changes'], [])
        raw = report['raw_changes']
        self.assertEqual([(e['kind'], e['family']) for e in raw],
                         [('family_removed', 'h'), ('family_added', 'j')])
        self.assertEqual(raw[0]['before']['samples'], [
            {'name': 'h_bucket', 'label_keys': ['le', 'old']},
            {'name': 'h_count', 'label_keys': ['old']}, {'name': 'h_sum', 'label_keys': ['old']}])
        report = result('# TYPE s summary\n', '# TYPE t summary\n', {'metrics': {'s': 't'}},
                        include_raw_changes=True)
        self.assertEqual(report['raw_changes'], [
            event('family_removed', 's', {'type': 'summary', 'help': None, 'unit': None, 'samples': []}, None),
            event('family_added', 't', None, {'type': 'summary', 'help': None, 'unit': None, 'samples': []})])

    def test_deterministic_and_does_not_mutate_snapshots_or_map(self):
        before = 'a{x="one",z="two"} 1\na{x="three",z="four"} 2\nb 1\n'
        after = 'c{y="one",z="two"} 99\nc{y="three",z="four"} 100\nd 2\n'
        mapping = {'metrics': {'a': 'c', 'b': 'd'}, 'labels': {'a': {'x': 'y'}}}
        left, right = parse_snapshot(before), parse_snapshot(after)
        original = deepcopy((left, right, mapping))
        first = compare(left, right, rename_map=mapping, include_raw_changes=True)
        self.assertEqual((left, right, mapping), original)
        second = result('b 1\na{z="four",x="three"} 2\na{z="two",x="one"} 1\n',
                        'd 2\nc{z="four",y="three"} 100\nc{z="two",y="one"} 99\n',
                        {'labels': {'a': {'x': 'y'}}, 'metrics': {'b': 'd', 'a': 'c'}},
                        include_raw_changes=True)
        self.assertEqual(first, second)

    def test_cli_json_text_defaults_and_failure_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before, after, mapping = [root / n for n in ('before.prom', 'after.prom', 'map.json')]
            before.write_text(BEFORE)
            after.write_text(AFTER)
            mapping.write_text(json.dumps(MAP))
            original = {p: p.read_bytes() for p in (before, after, mapping)}

            def run(*flags):
                return subprocess.run([sys.executable, '-m', 'otelwrench', 'promdiff', str(before), str(after),
                                       '--rename-map', str(mapping), *flags],
                                      text=True, capture_output=True, timeout=60)

            ordinary = run('--format', 'json')
            self.assertEqual(ordinary.returncode, 0, ordinary.stderr)
            output = run('--include-raw-changes', '--format', 'json')
            self.assertEqual(output.returncode, 0, output.stderr)
            data = json.loads(output.stdout)
            self.assertEqual(data.pop('raw_changes'), result(include_raw_changes=True)['raw_changes'])
            self.assertEqual(data, json.loads(ordinary.stdout))
            self.assertEqual(output.stdout, run('--include-raw-changes', '--format', 'json').stdout)
            text = run('--include-raw-changes')
            self.assertEqual(text.returncode, 0, text.stderr)
            self.assertIn('RAW CHANGES (wire, informational):\nRAW family_removed old_requests_total:', text.stdout)
            self.assertIn('RAW family_added requests_total:', text.stdout)
            self.assertNotIn('private-value', text.stdout)
            self.assertNotIn('RAW CHANGES', run().stdout)
            self.assertEqual(original, {p: p.read_bytes() for p in original})
            after.write_text(AFTER.replace('counter', 'gauge'))
            output = run('--include-raw-changes', '--format', 'json')
            self.assertEqual(output.returncode, 1, output.stderr)
            self.assertEqual(json.loads(output.stdout)['changes'][0]['kind'], 'type_changed')

    def test_cli_errors_have_no_partial_raw_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before, after, mapping, allow = [root / n for n in ('before', 'after', 'map', 'allow')]
            cases = [
                (BEFORE, AFTER, {'metrics': {'absent': 'target'}}, []),
                ('a 1\nb 1\n', 'b 2\n', {'metrics': {'a': 'b'}}, []),
                (BEFORE, 'broken', MAP, []),
                (BEFORE, AFTER, MAP, [event('family_removed', 'old_requests_total',
                                          contract('old_requests_total', 'verb'), None)])]
            for left, right, declared, allowed in cases:
                before.write_text(left)
                after.write_text(right)
                mapping.write_text(json.dumps(declared))
                allow.write_text(json.dumps(allowed))
                for fmt in ('json', 'text'):
                    with self.subTest(left=left, fmt=fmt):
                        output = subprocess.run([
                            sys.executable, '-m', 'otelwrench', 'promdiff', str(before), str(after),
                            '--rename-map', str(mapping), '--allowlist', str(allow),
                            '--include-raw-changes', '--format', fmt],
                            text=True, capture_output=True, timeout=60)
                        self.assertEqual(output.returncode, 2)
                        self.assertEqual(output.stdout, '')
                        self.assertNotIn('Traceback', output.stderr)
                        if fmt == 'json':
                            self.assertEqual(json.loads(output.stderr)['status'], 'error')


if __name__ == '__main__':
    unittest.main()
