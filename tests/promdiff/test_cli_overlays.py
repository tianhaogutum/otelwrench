"""Ordered CLI file overlays preserve exact budgets and atomic diagnostics."""
from contextlib import redirect_stderr, redirect_stdout
from decimal import Inexact, localcontext
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from otelwrench.promdiff.cli import main
from otelwrench.promdiff.config import DEFAULT_POLICY


def wire(n, name='x', key='id'):
    return f'# TYPE {name} gauge\n' + ''.join(f'{name}{{{key}="{i}"}} 1\n' for i in range(n))


class CliOverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.files = {}
        self.left = self.write('before.prom', wire(1000))
        self.right = self.write('after.prom', wire(1001))
        self.base = self.write('base.json', '{"max_series_growth_percent":20,"policy":{"family_removed":"warn"}}')
        self.overlay = self.write('overlay-λ.json', '{"max_series_growth_percent":0.099999999999999999,'
                                  '"policy":{"series_growth":"warn"}}')

    def tearDown(self):
        self.assertEqual(set(self.directory.iterdir()), set(self.files))
        for path, data in self.files.items():
            self.assertEqual(path.read_bytes(), data, str(path))

    def write(self, name, text):
        path = self.directory / name
        data = text.encode('utf-8') if isinstance(text, str) else text
        path.write_bytes(data)
        self.files[path] = data
        return path

    def invoke(self, *args, fmt='json', left=None, right=None):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(map(str, [left or self.left, right or self.right,
                                           *args, '--format', fmt])))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def report(self, *args, code=0, **kwargs):
        actual, out, err = self.invoke(*args, **kwargs)
        self.assertEqual(actual, code, err)
        self.assertEqual(err, '')
        return json.loads(out)

    def test_concrete_exact_overlay(self):
        data = self.report('--config', self.base, '--config-overlay', self.overlay, '--include-raw-changes')
        self.assertEqual(data['status'], 'warn')
        self.assertEqual(data['policy'], dict(DEFAULT_POLICY, family_removed='warn', series_growth='warn'))
        self.assertTrue(data['observed_series']['growth_limit_exceeded'])
        self.assertEqual(data['observed_series']['max_growth_percent_exact'], '0.099999999999999999')
        for key in ['changes', 'raw_changes']:
            self.assertEqual([e['kind'] for e in data[key]], ['series_growth', 'series_added'])

    def test_order_replacement_retention_and_base_always_first(self):
        strict = self.write('strict.json', '{"policy":{"series_growth":"fail","series_added":"fail"}}')
        empty = self.write('empty.json', '{"policy":{}}')
        loose = self.write('loose.json', '{"max_series_growth_percent":0.1,"policy":{"series_added":"warn"}}')
        # --config position never changes its role as the base.
        args = ['--config-overlay', self.overlay, '--config', self.base,
                '--config-overlay', strict, '--config-overlay', empty]
        first = self.report(*args, code=1)
        self.assertEqual(first['policy']['family_removed'], 'warn')
        self.assertEqual(first['policy']['series_growth'], 'fail')
        self.assertEqual(first['observed_series']['max_growth_percent_exact'], '0.099999999999999999')
        last = self.report(*args, '--config-overlay', loose)
        self.assertEqual(last['status'], 'warn')
        self.assertFalse(last['observed_series']['growth_limit_exceeded'])
        self.assertNotIn('max_growth_percent_exact', last['observed_series'])
        self.assertEqual([e['kind'] for e in last['changes']], ['series_added'])
        reverse = self.report('--config-overlay', loose, '--config-overlay', self.overlay)
        self.assertTrue(reverse['observed_series']['growth_limit_exceeded'])
        self.assertEqual(reverse['policy']['family_removed'], 'fail')
        # A new invocation uses independent defaults, including after an empty overlay.
        for args in [[], ['--config-overlay', empty]]:
            default = self.report(*args)
            self.assertEqual(default['policy'], DEFAULT_POLICY)
            self.assertEqual(default['observed_series']['max_growth_percent'], 20)

    def test_exact_replacements_and_decimal_context(self):
        cases = [('9.9999999999999999e-2', True, True), ('0.100000000000000001', False, True),
                 ('1e-9999', True, True), ('1e-99999999999999999999', True, True),
                 ('-0e-99999999999999999999', True, False), ('0', True, False),
                 ('1e6', False, False)]
        empty = self.write('empty.json', '{}')
        for token, exceeds, evidence in cases:
            path = self.write('numeric.json', '{"max_series_growth_percent":' + token + '}')
            with self.subTest(token=token), localcontext() as ctx:
                ctx.prec, ctx.Emin, ctx.Emax = 1, -1, 1
                for signal in ctx.traps:
                    ctx.traps[signal] = True
                ctx.flags[Inexact] = True
                saved = str(ctx)
                data = self.report('--config-overlay', self.overlay, '--config-overlay', path,
                                   '--config-overlay', empty, '--include-raw-changes')
                self.assertEqual(str(ctx), saved)
                stats = data['observed_series']
                self.assertEqual(stats['growth_limit_exceeded'], exceeds)
                self.assertEqual('max_growth_percent_exact' in stats, evidence)
                if evidence:
                    self.assertEqual(stats['max_growth_percent_exact'], token)
                self.assertEqual('series_growth' in [e['kind'] for e in data['raw_changes']], exceeds)

    def test_effective_policy_projection_allowlist_and_raw_evidence(self):
        left = self.write('renamed-before.prom', wire(1000, 'old', 'old_key'))
        mapping = self.write('map.json', '{"metrics":{"old":"x"},"labels":{"old":{"old_key":"id"}}}')
        strict = self.write('strict.json', '{"policy":{"series_growth":"fail"}}')
        event = {'kind': 'series_growth', 'family': None, 'before': 1000, 'after': 1001}
        allowed = self.write('allow.json', json.dumps([event]))
        args = ['--config', self.base, '--config-overlay', self.overlay, '--config-overlay', strict,
                '--rename-map', mapping, '--include-raw-changes']
        for extra, code in [([], 1), (['--allowlist', allowed], 0)]:
            data = self.report(*args, *extra, left=left, code=code)
            self.assertEqual(data['changes'][0], dict(event, severity='fail', allowed=bool(extra)))
            self.assertEqual(data['raw_changes'][0], event)
            self.assertEqual([e['kind'] for e in data['raw_changes']],
                             ['series_growth', 'family_removed', 'family_added'])
            self.assertEqual(data['observed_series']['new'], 1)
            self.assertEqual(data['observed_series']['missing'], 0)
        code, out, err = self.invoke(*args, '--allowlist', allowed, left=left, fmt='text')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('ALLOW series_growth (snapshot): 1000 -> 1001', out)
        self.assertIn('Exact growth budget: 0.099999999999999999%', out)
        self.assertIn('RAW family_removed old:', out)
        boundary = self.write('boundary.json', '{"max_series_growth_percent":0.1}')
        code, out, err = self.invoke(*args, '--config-overlay', boundary, '--allowlist', allowed, left=left)
        self.assertEqual((code, out), (2, ''))
        self.assertIn('unused allowlist', json.loads(err)['error'])

    def test_invalid_overlays_stop_without_partial_reports_or_snapshot_reads(self):
        bad = ['', '{', '{} {}', '\ufeff{}', '[]', 'null', '{"unknown":1}', '{"policy":null}',
               '{"policy":{"unknown":"warn"}}', '{"policy":{"series_growth":"ignore"}}',
               '{"policy":{},"policy":{}}', '{"policy":{"series_growth":"warn","series_growth":"fail"}}',
               '[' * 2000 + '0' + ']' * 2000]
        bad += ['{"max_series_growth_percent":' + token + '}' for token in
                ['NaN', 'Infinity', '-Infinity', '1e999', '-1', '1000001',
                 '-1e-99999999999999999999', '1000000.00000000001', '1.00000000000000001e6',
                 'true', 'null', '"20"', '[]', '{}']]
        for text in bad:
            path = self.write('invalid.json', text)
            for fmt in ['json', 'text']:
                with self.subTest(text=text[:80], fmt=fmt), patch('otelwrench.promdiff.cli.parse_snapshot') as parse:
                    code, out, err = self.invoke('--config-overlay', self.overlay,
                                                 '--config-overlay', path, '--config-overlay', self.overlay, fmt=fmt)
                    self.assertEqual((code, out), (2, ''))
                    self.assertNotIn('Traceback', err)
                    if fmt == 'json':
                        error = json.loads(err)
                        self.assertEqual(set(error), {'report_version', 'status', 'error'})
                        self.assertEqual((error['report_version'], error['status']), (1, 'error'))
                    else:
                        self.assertTrue(err.startswith('otelwrench promdiff: '))
                    parse.assert_not_called()

    def test_io_diagnostics_and_invalid_config_precedence(self):
        missing = self.directory / 'missing.json'
        malformed = self.write('malformed.json', '{')
        invalid_utf8 = self.write('invalid-utf8.json', b'{}\xff')
        duplicate = self.write('duplicate.json', '{"policy":{},"policy":{}}')
        for path in [missing, self.directory, malformed, invalid_utf8, duplicate]:
            for fmt in ['json', 'text']:
                with self.subTest(path=path, fmt=fmt):
                    code, out, err = self.invoke('--config-overlay', path, fmt=fmt)
                    self.assertEqual((code, out), (2, ''))
                    message = json.loads(err)['error'] if fmt == 'json' else err
                    self.assertIn(str(path) + ':', message)
        for args, first in [(['--config-overlay', missing, '--config', malformed], malformed),
                            (['--config-overlay', malformed, '--config-overlay', missing], malformed)]:
            code, out, err = self.invoke(*args)
            self.assertEqual((code, out), (2, ''))
            self.assertIn(str(first), json.loads(err)['error'])
            self.assertNotIn(str(missing), err)

    def test_inclusive_per_file_byte_cap_before_utf8_and_json(self):
        left = self.write('tiny-before.prom', 'x 1\n')
        right = self.write('tiny-after.prom', 'x 1\n')
        # Each file fits, although the aggregate exceeds the cap. Count UTF-8 bytes.
        one = self.write('one.json', '{"policy":{}}' + ' ' * 51)
        two = self.write('two.json', '{"policy":{}}' + ' ' * 51)
        bad = self.write('large.json', b'{}' + b' ' * 62 + b'\xff')
        with patch('otelwrench.promdiff.cli.MAX_INPUT_BYTES', 64):
            data = self.report('--config', one, '--config-overlay', one, '--config-overlay', two,
                               left=left, right=right)
            self.assertEqual(data['status'], 'pass')
            for fmt in ['json', 'text']:
                code, out, err = self.invoke('--config-overlay', bad, left=left, right=right, fmt=fmt)
                self.assertEqual((code, out), (2, ''))
                self.assertIn(str(bad) + ': input exceeds 64 byte limit', err)
                self.assertNotIn('utf-8', err)


if __name__ == '__main__':
    unittest.main()
