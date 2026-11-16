# SPDX-License-Identifier: Apache-2.0
"""Context-induced duplicate input exposes its first canonical effective identity."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from otelwrench.cardinality import analysis
from otelwrench.cardinality.parser import InputError, parse
from otelwrench.cardinality.scrapes import IdentifiedScrape, contextualize, load_scrape_set

CLI = [sys.executable, "-m", "otelwrench", "cardinality"]
TIME = '2026-11-11T08:00:00Z'
ID = 'late "β"\\capture'
VALUE = '目標 "quoted"\n\\path'


def quoted(value):
    return json.dumps(value, ensure_ascii=False)


def capture(id, raw, context):
    return IdentifiedScrape(id, 'provenance', TIME, parse(raw), context)


def message(id, line, first):
    return f'scrape {id!r}, line {line}: duplicate input series after target labels; first seen on line {first}'


def evidence(id, line, first, metric='ns:y', labels=None):
    return dict(code='target_label_duplicate', scrape_id=id, line=line, first_seen_line=first,
                effective_key=dict(metric=metric, labels={'instance': 'a', 'note': VALUE} if labels is None else labels))


def duplicate_capture(values=('7 100', '7 100', '7 100', '7 100')):
    # Four distinct raw identities become one. Empty zone is omitted only from
    # the canonical key, and comments/blank lines retain original line numbers.
    raw = ('# capture\nx{debug="d"} 1\n\n'
           'ns:y{instance="a",note=' + quoted(VALUE) + ',zone=""} ' + values[0] + '\n# gap\n'
           'ns:y{instance="a"} ' + values[1] + '\n'
           'ns:y{note=' + quoted(VALUE) + '} ' + values[2] + '\n'
           'ns:y ' + values[3] + '\n')
    return raw, capture(ID, raw, {'zone': '', 'note': VALUE, 'instance': 'a'})


class DuplicateDiagnosticTests(unittest.TestCase):
    def assert_error(self, call, expected):
        with self.assertRaises(InputError) as raised:
            call()
        exc = raised.exception
        text = message(expected['scrape_id'], expected['line'], expected['first_seen_line'])
        self.assertIsInstance(exc, ValueError)
        self.assertEqual((str(exc), exc.args), (text, (text,)))
        self.assertEqual(exc.details, expected)

    def test_contextualize_attaches_exact_canonical_duplicate_evidence(self):
        _, item = duplicate_capture()
        self.assert_error(lambda: contextualize(item), evidence(ID, 6, 4))

    def test_late_unselected_duplicate_is_atomic_regardless_of_values_or_timestamps(self):
        good = capture('first', 'x{debug="d"} 1\n', {})
        for values in [('7 100',) * 4, ('7 100', '7 200', '7', '7 300'),
                       ('7 100', '9 100', 'NaN 200', '-0')]:
            _, item = duplicate_capture(values)
            original = item.scrape
            before = tuple(s.labels for s in original.samples)
            for ranked in (False, True):
                for scope in ({'metrics': ['x']}, {'plan_names': ['Only x'], 'plan_metrics': [['x']]}):
                    with self.subTest(values=values, ranked=ranked, scope=scope), \
                            patch.object(analysis, 'summarize', side_effect=AssertionError('summarized')), \
                            patch.object(analysis, '_analyze_validated', side_effect=AssertionError('projected')), \
                            patch.object(analysis, '_union_plan', side_effect=AssertionError('union projected')), \
                            patch.object(analysis, '_rank_safe_plans', side_effect=AssertionError('ranked')):
                        self.assert_error(lambda: analysis.report_scrapes(
                            [good, item], [['debug']], rank_plans=ranked,
                            required_target_labels=['missing'], protected_target_labels=['debug'], **scope),
                            evidence(ID, 6, 4))
            self.assertIs(item.scrape, original)
            self.assertEqual(tuple(s.labels for s in original.samples), before)
            self.assertEqual(dict(original.samples[1].labels), {'instance': 'a', 'note': VALUE, 'zone': ''})
            self.assertEqual(dict(original.samples[2].labels), {'instance': 'a'})

    def test_first_duplicate_preserves_capture_and_sample_order_and_empty_canonical_key(self):
        # Two groups collide; z wins despite metric sort order. The second
        # occurrence must fail before a third can overwrite its first line.
        first = capture('Z first', '# header\nz{job="api",zone=""} 1\na{job="api"} 1\nz 2\na 2\n',
                        {'job': 'api', 'zone': ''})
        later = capture('A later', 'x{job="api"} 1\nx 1\n', {'job': 'api'})
        for ranked in (False, True):
            for items, expected in [([first, later], evidence('Z first', 4, 2, 'z', {'job': 'api'})),
                                    ([later, first], evidence('A later', 2, 1, 'x', {'job': 'api'}))]:
                with self.subTest(expected=expected, ranked=ranked):
                    self.assert_error(lambda: analysis.report_scrapes(items, [['job']], rank_plans=ranked), expected)
        # Absent vs explicit empty raw spelling yields the same effective key.
        for raw in ('x{job="api",zone=""} 1\nx 2\n', 'x 1\nx{job="api",zone=""} 2\n'):
            item = capture('empty', raw, {'job': 'api'})
            self.assert_error(lambda: contextualize(item), evidence('empty', 2, 1, 'x', {'job': 'api'}))

    def manifest(self, root, raw, context):
        (root / 'good.prom').write_text('x{debug="d"} 1\n', encoding='utf-8')
        (root / 'late.prom').write_text(raw, encoding='utf-8')
        entries = [dict(id='first', source='source', captured_at=TIME, path='good.prom'),
                   dict(id=ID, source='source', captured_at=TIME, path='late.prom', target_labels=context)]
        path = root / 'set.json'
        path.write_text(json.dumps(dict(schema_version=1, scrapes=entries)), encoding='utf-8')
        return path

    def test_cli_baseline_inline_v1_v2_comparison_ranking_json_and_exact_text(self):
        raw, item = duplicate_capture(('7 100', '9 200', '7', 'NaN'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.manifest(root, raw, item.target_labels)
            self.assert_error(lambda: load_scrape_set(manifest), evidence(ID, 6, 4))
            for kind in ('baseline', 'inline', 'v1', 'v2'):
                if kind == 'baseline':
                    options = []
                elif kind == 'inline':
                    options = ['--drop-labels', 'debug', '--metric', 'x']
                else:
                    candidate = dict(name='Only x', drop_labels=['debug'])
                    if kind == 'v2':
                        candidate['metrics'] = ['x']
                    path = root / 'candidates.json'
                    path.write_text(json.dumps(dict(schema_version=int(kind[1]), candidates=[candidate])))
                    options = ['--candidate-file', str(path)] + (['--metric', 'x'] if kind == 'v1' else [])
                for ranked in (False, True) if kind != 'baseline' else (False,):
                    args = [*CLI, '--scrape-set', str(manifest), *options]
                    if ranked:
                        args.append('--rank-plans')
                    for format in ('json', 'text'):
                        with self.subTest(kind=kind, ranked=ranked, format=format):
                            proc = subprocess.run([*args, '--format', format], cwd='/', capture_output=True, timeout=60)
                            self.assertEqual((proc.returncode, proc.stdout), (2, b''), proc.stderr)
                            if format == 'json':
                                self.assertEqual(json.loads(proc.stderr), dict(error=message(ID, 6, 4),
                                    error_type='invalid_or_unsupported_input', details=evidence(ID, 6, 4)))
                            else:
                                self.assertEqual(proc.stderr, f'otelwrench cardinality: {message(ID, 6, 4)}\n'.encode())

    def test_malformed_context_and_conflict_precedence_and_original_parser_duplicates(self):
        raw, item = duplicate_capture()
        for context, text in [({**item.target_labels, 'bad-name': 'x'},
                               f'scrape {ID!r}: invalid or protected target label \'bad-name\''),
                              ({**item.target_labels, 'later': 1},
                               f'scrape {ID!r}: target label \'later\' requires a UTF-8 string without NUL/CR')]:
            with self.subTest(context=context), self.assertRaises(InputError) as raised:
                contextualize(capture(ID, raw, context))
            self.assertEqual((str(raised.exception), raised.exception.details), (text, None))
        # A conflict on the duplicate sample wins before that sample's key check;
        # an earlier duplicate wins before a later conflict (covered in conflict tests).
        conflict = capture('conflict', 'x{job="api"} 1\nx{note="raw"} 2\n', {'job': 'api', 'note': 'target'})
        with self.assertRaises(InputError) as raised:
            contextualize(conflict)
        self.assertEqual((str(raised.exception), raised.exception.details), (
            "scrape 'conflict', line 2: target label 'note' conflicts with exposition",
            dict(code='target_label_conflict', scrape_id='conflict', line=2, label='note',
                 exposition_value='raw', target_value='target')))
        for raw in ('x 1 100\nx 1 200\n', 'x{zone=""} 1\nx 2\n'):
            with self.subTest(raw=raw), self.assertRaises(InputError) as raised:
                parse(raw)
            self.assertEqual(raised.exception.details, dict(code='duplicate_input_series', line=2,
                first_seen_line=1, original_key=dict(metric='x', labels={})))
            self.assertEqual(str(raised.exception), 'line 2: duplicate input series; first seen on line 1 (timestamps do not change series identity)')


if __name__ == '__main__':
    unittest.main()
