"""Bounded, read-only UTF-8 exact overlays compose the existing text contract."""
from copy import deepcopy
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


class ConfigFileOverrideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'overrides-λ.json'
        self.base = Config.from_json('{"max_series_growth_percent":20,"policy":{"family_removed":"warn"}}')

    def write(self, text):
        data = text.encode('utf-8') if isinstance(text, str) else text
        self.path.write_bytes(data)
        return data

    def test_concrete_overlay_and_independent_retained_successive_values(self):
        data = self.write('{"max_series_growth_percent":0.099999999999999999,"policy":{"series_growth":"warn"}}')
        saved = self.base.to_json()
        for path in [self.path, str(self.path)]:
            derived = self.base.with_overrides_file(path)
            self.assertEqual(derived.policy, dict(DEFAULT_POLICY, family_removed='warn', series_growth='warn'))
            report = compare(parse_snapshot(wire(1000)), parse_snapshot(wire(1001)), derived, include_raw_changes=True)
            self.assertEqual(report['status'], 'warn')
            self.assertTrue(report['observed_series']['growth_limit_exceeded'])
            self.assertEqual(report['observed_series']['max_growth_percent_exact'], '0.099999999999999999')
            self.assertEqual([e['kind'] for e in report['changes']], ['series_growth', 'series_added'])
            self.assertEqual(self.base.to_json(), saved)
            self.assertEqual(self.path.read_bytes(), data)
        for text in ['{}', '{"policy":{}}', '{"policy":{"series_missing":"fail"}}']:
            self.write(text)
            copy = derived.with_overrides_file(self.path)
            self.assertIsNot(copy, derived)
            self.assertIsNot(copy.policy, derived.policy)
            self.assertEqual(copy.max_series_growth_percent, derived.max_series_growth_percent)
            self.assertEqual(copy.policy['family_removed'], 'warn')
            copy.policy['family_removed'] = 'fail'
            self.assertEqual(derived.policy['family_removed'], 'warn')
        self.assertEqual(self.base.to_json(), saved)
        self.assertEqual(DEFAULT_POLICY['family_removed'], 'fail')

    def test_exact_replacements_export_reload_and_decimal_context(self):
        left, right = parse_snapshot(wire(1000)), parse_snapshot(wire(1001))
        for token in ['0.099999999999999999', '9.9999999999999999e-2', '0.100000000000000001',
                      '1e-9999', '1e-99999999999999999999', '-0e-99999999999999999999', '1e6']:
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
                    derived = self.base.with_overrides_file(self.path, max_bytes=len(data))
                    expected = self.base.with_overrides_json(text, source=str(self.path))
                    self.assertEqual(derived.to_json(), expected.to_json())
                    report = compare(left, right, derived, include_raw_changes=True)
                    self.assertEqual(report, compare(left, right, expected, include_raw_changes=True))
                    restored = Config.from_json(derived.to_json())
                    self.assertEqual(restored.to_json(), derived.to_json())
                    self.assertEqual(compare(left, right, restored, include_raw_changes=True), report)
                    self.write('{}')
                    retained = derived.with_overrides_file(self.path)
                    self.assertEqual(retained.to_json(), derived.to_json())
                    self.assertEqual(str(ctx), saved)

    def test_bounded_binary_read_and_inclusive_utf8_byte_boundary(self):
        # Non-ASCII whitespace inside a string is valid JSON but invalid schema;
        # byte limiting must happen before schema, without counting characters.
        data = self.write('{"policy":{},"λ":0}')
        with self.assertRaisesRegex(InputError, f'exceeds {len(data)-1} byte limit'):
            self.base.with_overrides_file(self.path, max_bytes=len(data)-1)
        with self.assertRaisesRegex(InputError, 'config must be an object'):
            self.base.with_overrides_file(self.path, max_bytes=len(data))
        data = self.write('{}\r\n ')
        self.assertEqual(self.base.with_overrides_file(self.path, max_bytes=len(data)), self.base)
        with self.assertRaisesRegex(InputError, 'exceeds 0 byte limit'):
            self.base.with_overrides_file(self.path, max_bytes=0)
        self.write(b'')
        with self.assertRaisesRegex(InputError, 'invalid JSON'):
            self.base.with_overrides_file(self.path, max_bytes=0)
        # A recorded read proves the cap without allocating 64 MiB.
        for limit, size in [(8, 9), (64 * 1024 * 1024, 64 * 1024 * 1024 + 1)]:
            stream = io.BytesIO(b'{}')
            with patch.object(Path, 'open', return_value=stream) as opened, \
                    patch.object(stream, 'read', wraps=stream.read) as read:
                kwargs = {} if limit != 8 else {'max_bytes': limit}
                self.base.with_overrides_file(self.path, **kwargs)
                opened.assert_called_once_with('rb')
                read.assert_called_once_with(size)

    def test_invalid_arguments_fail_before_open_without_mutation(self):
        saved = self.base.to_json()
        self.write('{}')
        for path in [None, b'overrides.json', 0, True, [], object(), 'bad\0path', Path('bad\0path')]:
            with self.subTest(path=path), patch.object(Path, 'open') as opened:
                with self.assertRaisesRegex(InputError, 'path'):
                    self.base.with_overrides_file(path)
                opened.assert_not_called()
        for limit in [None, True, False, 1.0, '8', -1, sys.maxsize, 10**100]:
            with self.subTest(limit=limit), patch.object(Path, 'open') as opened:
                with self.assertRaisesRegex(InputError, 'max_bytes'):
                    self.base.with_overrides_file(self.path, max_bytes=limit)
                opened.assert_not_called()
        # The largest supported read size is not allowed to overflow read().
        with patch.object(Path, 'open', return_value=io.BytesIO(b'{}')):
            self.assertEqual(self.base.with_overrides_file(self.path, max_bytes=sys.maxsize-1), self.base)
        self.assertEqual(self.base.to_json(), saved)

    def test_read_decode_errors_are_path_labeled_and_leave_files_unchanged(self):
        saved = self.base.to_json()
        for path in [self.path, self.path.parent]:
            with self.subTest(path=path):
                with self.assertRaises(InputError) as error:
                    self.base.with_overrides_file(path)
                self.assertTrue(str(error.exception).startswith(str(path) + ': '))
        for data in [b'\xff{}', b'{}\xc3']:
            self.write(data)
            with self.assertRaises(InputError) as error:
                self.base.with_overrides_file(self.path)
            self.assertTrue(str(error.exception).startswith(str(self.path) + ': '))
            self.assertIn('utf-8', str(error.exception))
            self.assertEqual(self.path.read_bytes(), data)
        with patch.object(Path, 'open', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(InputError, 'denied') as error:
                self.base.with_overrides_file(self.path)
            self.assertTrue(str(error.exception).startswith(str(self.path) + ': '))
        for failure in [OSError('read failed'), OverflowError('read size unsupported')]:
            stream = io.BytesIO(b'{}')
            with patch.object(Path, 'open', return_value=stream), \
                    patch.object(stream, 'read', side_effect=failure):
                with self.assertRaises(InputError) as error:
                    self.base.with_overrides_file(self.path)
                self.assertEqual(str(error.exception), f'{self.path}: {failure}')
        self.write(b'{}\xff')
        with self.assertRaisesRegex(InputError, 'exceeds 2 byte limit'):
            self.base.with_overrides_file(self.path, max_bytes=2)
        self.assertEqual(self.base.to_json(), saved)

    def test_json_and_schema_errors_match_shared_text_validation(self):
        bad = ['{', '{} {}', '\ufeff{}', '[', '[]', 'null', '{"unknown":1}', '{"policy":null}',
               '{"policy":{"series_growth":"ignore"}}', '{"policy":{},"policy":{}}',
               '{"policy":{"series_growth":"warn","series_growth":"fail"}}',
               '[' * 10000 + '0' + ']' * 10000]
        bad += ['{"max_series_growth_percent":' + v + '}' for v in
                ['NaN', 'Infinity', '-Infinity', '1e999', '-1e-99999999999999999999',
                 '1000000.00000000001', 'true', 'null', '"20"']]
        saved = self.base.to_json()
        for text in bad:
            with self.subTest(text=text[:100]), localcontext() as ctx:
                ctx.prec, ctx.Emin, ctx.Emax = 1, -1, 1
                for signal in ctx.traps:
                    ctx.traps[signal] = True
                ctx.flags[Inexact] = True
                context = str(ctx)
                data = self.write(text)
                with self.assertRaises(InputError) as expected:
                    self.base.with_overrides_json(text, source=str(self.path))
                with self.assertRaises(InputError) as error:
                    self.base.with_overrides_file(self.path)
                self.assertEqual(str(error.exception), str(expected.exception))
                self.assertEqual(self.path.read_bytes(), data)
                self.assertEqual(self.base.to_json(), saved)
                self.assertEqual(str(ctx), context)

    def test_invalid_base_cannot_be_repaired_read_decode_precede_validation(self):
        for fields in [{'policy': None}, {'policy': {'unknown': 'warn'}},
                       {'max_series_growth_percent': -1}, {'max_series_growth_percent': True}]:
            base = Config(**deepcopy(fields))
            saved = deepcopy(base)
            for text in ['{}', '[]', Config().to_json()]:
                self.write(text)
                with self.assertRaises(InputError) as expected:
                    base.with_overrides_json(text, source=str(self.path))
                with self.assertRaises(InputError) as error:
                    base.with_overrides_file(self.path)
                self.assertEqual(str(error.exception), str(expected.exception))
                self.assertEqual(base, saved)
            self.write('{')
            with self.assertRaisesRegex(InputError, 'invalid JSON'):
                base.with_overrides_file(self.path)
            with self.assertRaisesRegex(InputError, 'exceeds 0 byte limit'):
                base.with_overrides_file(self.path, max_bytes=0)
            self.write(b'\xff')
            with self.assertRaisesRegex(InputError, 'utf-8'):
                base.with_overrides_file(self.path)
            self.assertEqual(base, saved)


if __name__ == '__main__':
    unittest.main()
