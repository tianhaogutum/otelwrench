# SPDX-License-Identifier: Apache-2.0
"""Reserved exposition labels keep rejection order and expose original evidence."""

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
RAW = '# capture\nx{debug="d"} 1\n\n# gap\nns:y{note="雪", __name__=unquoted} nope\n'
MESSAGE = "line 5: reserved/internal label '__name__' is unsupported"
DETAILS = dict(code='reserved_input_label', line=5, label='__name__')


class ReservedLabelDiagnosticTests(unittest.TestCase):
    def assert_error(self, call, text, details):
        with self.assertRaises(InputError) as raised:
            call()
        exc = raised.exception
        self.assertIsInstance(exc, ValueError)
        self.assertEqual((str(exc), exc.args, exc.details), (text, (text,), details))
        return exc

    def manifest(self, root, raw=RAW, context=None):
        (root / 'first.prom').write_text('x{debug="d"} 1\n', encoding='utf-8')
        (root / 'late.prom').write_text(raw, encoding='utf-8')
        entries = [dict(id='first', source='source', captured_at=TIME, path='first.prom'),
                   dict(id=ID, source='source', captured_at=TIME, path='late.prom')]
        if context is not None:
            entries[1]['target_labels'] = context
        path = root / 'set.json'
        path.write_text(json.dumps(dict(schema_version=1, scrapes=entries)), encoding='utf-8')
        return path

    def test_reserved_name_precedes_assignment_value_and_sample_value_parsing(self):
        for name in ('__name__', '__foo', '__', '___9', '__meta_kubernetes_pod_name'):
            for suffix in ('="same"', '=""', '', ' nope', '=', '=unquoted',
                           '="unterminated', '="\\q"', '="x",' + name + '="y"'):
                with self.subTest(name=name, suffix=suffix):
                    self.assert_error(lambda: parse(f'# header\n\nx{{a="雪", {name}{suffix}}} nope\n'),
                        f'line 3: reserved/internal label {name!r} is unsupported',
                        dict(code='reserved_input_label', line=3, label=name))
        # First encountered name wins, without sorting or assignment parsing.
        self.assert_error(lambda: parse('x{__z,__a="x"} 1\n'),
            "line 1: reserved/internal label '__z' is unsupported",
            dict(code='reserved_input_label', line=1, label='__z'))
        # Single underscores and an internal-looking value remain valid exposition.
        self.assertEqual(parse('x{_="__name__",_name="ok",a__="yes"} 1\n').samples[0].labels,
                         (('_', '__name__'), ('_name', 'ok'), ('a__', 'yes')))

    def test_late_manifest_copies_details_adds_supplied_id_and_preserves_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for context in (None, {}, {'__name__': 'still forbidden'}, {'bad-name': 1}):
                path = self.manifest(root, context=context)
                with self.subTest(context=context):
                    exc = self.assert_error(lambda: load_scrape_set(path),
                        f'scrape {ID!r} (late.prom): {MESSAGE}', {**DETAILS, 'scrape_id': ID})
                    self.assertIs(type(exc.__cause__), InputError)
                    self.assertEqual((str(exc.__cause__), exc.__cause__.args, exc.__cause__.details),
                                     (MESSAGE, (MESSAGE,), DETAILS))
                    self.assertIsNot(exc.details, exc.__cause__.details)
                    exc.details['label'] = 'mutated copy'
                    self.assertEqual(exc.__cause__.details, DETAILS)
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
            path = self.manifest(root, context={'bad-name': 1})
            modes = [('baseline', []), ('inline', ['--drop-labels', 'debug', '--metric', 'x'])]
            for version in (1, 2):
                candidate = dict(name='Only x', drop_labels=['debug'])
                if version == 2:
                    candidate['metrics'] = ['x']
                file = root / f'v{version}.json'
                file.write_text(json.dumps(dict(schema_version=version, candidates=[candidate])))
                modes.append((f'v{version}', ['--candidate-file', str(file)] +
                              (['--metric', 'x'] if version == 1 else [])))
            for mode, args in [('file', [str(root / 'late.prom')]), ('stdin', ['-']),
                               ('set', ['--scrape-set', str(path)])]:
                text = f'scrape {ID!r} (late.prom): {MESSAGE}' if mode == 'set' else MESSAGE
                details = {**DETAILS, 'scrape_id': ID} if mode == 'set' else DETAILS
                for kind, opts in modes:
                    for rank in (False, True) if kind != 'baseline' else (False,):
                        for fmt in ('text', 'json'):
                            out, err = io.StringIO(), io.StringIO()
                            with self.subTest(mode=mode, kind=kind, rank=rank, fmt=fmt), \
                                    patch.object(cli, 'report', side_effect=AssertionError('analyzed single')), \
                                    patch.object(cli, 'report_scrapes', side_effect=AssertionError('analyzed set')), \
                                    patch('sys.stdin', buffer=io.BytesIO(RAW.encode())), \
                                    redirect_stdout(out), redirect_stderr(err):
                                code = cli.main([*args, *opts, *(['--rank-plans'] if rank else []), '--format', fmt])
                            self.assertEqual((code, out.getvalue()), (2, ''))
                            if fmt == 'json':
                                self.assertEqual(json.loads(err.getvalue()), dict(error=text,
                                    error_type='invalid_or_unsupported_input', details=details))
                            else:
                                self.assertEqual(err.getvalue(), f'otelwrench cardinality: {text}\n')

    def test_earlier_syntax_metadata_and_manifest_errors_remain_generic(self):
        cases = [
            ('x{a=oops,__name__="x"} 1\n', 'line 1: expected quoted label value'),
            ('x{a="\\q",__name__="x"} 1\n', 'line 1: unsupported or incomplete escape'),
            ('x{雪="x",__name__="x"} 1\n', 'line 1: expected an ASCII label name or closing brace'),
            ('# TYPE x gauge\n# TYPE x gauge\n' + RAW, "line 2: duplicate TYPE for 'x'"),
            (RAW.rstrip('\n'), 'Prometheus text input must end with a newline'),
            (RAW + '\r\n', 'CR/CRLF and NUL are unsupported; use UTF-8 with LF line endings'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for raw, text in cases:
                with self.subTest(text=text):
                    self.assert_error(lambda: parse(raw), text, None)
                    path = self.manifest(root, raw)
                    self.assert_error(lambda: load_scrape_set(path), f'scrape {ID!r} (late.prom): {text}', None)
            path = self.manifest(root)
            data = json.loads(path.read_text())
            data['scrapes'][1]['id'] = 'first'
            path.write_text(json.dumps(data))
            self.assert_error(lambda: load_scrape_set(path), "duplicate scrape id 'first'", None)

    def test_reserved_precedes_deferred_checks_but_not_earlier_duplicate_label(self):
        for prefix in ('x 1\nx 2\n', '# TYPE h histogram\nh_bucket 1\n',
                       '# TYPE h histogram\n# TYPE h_count gauge\nh_count 1\n',
                       'x 1\n# HELP x late\n'):
            line = prefix.count('\n') + 1
            with self.subTest(prefix=prefix):
                self.assert_error(lambda: parse(prefix + 'z{__foo=oops} 1\n'),
                    f"line {line}: reserved/internal label '__foo' is unsupported",
                    dict(code='reserved_input_label', line=line, label='__foo'))
        self.assert_error(lambda: parse(RAW + 'z nope\n'), MESSAGE, DETAILS)
        self.assert_error(lambda: parse('x{a="x",a=oops,__name__="y"} 1\n'),
                          "line 1: duplicate label 'a'",
                          dict(code='duplicate_input_label', line=1, label='a'))


if __name__ == '__main__':
    unittest.main()
