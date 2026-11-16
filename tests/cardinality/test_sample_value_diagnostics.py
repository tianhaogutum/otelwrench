# SPDX-License-Identifier: Apache-2.0
"""Value/token-count failures expose saved-file lines without new syntax."""

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

ID = 'late "β"\\capture'
TIME = '2026-11-11T08:00:00Z'
PREFIX = '# capture\nx{debug="d"} 1\n\n# gap\n'
BAD_VALUES = ('nope', 'nan', 'NAN', '+NaN', '-NaN', 'Infinity', '-Infinity',
              '0x1p2', '1_000', '1e', '1e+', '.', '+', '１', '1\v2', '1\u00a02',
              '{"native":1}', '#', 'nope 100', '1 2 more', '1 # {trace_id="雪"} 2',
              '1 100 # {trace_id="a"} 2', 'NaN\t100\textra', '1e999 0 extra')
MESSAGE = 'line 5: invalid value or unsupported trailing data (e.g. exemplar)'
DETAILS = dict(code='invalid_input_sample_value', line=5)


def raw(value='nope'):
    return PREFIX + 'ns:y{note="雪"}\t' + value + ' \t\n'


class SampleValueDiagnosticTests(unittest.TestCase):
    def assert_error(self, call, text, details):
        with self.assertRaises(InputError) as raised:
            call()
        exc = raised.exception
        self.assertIsInstance(exc, ValueError)
        self.assertEqual((str(exc), exc.args, exc.details), (text, (text,), details))
        return exc

    def manifest(self, root, text, context=None):
        (root / 'first.prom').write_text('x{debug="d"} 1\n', encoding='utf-8')
        (root / 'late.prom').write_text(text, encoding='utf-8')
        entries = [dict(id='first', source='source', captured_at=TIME, path='first.prom'),
                   dict(id=ID, source='source', captured_at=TIME, path='late.prom')]
        if context is not None:
            entries[1]['target_labels'] = context
        path = root / 'set.json'
        path.write_text(json.dumps(dict(schema_version=1, scrapes=entries)), encoding='utf-8')
        return path

    def test_invalid_value_and_excess_tokens_keep_exact_message_and_original_line(self):
        for value in BAD_VALUES:
            with self.subTest(value=value):
                self.assert_error(lambda: parse(raw(value)), MESSAGE, DETAILS)
        # First encountered bad sample wins, independent of metric sorting.
        self.assert_error(lambda: parse(raw() + 'a nope\n'), MESSAGE, DETAILS)

    def test_late_manifest_copies_evidence_preserving_wrapper_and_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in BAD_VALUES:
                for context in (None, {}, {'note': 'conflict'}, {'bad-name': 1}):
                    path = self.manifest(root, raw(value), context)
                    with self.subTest(value=value, context=context):
                        exc = self.assert_error(lambda: load_scrape_set(path),
                            f'scrape {ID!r} (late.prom): {MESSAGE}', {**DETAILS, 'scrape_id': ID})
                        self.assertEqual((str(exc.__cause__), exc.__cause__.args, exc.__cause__.details),
                                         (MESSAGE, (MESSAGE,), DETAILS))
                        self.assertIsNot(exc.details, exc.__cause__.details)
                        exc.details['line'] = 999
                        self.assertEqual(exc.__cause__.details, DETAILS)
            # Context in an earlier capture is still validated first.
            data = json.loads(path.read_text())
            data['scrapes'][0]['target_labels'] = {'debug': 'conflict'}
            path.write_text(json.dumps(data))
            self.assert_error(lambda: load_scrape_set(path),
                "scrape 'first', line 1: target label 'debug' conflicts with exposition",
                dict(code='target_label_conflict', scrape_id='first', line=1, label='debug',
                     exposition_value='d', target_value='conflict'))

    def test_cli_file_stdin_late_set_baseline_scoped_inline_v1_v2_comparison_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modes = [('baseline', []), ('inline', ['--drop-labels', 'debug', '--metric', 'x'])]
            for version in (1, 2):
                candidate = dict(name='Only x', drop_labels=['debug'])
                if version == 2:
                    candidate['metrics'] = ['x']
                file = root / f'v{version}.json'
                file.write_text(json.dumps(dict(schema_version=version, candidates=[candidate])))
                modes.append((f'v{version}', ['--candidate-file', str(file)] +
                              (['--metric', 'x'] if version == 1 else [])))
            for value in ('nope', '1 2 more', '1 # {trace_id="雪"} 2'):
                path = self.manifest(root, raw(value), {'bad-name': 1})
                for mode, args in [('file', [str(root / 'late.prom')]), ('stdin', ['-']),
                                   ('set', ['--scrape-set', str(path)])]:
                    text = f'scrape {ID!r} (late.prom): {MESSAGE}' if mode == 'set' else MESSAGE
                    details = {**DETAILS, 'scrape_id': ID} if mode == 'set' else DETAILS
                    for kind, opts in modes:
                        for rank in (False, True) if kind != 'baseline' else (False,):
                            for fmt in ('text', 'json'):
                                out, err = io.StringIO(), io.StringIO()
                                with self.subTest(value=value, mode=mode, kind=kind, rank=rank, fmt=fmt), \
                                        patch.object(cli, 'report', side_effect=AssertionError('analyzed single')), \
                                        patch.object(cli, 'report_scrapes', side_effect=AssertionError('analyzed set')), \
                                        patch('sys.stdin', buffer=io.BytesIO(raw(value).encode())), \
                                        redirect_stdout(out), redirect_stderr(err):
                                    code = cli.main([*args, *opts, *(['--rank-plans'] if rank else []), '--format', fmt])
                                self.assertEqual((code, out.getvalue()), (2, ''))
                                if fmt == 'json':
                                    self.assertEqual(json.loads(err.getvalue()), dict(error=text,
                                        error_type='invalid_or_unsupported_input', details=details))
                                else:
                                    self.assertEqual(err.getvalue(), f'otelwrench cardinality: {text}\n')

    def test_adjacent_missing_value_and_earlier_errors_stay_generic(self):
        cases = [
            ('x\n', 'line 1: sample value must be separated by whitespace'),
            ('x \t\n', 'line 1: sample value must be separated by whitespace'),
            ('x{a="b"} \t\n', 'line 1: sample value must be separated by whitespace'),
            ('x{a="b"}nope\n', 'line 1: sample value must be separated by whitespace'),
            ('x{a=oops} nope\n', 'line 1: expected quoted label value'),
            ('x{a="\\q"} nope\n', 'line 1: unsupported or incomplete escape'),
            ('# TYPE x gauge\n# TYPE x gauge\n' + raw(), "line 2: duplicate TYPE for 'x'"),
            (raw().rstrip('\n'), 'Prometheus text input must end with a newline'),
            (raw() + '\r\n', 'CR/CRLF and NUL are unsupported; use UTF-8 with LF line endings'),
            (raw() + '\x00\n', 'CR/CRLF and NUL are unsupported; use UTF-8 with LF line endings'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for text, message in cases:
                with self.subTest(text=text):
                    self.assert_error(lambda: parse(text), message, None)
                    path = self.manifest(root, text)
                    self.assert_error(lambda: load_scrape_set(path),
                                      f'scrape {ID!r} (late.prom): {message}', None)
            path = self.manifest(root, raw())
            data = json.loads(path.read_text())
            data['scrapes'][1]['id'] = 'first'
            path.write_text(json.dumps(data))
            self.assert_error(lambda: load_scrape_set(path), "duplicate scrape id 'first'", None)

    def test_value_error_precedence_and_existing_structured_label_errors(self):
        for prefix in ('x 1\nx 2\n', '# TYPE h histogram\nh_bucket 1\n',
                       '# TYPE h histogram\n# TYPE h_count gauge\nh_count 1\n',
                       'x 1\n# HELP x late\n'):
            line = prefix.count('\n') + 1
            with self.subTest(prefix=prefix):
                self.assert_error(lambda: parse(prefix + 'z nope\n'),
                    f'line {line}: invalid value or unsupported trailing data (e.g. exemplar)',
                    dict(code='invalid_input_sample_value', line=line))
        self.assert_error(lambda: parse('x{__name__=oops} nope\n'),
            "line 1: reserved/internal label '__name__' is unsupported",
            dict(code='reserved_input_label', line=1, label='__name__'))
        self.assert_error(lambda: parse('x{a="one",a=oops} nope\n'),
            "line 1: duplicate label 'a'", dict(code='duplicate_input_label', line=1, label='a'))


if __name__ == '__main__':
    unittest.main()
