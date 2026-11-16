# SPDX-License-Identifier: Apache-2.0
"""Optional timestamp evidence preserves integer syntax, range and precedence."""

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
INVALID_INTEGER = ('nope', '#', 'NaN', 'Inf', '+Inf', '-Inf', '.', '+', '-',
                   '--1', '+-1', '++1', '1.0', '.5', '1e3', '0x10', '1_000',
                   '１', '١', '−1', '1\v2', '1\u00a02', '1\u20032',
                   '9' * 5000 + 'x', '+' + '0' * 5000 + '１')
OUT_OF_RANGE = ('9223372036854775808', '+9223372036854775808',
                '-9223372036854775809', '0009223372036854775808',
                '-0009223372036854775809', '9' * 5000,
                '+' + '0' * 5000 + '9223372036854775808',
                '-' + '0' * 5000 + '9223372036854775809')
VALID = ('0', '+0', '-0', '000', '1', '-1', '9223372036854775807',
         '+9223372036854775807', '-9223372036854775808',
         '+0009223372036854775807', '-0009223372036854775808',
         '0' * 5000, '+' + '0' * 5000 + '9223372036854775807',
         '-' + '0' * 5000 + '9223372036854775808')


def raw(timestamp):
    return PREFIX + 'ns:y{note="雪"}\t1\t' + timestamp + ' \t\n'


def message(reason, line=5):
    return f'line {line}: ' + ('timestamp must be an integer in milliseconds'
                               if reason == 'invalid_integer' else 'timestamp outside signed int64 range')


def details(reason, line=5):
    return dict(code='invalid_input_sample_timestamp', line=line, reason=reason)


