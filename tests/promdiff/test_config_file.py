"""Standalone bounded file loading preserves the exact text config contract."""
from decimal import Inexact, InvalidOperation, ROUND_UP, localcontext
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from otelwrench.promdiff.compare import Config, DEFAULT_POLICY, compare
from otelwrench.promdiff.parser import InputError, parse_snapshot


def wire(n):
    return '# TYPE x gauge\n' + ''.join(f'x{{id="{i}"}} 1\n' for i in range(n))


class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config-λ.json'

    def write(self, text):
        data = text.encode('utf-8') if isinstance(text, str) else text
        self.path.write_bytes(data)
        return data

    def test_standalone_exact_example_and_independent_effective_policies(self):
        data = self.write('{"max_series_growth_percent":0.099999999999999999,'
                          '"policy":{"family_removed":"warn","series_growth":"warn"}}')
        left, right = parse_snapshot(wire(1000)), parse_snapshot(wire(1001))
        first = Config.from_file(self.path)
        second = Config.from_file(str(self.path))
        self.assertIsNot(first, second)
        self.assertIsNot(first.policy, second.policy)
        self.assertEqual(first.policy, dict(DEFAULT_POLICY, family_removed='warn', series_growth='warn'))
        report = compare(left, right, first, include_raw_changes=True)
        self.assertEqual(report['status'], 'warn')
        self.assertTrue(report['observed_series']['growth_limit_exceeded'])
        self.assertEqual(report['observed_series']['max_growth_percent_exact'], '0.099999999999999999')
        self.assertEqual([e['kind'] for e in report['changes']], ['series_growth', 'series_added'])
        self.assertEqual([e['kind'] for e in report['raw_changes']], ['series_growth', 'series_added'])
        self.assertEqual(self.path.read_bytes(), data)
        first.policy['family_removed'] = 'fail'
        self.assertEqual(second.policy['family_removed'], 'warn')
        for text in ['{}', '{"policy":{}}', second.to_json()]:
            self.write(text)
            loaded = Config.from_file(self.path)
            self.assertEqual(loaded, Config.from_json(text))
            self.assertEqual(loaded.to_json(), Config.from_json(loaded.to_json()).to_json())
            loaded.policy['family_added'] = 'fail'
            self.assertEqual(Config.from_file(self.path).policy['family_added'], 'warn')
        self.assertEqual(DEFAULT_POLICY['family_added'], 'warn')

    def test_exact_budgets_reload_reports_and_decimal_context(self):
        left, right = parse_snapshot(wire(1000)), parse_snapshot(wire(1001))
        cases = [('0.099999999999999999', True, True), ('9.9999999999999999e-2', True, True),
                 ('0.100000000000000001', False, True), ('1e-9999', True, True),
                 ('1e-99999999999999999999', True, True),
                 ('-0e-99999999999999999999', True, False),
                 ('0.1', False, False), ('0', True, False), ('1e6', False, False)]
        for token, exceeds, evidence in cases:
            for trap in [False, True]:
                with self.subTest(token=token, trap=trap), localcontext() as ctx:
                    ctx.prec, ctx.Emin, ctx.Emax, ctx.rounding = 1, -1, 1, ROUND_UP
                    for signal in ctx.traps:
                        ctx.traps[signal] = True
                    ctx.traps[InvalidOperation] = trap
                    ctx.flags[Inexact] = True
                    saved = str(ctx)
                    text = '{"max_series_growth_percent":' + token + '}'
                    data = self.write(text)
                    loaded = Config.from_file(self.path, max_bytes=len(data))
                    report = compare(left, right, loaded, include_raw_changes=True)
                    self.assertEqual(report['observed_series']['growth_limit_exceeded'], exceeds)
                    self.assertEqual('max_growth_percent_exact' in report['observed_series'], evidence)
                    if evidence:
                        self.assertEqual(report['observed_series']['max_growth_percent_exact'], token)
                    self.assertEqual(report, compare(left, right, Config.from_json(text), include_raw_changes=True))
                    exported = loaded.to_json()
                    self.write(exported)
                    reloaded = Config.from_file(self.path)
                    self.assertEqual(reloaded.to_json(), exported)
                    self.assertEqual(Config.from_json(exported).to_json(), exported)
                    self.assertEqual(compare(left, right, reloaded, include_raw_changes=True), report)
                    self.assertEqual(str(ctx), saved)

    def test_bounded_binary_read_inclusive_bytes_and_oversize_precedence(self):
        data = self.write('{"policy":{},"λ":0}')
        with self.assertRaisesRegex(InputError, f'exceeds {len(data)-1} byte limit'):
            Config.from_file(self.path, max_bytes=len(data)-1)
        with self.assertRaisesRegex(InputError, 'config must be an object'):
            Config.from_file(self.path, max_bytes=len(data))
        data = self.write('{}\r\n ')
        self.assertEqual(Config.from_file(self.path, max_bytes=len(data)), Config())
        with self.assertRaisesRegex(InputError, 'exceeds 0 byte limit'):
            Config.from_file(self.path, max_bytes=0)
        self.write(b'')
        with self.assertRaisesRegex(InputError, 'invalid JSON'):
            Config.from_file(self.path, max_bytes=0)
        for data in [b'{}\xff', b'{xx']:
            self.write(data)
            with self.assertRaises(InputError) as error:
                Config.from_file(self.path, max_bytes=2)
            self.assertEqual(str(error.exception), f'{self.path}: input exceeds 2 byte limit')
            self.assertEqual(self.path.read_bytes(), data)
        self.write('{"policy":{}}')
        with self.assertRaises(InputError) as error:
            Config.from_file(self.path, max_bytes=8)
        self.assertEqual(str(error.exception), f'{self.path}: input exceeds 8 byte limit')
        for limit in [8, 64 * 1024 * 1024, sys.maxsize-1]:
            stream = io.BytesIO(b'{}')
            with patch.object(Path, 'open', return_value=stream) as opened, \
                    patch.object(stream, 'read', wraps=stream.read) as read:
                kwargs = {} if limit == 64 * 1024 * 1024 else {'max_bytes': limit}
                self.assertEqual(Config.from_file(self.path, **kwargs), Config())
                opened.assert_called_once_with('rb')
                read.assert_called_once_with(limit+1)

    def test_argument_validation_before_open_and_limit_before_path(self):
        for path in [None, b'config.json', 0, True, [], object(), 'bad\0path', Path('bad\0path')]:
            with self.subTest(path=path), patch.object(Path, 'open') as opened:
                with self.assertRaisesRegex(InputError, 'path must be a str or Path'):
                    Config.from_file(path)
                opened.assert_not_called()
        for limit in [None, True, False, 1.0, '8', -1, sys.maxsize, 10**100]:
            for path in [self.path, None]:
                with self.subTest(limit=limit, path=path), patch.object(Path, 'open') as opened:
                    with self.assertRaisesRegex(InputError, 'max_bytes must be an integer'):
                        Config.from_file(path, max_bytes=limit)
                    opened.assert_not_called()

    def test_io_and_utf8_errors_name_path_and_preserve_bytes(self):
        for path in [self.path, self.path.parent]:
            with self.subTest(path=path):
                with self.assertRaises(InputError) as error:
                    Config.from_file(path)
                self.assertTrue(str(error.exception).startswith(str(path) + ': '))
        for data in [b'\xff{}', b'{}\xc3']:
            self.write(data)
            with self.assertRaises(InputError) as error:
                Config.from_file(self.path)
            self.assertTrue(str(error.exception).startswith(str(self.path) + ': '))
            self.assertIn('utf-8', str(error.exception))
            self.assertEqual(self.path.read_bytes(), data)
        for failure in [PermissionError('denied'), ValueError('unsupported path')]:
            with patch.object(Path, 'open', side_effect=failure):
                with self.assertRaises(InputError) as error:
                    Config.from_file(self.path)
                self.assertEqual(str(error.exception), f'{self.path}: {failure}')
        for failure in [OSError('read failed'), OverflowError('unsupported size')]:
            stream = io.BytesIO(b'{}')
            with patch.object(Path, 'open', return_value=stream), \
                    patch.object(stream, 'read', side_effect=failure):
                with self.assertRaises(InputError) as error:
                    Config.from_file(self.path)
                self.assertEqual(str(error.exception), f'{self.path}: {failure}')

    def test_json_schema_errors_match_text_with_path_and_preserve_decimal_context(self):
        bad = ['', '{', '{} {}', '\ufeff{}', '[]', 'null', '{"unknown":1}',
               '{"policy":null}', '{"policy":{"unknown":"warn"}}',
               '{"policy":{"series_growth":"ignore"}}', '{"policy":{},"policy":{}}',
               '{"policy":{"series_growth":"warn","series_growth":"fail"}}',
               '[' * 2000 + '0' + ']' * 2000]
        bad += ['{"max_series_growth_percent":' + v + '}' for v in
                ['NaN', 'Infinity', '-Infinity', '1e999', '-1', '1000001',
                 '-1e-99999999999999999999', '1000000.00000000001',
                 'true', 'null', '"20"', '[]', '{}']]
        for text in bad:
            for trap in [False, True]:
                with self.subTest(text=text[:100], trap=trap), localcontext() as ctx:
                    ctx.prec, ctx.Emin, ctx.Emax = 1, -1, 1
                    for signal in ctx.traps:
                        ctx.traps[signal] = True
                    ctx.traps[InvalidOperation] = trap
                    ctx.flags[Inexact] = True
                    saved = str(ctx)
                    data = self.write(text)
                    with self.assertRaises(InputError) as expected:
                        Config.from_json(text, source=str(self.path))
                    with self.assertRaises(InputError) as error:
                        Config.from_file(self.path)
                    self.assertEqual(str(error.exception), str(expected.exception))
                    self.assertEqual(self.path.read_bytes(), data)
                    self.assertEqual(str(ctx), saved)


if __name__ == '__main__':
    unittest.main()
