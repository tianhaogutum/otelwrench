# SPDX-License-Identifier: Apache-2.0
"""Original exposition duplicates fail before target merge or analysis."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from otelwrench.cardinality import cli
from otelwrench.cardinality.parser import InputError, parse
from otelwrench.cardinality.scrapes import load_scrape_set

TIME = '2026-11-11T08:00:00Z'
ID = 'late "β"\\capture'
VALUE = '目標 "quoted"\n\\path'


def message(line=6, first=4):
    return (f'line {line}: duplicate input series; first seen on line {first} '
            '(timestamps do not change series identity)')


def evidence(line=6, first=4, metric='ns:y', labels=None):
    return dict(code='duplicate_input_series', line=line, first_seen_line=first,
                original_key=dict(metric=metric, labels={'note': VALUE} if labels is None else labels))


def duplicate(values=('7 100', '7 100', '7 100', '7 100')):
    quote = json.dumps(VALUE, ensure_ascii=False)
    # Label order, trailing comma, and empty labels do not change identity.
    return ('# capture\nx{debug="d"} 1\n\nns:y{zone="",note=' + quote + '} ' + values[0]
            + '\n# gap\nns:y{note = ' + quote + ',} ' + values[1]
            + '\nns:y{note=' + quote + '} ' + values[2]
            + '\nns:y{note=' + quote + ',zone=""} ' + values[3] + '\n')


class InputDuplicateDiagnosticTests(unittest.TestCase):
    def assert_error(self, call, text, details):
        with self.assertRaises(InputError) as raised:
            call()
        exc = raised.exception
        self.assertIsInstance(exc, ValueError)
        self.assertEqual((str(exc), exc.args, exc.details), (text, (text,), details))
        return exc

    def manifest(self, root, raw, context=None):
        (root / 'good.prom').write_text('x{debug="d"} 1\n', encoding='utf-8')
        (root / 'late.prom').write_text(raw, encoding='utf-8')
        entries = [dict(id='first', source='source', captured_at=TIME, path='good.prom'),
                   dict(id=ID, source='source', captured_at=TIME, path='late.prom')]
        if context is not None:
            entries[1]['target_labels'] = context
        path = root / 'set.json'
        path.write_text(json.dumps(dict(schema_version=1, scrapes=entries)), encoding='utf-8')
        return path

    def test_decoded_canonical_original_identity_ignores_values_timestamps_and_later_repeats(self):
        for values in [('7 100',) * 4, ('7 100', '7 200', '7', '7 300'),
                       ('7 100', '9 100', 'NaN 200', '-0')]:
            with self.subTest(values=values):
                self.assert_error(lambda: parse(duplicate(values)), message(), evidence())
        for raw in ('x{zone=""} 1\nx 2\nx{} 3\n', 'x 1\nx{zone=""} 2\nx 3\n',
                    'x{} 1\nx{zone="",empty=""} 2\nx 3\n'):
            with self.subTest(raw=raw):
                self.assert_error(lambda: parse(raw), message(2, 1), evidence(2, 1, 'x', {}))
        # z's first repeat wins before a's, regardless of metric sorting.
        raw = '# header\nz{b="2",a="1"} 1\na 1\nz{a="1",b="2"} 2\nz 3\na 2\n'
        self.assert_error(lambda: parse(raw), message(4, 2), evidence(4, 2, 'z', {'a': '1', 'b': '2'}))

    def test_manifest_adds_supplied_id_before_context_validation_and_preserves_both_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for context in (None, {}, {'note': 'conflict'}, {'bad-name': 1}):
                path = self.manifest(root, duplicate(), context)
                with self.subTest(context=context):
                    exc = self.assert_error(lambda: load_scrape_set(path),
                        f'scrape {ID!r} (late.prom): {message()}', {**evidence(), 'scrape_id': ID})
                    self.assertEqual(exc.__cause__.details, evidence())
            # Capture traversal wins, even though the late duplicate is earlier in its own file.
            (root / 'good.prom').write_text('x 1\nx 2\n')
            self.assert_error(lambda: load_scrape_set(path), f"scrape 'first' (good.prom): {message(2, 1)}",
                              {**evidence(2, 1, 'x', {}), 'scrape_id': 'first'})

    def test_manifest_wrapper_copies_parser_details_without_mutating_cause(self):
        details = evidence()
        original = InputError(message(), details=details)
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(Path(directory), duplicate())
            with patch('otelwrench.cardinality.scrapes.parse', side_effect=original):
                exc = self.assert_error(lambda: load_scrape_set(path),
                    f"scrape 'first' (good.prom): {message()}", {**details, 'scrape_id': 'first'})
        self.assertIs(exc.__cause__, original)
        self.assertEqual(original.details, evidence())
        self.assertIsNot(exc.details, original.details)

    def test_cli_single_stdin_and_set_baseline_scoped_comparison_ranking_fail_before_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.manifest(root, duplicate(), {'bad-name': 1})
            options = [('baseline', []) , ('inline', ['--drop-labels', 'debug', '--metric', 'x'])]
            for version in (1, 2):
                candidate = dict(name='Only x', drop_labels=['debug'])
                if version == 2:
                    candidate['metrics'] = ['x']
                file = root / f'v{version}.json'
                file.write_text(json.dumps(dict(schema_version=version, candidates=[candidate])))
                options.append((f'v{version}', ['--candidate-file', str(file)] +
                                (['--metric', 'x'] if version == 1 else [])))
            inputs = [('single', [str(root / 'late.prom')]), ('stdin', ['-']),
                      ('set', ['--scrape-set', str(path)])]
            for mode, args in inputs:
                text = f'scrape {ID!r} (late.prom): {message()}' if mode == 'set' else message()
                details = {**evidence(), 'scrape_id': ID} if mode == 'set' else evidence()
                for kind, opts in options:
                    for rank in (False, True) if kind != 'baseline' else (False,):
                        for fmt in ('text', 'json'):
                            out, err = io.StringIO(), io.StringIO()
                            with self.subTest(mode=mode, kind=kind, rank=rank, fmt=fmt), \
                                    patch.object(cli, 'report', side_effect=AssertionError('analyzed single')), \
                                    patch.object(cli, 'report_scrapes', side_effect=AssertionError('analyzed set')), \
                                    patch('sys.stdin', buffer=io.BytesIO(duplicate().encode())), \
                                    redirect_stdout(out), redirect_stderr(err):
                                code = cli.main([*args, *opts, *(['--rank-plans'] if rank else []), '--format', fmt])
                            self.assertEqual((code, out.getvalue()), (2, ''))
                            if fmt == 'json':
                                self.assertEqual(json.loads(err.getvalue()), dict(error=text,
                                    error_type='invalid_or_unsupported_input', details=details))
                            else:
                                self.assertEqual(err.getvalue(), f'otelwrench cardinality: {text}\n')

    def test_syntax_family_metadata_and_manifest_validation_precedence_stays_exact(self):
        cases = [
            ('# TYPE h histogram\nh_bucket 1\nh_bucket 2\n', 'line 2: only histogram buckets must have an le label'),
            ('# TYPE h histogram\n# TYPE h_count gauge\nh_count 1\nh_count 2\n', "ambiguous family ownership for metric 'h_count'"),
            ('x 1\n# HELP x late\n', "line 2: HELP must precede samples for 'x'"),
            ('# TYPE x gauge\n# TYPE x gauge\nx 1\n', "line 2: duplicate TYPE for 'x'"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for raw, text in cases:
                with self.subTest(text=text):
                    self.assert_error(lambda: parse(raw), text, None)
                    path = self.manifest(root, raw)
                    self.assert_error(lambda: load_scrape_set(path), f'scrape {ID!r} (late.prom): {text}', None)
            # Metadata ordering remains after duplicate detection in the second pass.
            raw = 'x 1\n# HELP x late\nx 2\n'
            self.assert_error(lambda: parse(raw), message(3, 1), evidence(3, 1, 'x', {}))
            path = self.manifest(root, raw)
            data = json.loads(path.read_text())
            data['scrapes'][1]['id'] = 'first'
            path.write_text(json.dumps(data))
            self.assert_error(lambda: load_scrape_set(path), "duplicate scrape id 'first'", None)

    def test_unrelated_wrapped_io_unicode_and_parser_errors_remain_generic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for error in (InputError('generic'), OSError('unreadable'), UnicodeError('bad UTF-8')):
                path = self.manifest(root, duplicate())
                with self.subTest(error=error), patch('otelwrench.cardinality.scrapes.parse', side_effect=error):
                    self.assert_error(lambda: load_scrape_set(path), f"scrape 'first' (good.prom): {error}", None)


if __name__ == '__main__':
    unittest.main()