class SampleTimestampDiagnosticTests(unittest.TestCase):
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

    def test_invalid_integer_and_range_keep_exact_message_and_original_line(self):
        for reason, tokens in [('invalid_integer', INVALID_INTEGER), ('out_of_range', OUT_OF_RANGE)]:
            for i, token in enumerate(tokens):
                with self.subTest(reason=reason, token_index=i):
                    self.assert_error(lambda: parse(raw(token)), message(reason), details(reason))
                    self.assert_error(lambda: parse(raw(token) + 'a 1 nope\n'), message(reason), details(reason))

    def test_accepted_bounds_long_padding_and_special_values_preserve_spelling(self):
        for token in VALID:
            for value in ('1', '-0.0', '1e-999', 'NaN', 'Inf', '+Inf', '-Inf'):
                with self.subTest(length=len(token), prefix=token[:24], value=value):
                    sample = parse(f'# gap\n\nx\t{value}\t{token} \t\n').samples[0]
                    self.assertEqual((sample.value, sample.timestamp_ms, sample.line), (value, token, 3))
        self.assertIsNone(parse('x 1\n').samples[0].timestamp_ms)
        # Timestamps still do not distinguish series identity.
        self.assert_error(lambda: parse('x 1 +000\nx 2 -000\n'),
            'line 2: duplicate input series; first seen on line 1 (timestamps do not change series identity)',
            dict(code='duplicate_input_series', line=2, first_seen_line=1,
                 original_key=dict(metric='x', labels={})))

    def test_manifest_copies_evidence_preserving_wrapper_and_separate_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for reason, tokens in [('invalid_integer', INVALID_INTEGER), ('out_of_range', OUT_OF_RANGE)]:
                for i, token in enumerate(tokens):
                    for context in (None, {}, {'note': 'conflict'}, {'bad-name': 1}):
                        with self.subTest(reason=reason, token_index=i, context=context):
                            path = self.manifest(root, raw(token), context)
                            exc = self.assert_error(lambda: load_scrape_set(path),
                                f'scrape {ID!r} (late.prom): {message(reason)}', {**details(reason), 'scrape_id': ID})
                            self.assertEqual((str(exc.__cause__), exc.__cause__.args, exc.__cause__.details),
                                             (message(reason), (message(reason),), details(reason)))
                            self.assertIsNot(exc.details, exc.__cause__.details)
                            exc.details['reason'] = 'changed'
                            self.assertEqual(exc.__cause__.details, details(reason))
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
            for reason, token in [('invalid_integer', 'nope'), ('invalid_integer', '１'),
                                  ('out_of_range', '-9223372036854775809')]:
                path = self.manifest(root, raw(token), {'bad-name': 1})
                for mode, args in [('file', [str(root / 'late.prom')]), ('stdin', ['-']),
                                   ('set', ['--scrape-set', str(path)])]:
                    text = f'scrape {ID!r} (late.prom): {message(reason)}' if mode == 'set' else message(reason)
                    evidence = {**details(reason), 'scrape_id': ID} if mode == 'set' else details(reason)
                    for kind, opts in modes:
                        for rank in (False, True) if kind != 'baseline' else (False,):
                            for fmt in ('text', 'json'):
                                out, err = io.StringIO(), io.StringIO()
                                with self.subTest(reason=reason, mode=mode, kind=kind, rank=rank, fmt=fmt), \
                                        patch.object(cli, 'report', side_effect=AssertionError('analyzed single')), \
                                        patch.object(cli, 'report_scrapes', side_effect=AssertionError('analyzed set')), \
                                        patch('sys.stdin', buffer=io.BytesIO(raw(token).encode())), \
                                        redirect_stdout(out), redirect_stderr(err):
                                    code = cli.main([*args, *opts, *(['--rank-plans'] if rank else []), '--format', fmt])
                                self.assertEqual((code, out.getvalue()), (2, ''))
                                if fmt == 'json':
                                    self.assertEqual(json.loads(err.getvalue()), dict(error=text,
                                        error_type='invalid_or_unsupported_input', details=evidence))
                                else:
                                    self.assertEqual(err.getvalue(), f'otelwrench cardinality: {text}\n')

    def test_prior_value_label_overflow_and_framing_precedence(self):
        for text in ('x nope 1e3\n', 'x 1 nope extra\n', 'x 1e999 nope extra\n', 'x 1 # exemplar\n'):
            self.assert_error(lambda: parse(text),
                'line 1: invalid value or unsupported trailing data (e.g. exemplar)',
                dict(code='invalid_input_sample_value', line=1))
        for text in ('x 1e999 nope\n', 'x -1e999 9223372036854775808\n'):
            self.assert_error(lambda: parse(text), 'line 1: decimal value overflows float64; use explicit Inf',
                              dict(code='input_sample_value_overflow', line=1))
        cases = [
            ('x\n', 'line 1: sample value must be separated by whitespace'),
            ('x{a="b"}1 nope\n', 'line 1: sample value must be separated by whitespace'),
            ('x{a=oops} 1 nope\n', 'line 1: expected quoted label value'),
            ('x{a="\\q"} 1 nope\n', 'line 1: unsupported or incomplete escape'),
            ('# TYPE x gauge\n# TYPE x gauge\n' + raw('nope'), "line 2: duplicate TYPE for 'x'"),
            (raw('nope').rstrip('\n'), 'Prometheus text input must end with a newline'),
            (raw('nope') + '\r\n', 'CR/CRLF and NUL are unsupported; use UTF-8 with LF line endings'),
            (raw('nope') + '\x00\n', 'CR/CRLF and NUL are unsupported; use UTF-8 with LF line endings'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for text, msg in cases:
                self.assert_error(lambda: parse(text), msg, None)
                path = self.manifest(Path(directory), text)
                self.assert_error(lambda: load_scrape_set(path), f'scrape {ID!r} (late.prom): {msg}', None)
        for text, msg, evidence in [
            ('x{__name__=oops} 1 nope\n', "line 1: reserved/internal label '__name__' is unsupported",
             dict(code='reserved_input_label', line=1, label='__name__')),
            ('x{a="one",a=oops} 1 nope\n', "line 1: duplicate label 'a'",
             dict(code='duplicate_input_label', line=1, label='a')),
        ]:
            self.assert_error(lambda: parse(text), msg, evidence)
        # Parsing still completes before second-pass series/family/metadata validation.
        for prefix in ('x 1\nx 2\n', '# TYPE h histogram\nh_bucket 1\n', 'x 1\n# HELP x late\n'):
            line = prefix.count('\n') + 1
            self.assert_error(lambda: parse(prefix + 'z 1 nope\n'), message('invalid_integer', line),
                              details('invalid_integer', line))


if __name__ == '__main__':
    unittest.main()
