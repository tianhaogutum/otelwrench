# SPDX-License-Identifier: Apache-2.0
"""Decimal float64 overflow evidence leaves numeric syntax and precedence intact."""

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
OVERFLOWS = ('1e999', '+1E+999', '-1e999', '1.8e308', '-1.8e308',
             '9' * 5000, '+' + '9' * 5000 + '.', '-' + '9' * 5000 + '.0',
             '.1e999', '-.1E+999', '0001e999')
ACCEPTED = ('NaN', 'Inf', '+Inf', '-Inf', '1', '-2.5', '+.5', '1.',
            '1.7976931348623157e308', '-1.7976931348623157e308',
            '0', '+0', '-0', '-0.0', '+000.000', '1e-999', '-1e-999',
            '0e999', '0' * 5000, '0.' + '0' * 5000 + '1')


def raw(value):
    return PREFIX + 'ns:y{note="雪"}\t' + value + ' \t\n'


def message(line=5):
    return f'line {line}: decimal value overflows float64; use explicit Inf'


def details(line=5):
    return dict(code='input_sample_value_overflow', line=line)


class SampleOverflowDiagnosticTests(unittest.TestCase):
    def assert_error(self, call, text, evidence):
        with self.assertRaises(InputError) as raised:
            call()
        exc = raised.exception
        self.assertIsInstance(exc, ValueError)
        self.assertEqual((str(exc), exc.args, exc.details), (text, (text,), evidence))
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

    def test_positive_negative_exponent_long_decimal_original_lines_and_first_failure(self):
        for i, value in enumerate(OVERFLOWS):
            for timestamp in ('', ' 0', ' nope', ' 9223372036854775808'):
                with self.subTest(value_index=i, timestamp=timestamp):
                    self.assert_error(lambda: parse(raw(value + timestamp)), message(), details())
                    self.assert_error(lambda: parse(raw(value + timestamp) + 'a nope\n'), message(), details())
        self.assert_error(lambda: parse('x 1e999\n'), message(1), details(1))

    def test_accepted_explicit_specials_finite_zero_underflow_preserve_exact_spelling(self):
        for i, value in enumerate(ACCEPTED):
            with self.subTest(value_index=i):
                sample = parse(raw(value + ' +00042')).samples[-1]
                self.assertEqual((sample.value, sample.timestamp_ms, sample.line), (value, '+00042', 5))
                self.assertIsNone(parse(raw(value)).samples[-1].timestamp_ms)
                for token, reason, text in [('nope', 'invalid_integer', 'timestamp must be an integer in milliseconds'),
                                           ('9223372036854775808', 'out_of_range', 'timestamp outside signed int64 range')]:
                    self.assert_error(lambda: parse(raw(value + ' ' + token)), f'line 5: {text}',
                                      dict(code='invalid_input_sample_timestamp', line=5, reason=reason))

    def test_late_manifest_copies_evidence_preserving_wrapper_and_separate_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for i, value in enumerate(OVERFLOWS):
                for context in (None, {}, {'note': 'conflict'}, {'bad-name': 1}):
                    with self.subTest(value_index=i, context=context):
                        path = self.manifest(root, raw(value), context)
                        exc = self.assert_error(lambda: load_scrape_set(path),
                            f'scrape {ID!r} (late.prom): {message()}', {**details(), 'scrape_id': ID})
                        self.assertIsInstance(exc.__cause__, InputError)
                        self.assertEqual((str(exc.__cause__), exc.__cause__.args, exc.__cause__.details),
                                         (message(), (message(),), details()))
                        self.assertIsNot(exc.details, exc.__cause__.details)
                        exc.details['line'] = 999
                        self.assertEqual(exc.__cause__.details, details())
            data = json.loads(path.read_text())
            data['scrapes'][0]['target_labels'] = {'debug': 'conflict'}
            path.write_text(json.dumps(data))
            self.assert_error(lambda: load_scrape_set(path),
                "scrape 'first', line 1: target label 'debug' conflicts with exposition",
                dict(code='target_label_conflict', scrape_id='first', line=1, label='debug',
                     exposition_value='d', target_value='conflict'))
            data['scrapes'][1]['id'] = 'first'
            path.write_text(json.dumps(data))
            self.assert_error(lambda: load_scrape_set(path), "duplicate scrape id 'first'", None)

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
            for value in ('1e999', '-1e999 nope', '9' * 5000):
                path = self.manifest(root, raw(value), {'bad-name': 1})
                for mode, args in [('file', [str(root / 'late.prom')]), ('stdin', ['-']),
                                   ('set', ['--scrape-set', str(path)])]:
                    text = f'scrape {ID!r} (late.prom): {message()}' if mode == 'set' else message()
                    evidence = {**details(), 'scrape_id': ID} if mode == 'set' else details()
                    for kind, opts in modes:
                        for rank in (False, True) if kind != 'baseline' else (False,):
                            for fmt in ('text', 'json'):
                                out, err = io.StringIO(), io.StringIO()
                                with self.subTest(value_prefix=value[:20], mode=mode, kind=kind, rank=rank, fmt=fmt), \
                                        patch.object(cli, 'report', side_effect=AssertionError('analyzed single')), \
                                        patch.object(cli, 'report_scrapes', side_effect=AssertionError('analyzed set')), \
                                        patch('sys.stdin', buffer=io.BytesIO(raw(value).encode())), \
                                        redirect_stdout(out), redirect_stderr(err):
                                    code = cli.main([*args, *opts, *(['--rank-plans'] if rank else []), '--format', fmt])
                                self.assertEqual((code, out.getvalue()), (2, ''))
                                if fmt == 'json':
                                    self.assertEqual(json.loads(err.getvalue()), dict(error=text,
                                        error_type='invalid_or_unsupported_input', details=evidence))
                                else:
                                    self.assertEqual(err.getvalue(), f'otelwrench cardinality: {text}\n')

    def test_value_label_separator_metadata_framing_and_second_pass_precedence(self):
        for value in ('nope', 'Infinity', 'nan', '１e999', '1e999 nope extra', '1e999 0 extra', '1e999 # exemplar'):
            self.assert_error(lambda: parse(raw(value)),
                'line 5: invalid value or unsupported trailing data (e.g. exemplar)',
                dict(code='invalid_input_sample_value', line=5))
        cases = [
            ('x\n', 'line 1: sample value must be separated by whitespace'),
            ('x{a="b"}1e999\n', 'line 1: sample value must be separated by whitespace'),
            ('x{a=oops} 1e999\n', 'line 1: expected quoted label value'),
            ('x{a="\\q"} 1e999\n', 'line 1: unsupported or incomplete escape'),
            ('# TYPE x gauge\n# TYPE x gauge\n' + raw('1e999'), "line 2: duplicate TYPE for 'x'"),
            (raw('1e999').rstrip('\n'), 'Prometheus text input must end with a newline'),
            (raw('1e999') + '\r\n', 'CR/CRLF and NUL are unsupported; use UTF-8 with LF line endings'),
            (raw('1e999') + '\x00\n', 'CR/CRLF and NUL are unsupported; use UTF-8 with LF line endings'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for text, msg in cases:
                self.assert_error(lambda: parse(text), msg, None)
                path = self.manifest(Path(directory), text)
                self.assert_error(lambda: load_scrape_set(path), f'scrape {ID!r} (late.prom): {msg}', None)
        for text, msg, evidence in [
            ('x{__name__=oops} 1e999\n', "line 1: reserved/internal label '__name__' is unsupported",
             dict(code='reserved_input_label', line=1, label='__name__')),
            ('x{a="one",a=oops} 1e999\n', "line 1: duplicate label 'a'",
             dict(code='duplicate_input_label', line=1, label='a')),
        ]:
            self.assert_error(lambda: parse(text), msg, evidence)
        # Parse-time overflow still wins over pending second-pass checks.
        for prefix in ('x 1\nx 2\n', '# TYPE h histogram\nh_bucket 1\n',
                       '# TYPE h histogram\n# TYPE h_count gauge\nh_count 1\n', 'x 1\n# HELP x late\n'):
            line = prefix.count('\n') + 1
            self.assert_error(lambda: parse(prefix + 'z 1e999\n'), message(line), details(line))


if __name__ == '__main__':
    unittest.main()
