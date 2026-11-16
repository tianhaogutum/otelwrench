from contextlib import redirect_stderr, redirect_stdout
from decimal import Inexact, InvalidOperation, ROUND_DOWN, ROUND_UP, localcontext
from io import StringIO
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from otelwrench.promdiff.cli import main, read_text
from otelwrench.promdiff.parser import InputError

ROOT = Path(__file__).resolve().parents[2]


class CliTests(unittest.TestCase):
    def test_growth_cli_main_preserves_nondefault_decimal_context(self):
        def wire(n):
            return '# TYPE x gauge\n' + ''.join(f'x{{id="{i}"}} 1\n' for i in range(n))

        left = self.make_file('before.prom', wire(3))
        right = self.make_file('after.prom', wire(4))
        config = self.make_file('config.json', '{"max_series_growth_percent":33}')
        for trap in [True, False]:
            for fmt in ['json', 'text']:
                with self.subTest(trap=trap, fmt=fmt), localcontext() as ctx:
                    ctx.prec = 1
                    ctx.traps[InvalidOperation] = trap
                    ctx.clear_flags()
                    original = str(ctx)
                    out, err = StringIO(), StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        code = main([str(left), str(right), '--config', str(config),
                                     '--format', fmt, '--include-raw-changes'])
                    self.assertEqual(code, 1)
                    self.assertEqual(err.getvalue(), '')
                    if fmt == 'json':
                        result = json.loads(out.getvalue())
                        self.assertEqual(result['status'], 'fail')
                        self.assertEqual(result['observed_series']['net_growth_percent'], 33.333333)
                        self.assertTrue(result['observed_series']['growth_limit_exceeded'])
                        self.assertEqual(result['raw_changes'][0]['kind'], 'series_growth')
                        json.dumps(result, allow_nan=False)
                    else:
                        self.assertIn('FAIL series_growth (snapshot): 3 -> 4', out.getvalue())
                        self.assertIn('RAW series_growth (snapshot): 3 -> 4', out.getvalue())
                    self.assertEqual(str(ctx), original)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "otelwrench", "promdiff", *map(str, args)],
                              cwd=ROOT, text=True, capture_output=True, timeout=60)

    def make_file(self, name, content):
        path = self.directory / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def test_compatible_json_exit_zero_and_independent_contract_check(self):
        args = ["examples/promdiff/before.prom", "examples/promdiff/compatible.prom", "--format", "json"]
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        data = json.loads(result.stdout)
        self.assertEqual(data["status"], "pass")
        self.assertEqual(data["candidate"], {"families": 2, "observed_series": 3})
        self.assertEqual(data["changes"], [])
        self.assertEqual(data["contracts"]["candidate"], {
            "queue_depth": {"type": "gauge", "help": "Jobs waiting by queue.", "unit": None,
                            "samples": [{"name": "queue_depth", "label_keys": ["queue"]}]},
            "jobs_processed_total": {"type": "counter", "help": "Completed jobs.", "unit": None,
                                     "samples": [{"name": "jobs_processed_total", "label_keys": ["result"]}]},
        })
        self.assertEqual(self.run_cli(*args).stdout, result.stdout)

    def test_breaking_json_exit_one(self):
        result = self.run_cli("examples/promdiff/before.prom", "examples/promdiff/breaking.prom", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["summary"], {"allowed": 0, "failures": 1, "warnings": 2})
        event = data["changes"][0]
        self.assertEqual(event["kind"], "label_schema_changed")
        self.assertEqual(event["before"], [{"name": "queue_depth", "label_keys": ["queue"]}])
        self.assertEqual(event["after"], [{"name": "queue_depth", "label_keys": ["queue", "region"]}])
        self.assertEqual(data["observed_series"]["new"], 2)
        self.assertEqual(data["observed_series"]["missing"], 2)

    def test_malformed_json_exit_two(self):
        result = self.run_cli("examples/promdiff/before.prom", "examples/promdiff/malformed.prom", "--format", "json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr)["status"], "error")
        self.assertIn("examples/promdiff/malformed.prom:2", json.loads(result.stderr)["error"])

    def test_text_output(self):
        result = self.run_cli("examples/promdiff/before.prom", "examples/promdiff/breaking.prom")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL label_schema_changed queue_depth:", result.stdout)
        self.assertIn("Observed series: 3 -> 3; new=2, missing=2", result.stdout)

    def test_exact_allowlist_via_cli(self):
        result = self.run_cli("examples/promdiff/before.prom", "examples/promdiff/breaking.prom",
                              "--allowlist", "examples/promdiff/expected-changes.json", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["summary"], {"allowed": 1, "failures": 0, "warnings": 2})

    def test_policy_via_cli(self):
        policy = self.make_file("policy.json", '{"policy":{"label_schema_changed":"warn"}}')
        result = self.run_cli("examples/promdiff/before.prom", "examples/promdiff/breaking.prom", "--config", policy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("WARN:"))

    def test_invalid_config_json_exit_two(self):
        for content in ['{', '{"policy": {"family_removed":"fail", "family_removed":"warn"}}',
                        '{"max_series_growth_percent": NaN}', '{"max_series_growth_percent": 1e999}',
                        '{"policy": {"unknown": "warn"}}', '{"allow_everything": true}',
                        '{"max_series_growth_percent": true}']:
            with self.subTest(content=content):
                path = self.make_file("policy.json", content)
                result = self.run_cli("examples/promdiff/before.prom", "examples/promdiff/compatible.prom", "--config", path)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_growth_config_rejects_exact_out_of_range_numbers_before_rounding(self):
        for token in ['-1e-9999', '-1e-99999999999999999999',
                      '1000000.00000000001', '1.00000000000000001e6']:
            for fmt in ['json', 'text']:
                with self.subTest(token=token, fmt=fmt):
                    path = self.make_file('policy.json', '{"max_series_growth_percent":' + token + '}')
                    result = self.run_cli('examples/promdiff/before.prom', 'examples/promdiff/compatible.prom',
                                          '--config', path, '--format', fmt)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertIn('max_series_growth_percent', result.stderr)
                    self.assertIn('0..1000000', result.stderr)
                    self.assertNotIn('Traceback', result.stderr)
                    if fmt == 'json':
                        self.assertEqual(json.loads(result.stderr)['status'], 'error')

    def test_growth_config_range_checks_preserve_decimal_context_and_valid_output(self):
        # Check actual main() JSON parsing in the caller's context, including
        # signed zero, extreme positive underflow, and both inclusive endpoints.
        for token in ['0', '-0.0', '-0e-99999999999999999999', '1e-9999',
                      '1e-99999999999999999999', '1000000', '1000000.0',
                      '1e6', '999999.99999999999', '0.099999999999999999',
                      '-1e-9999', '1000000.00000000001']:
            path = self.make_file('policy.json', '{"max_series_growth_percent":' + token + '}')
            rejected = token in ['-1e-9999', '1000000.00000000001']
            for trap in [True, False]:
                with self.subTest(token=token, trap=trap), localcontext() as ctx:
                    ctx.prec, ctx.Emin, ctx.Emax = 1, -1, 1
                    for signal in ctx.traps:
                        ctx.traps[signal] = True
                    ctx.traps[InvalidOperation] = trap
                    ctx.clear_flags()
                    original = str(ctx)
                    out, err = StringIO(), StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        code = main(['examples/promdiff/before.prom', 'examples/promdiff/compatible.prom',
                                     '--config', str(path), '--format', 'json'])
                    self.assertEqual(str(ctx), original)
                    self.assertEqual(code, 2 if rejected else 0)
                    if rejected:
                        self.assertEqual(out.getvalue(), '')
                        self.assertIn('0..1000000', json.loads(err.getvalue())['error'])
                    else:
                        self.assertEqual(err.getvalue(), '')
                        data = json.loads(out.getvalue())
                        self.assertEqual(data['status'], 'pass')
                        self.assertEqual(data['observed_series']['max_growth_percent'], float(token))
                        json.dumps(data, allow_nan=False)

    def test_growth_json_token_is_exact_for_gate_and_report_evidence(self):
        def wire(n):
            return '# TYPE x gauge\n' + ''.join(f'x{{id="{i}"}} 1\n' for i in range(n))

        left = self.make_file('before.prom', wire(1000))
        right = self.make_file('after.prom', wire(1001))
        # All three display as 0.1. Only the first exceeds the exact budget.
        for token, exceeds, evidence in [
                ('0.099999999999999999', True, True),
                ('9.9999999999999999e-2', True, True),
                ('0.1', False, False), ('1e-1', False, False),
                ('0.1' + '0' * 5000, False, False),
                ('0.100000000000000001', False, True),
                ('1e-99999999999999999999', True, True),
                ('-0e-99999999999999999999', True, False)]:
            config = self.make_file('config.json', '{"max_series_growth_percent":' + token + '}')
            for trap in [True, False]:
                for rounding in [ROUND_DOWN, ROUND_UP]:
                    with self.subTest(token=token, trap=trap, rounding=rounding), localcontext() as ctx:
                        ctx.prec, ctx.Emin, ctx.Emax, ctx.rounding = 1, -1, 1, rounding
                        for signal in ctx.traps:
                            ctx.traps[signal] = True
                        ctx.traps[InvalidOperation] = trap
                        ctx.clear_flags()
                        ctx.flags[Inexact] = True
                        original = str(ctx)
                        out, err = StringIO(), StringIO()
                        with redirect_stdout(out), redirect_stderr(err):
                            code = main([str(left), str(right), '--config', str(config),
                                         '--include-raw-changes', '--format', 'json'])
                        self.assertEqual(str(ctx), original)
                        self.assertEqual(code, int(exceeds))
                        self.assertEqual(err.getvalue(), '')
                        result = json.loads(out.getvalue())
                        self.assertEqual(result['report_version'], 1)
                        self.assertEqual(result['status'], 'fail' if exceeds else 'warn')
                        stats = result['observed_series']
                        self.assertEqual(stats['net_growth_percent'], 0.1)
                        self.assertEqual(stats['max_growth_percent'], float(token))
                        self.assertEqual(stats['growth_limit_exceeded'], exceeds)
                        if evidence:
                            self.assertEqual(stats['max_growth_percent_exact'], token)
                        else:
                            self.assertNotIn('max_growth_percent_exact', stats)
                        kinds = ['series_growth', 'series_added'] if exceeds else ['series_added']
                        self.assertEqual([e['kind'] for e in result['changes']], kinds)
                        self.assertEqual([e['kind'] for e in result['raw_changes']], kinds)
                        json.dumps(result, allow_nan=False)

    def test_exact_json_growth_preserves_projection_policy_and_allowlists(self):
        def wire(name, key, n):
            return f'# TYPE {name} gauge\n' + ''.join(
                f'{name}{{{key}="{i}"}} 1\n' for i in range(n))

        left = self.make_file('before.prom', wire('old', 'old_key', 1000))
        right = self.make_file('after.prom', wire('x', 'id', 1001))
        mapping = self.make_file('map.json', json.dumps({
            'metrics': {'old': 'x'}, 'labels': {'old': {'old_key': 'id'}}}))
        event = {'kind': 'series_growth', 'family': None, 'before': 1000, 'after': 1001}
        for policy, allowed, code, status in [({}, [], 1, 'fail'),
                ({'series_growth': 'warn'}, [], 0, 'warn'), ({}, [event], 0, 'warn')]:
            config = self.make_file('config.json', '{"max_series_growth_percent":0.099999999999999999,'
                                    + '"policy":' + json.dumps(policy) + '}')
            allowlist = self.make_file('allow.json', json.dumps(allowed))
            for fmt in ['json', 'text']:
                with self.subTest(policy=policy, allowed=allowed, fmt=fmt):
                    result = self.run_cli(left, right, '--config', config, '--allowlist', allowlist,
                                          '--rename-map', mapping, '--include-raw-changes', '--format', fmt)
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertEqual(result.stderr, '')
                    if fmt == 'json':
                        data = json.loads(result.stdout)
                        self.assertEqual(data['status'], status)
                        self.assertEqual(data['observed_series']['new'], 1)
                        self.assertEqual(data['observed_series']['missing'], 0)
                        self.assertEqual(data['changes'][0], dict(event,
                            severity=policy.get('series_growth', 'fail'), allowed=bool(allowed)))
                        self.assertEqual(data['raw_changes'][0], event)
                        self.assertEqual([e['kind'] for e in data['raw_changes']],
                                         ['series_growth', 'family_removed', 'family_added'])
                    else:
                        self.assertIn('RAW series_growth (snapshot): 1000 -> 1001', result.stdout)
                        self.assertIn('Exact growth budget: 0.099999999999999999%', result.stdout)
        # The inclusive boundary generates no growth event, making that exact
        # allowlist entry stale. Rounded evidence must not be used for matching.
        config.write_text('{"max_series_growth_percent":0.1}')
        result = self.run_cli(left, right, '--config', config, '--rename-map', mapping,
                              '--allowlist', allowlist, '--format', 'json')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn('unused allowlist', json.loads(result.stderr)['error'])

    def test_exact_json_growth_zero_shrink_and_churn(self):
        config = self.make_file('config.json', '{"max_series_growth_percent":1e-99999999999999999999}')
        for before, after, exceeds, percent in [
                ('', '', False, 0.0), ('', 'x 1\n', True, None),
                ('x 1\n', '', False, 0.0),
                ('x{id="a"} 1\n', 'x{id="b"} 1\n', False, 0.0)]:
            with self.subTest(before=before, after=after):
                left = self.make_file('before.prom', before)
                right = self.make_file('after.prom', after)
                result = self.run_cli(left, right, '--config', config,
                                      '--include-raw-changes', '--format', 'json')
                self.assertIn(result.returncode, (0, 1), result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data['observed_series']['growth_limit_exceeded'], exceeds)
                self.assertEqual(data['observed_series']['net_growth_percent'], percent)
                self.assertEqual('series_growth' in [e['kind'] for e in data['raw_changes']], exceeds)

    def test_invalid_allowlist_exit_two(self):
        for content in ['[{"kind":"family_removed","family":"*","before":{},"after":null}]',
                        '[{"kind":"family_removed","family":"x","before":1e999,"after":null}]',
                        '[{"kind":"family_removed","family":"x","before":{},"after":null}]',
                        '{"family":"*"}']:
            with self.subTest(content=content):
                path = self.make_file("allow.json", content)
                result = self.run_cli("examples/promdiff/before.prom", "examples/promdiff/compatible.prom", "--allowlist", path)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_missing_files_and_invalid_utf8_exit_two(self):
        for path in [self.directory / "missing.prom", self.make_file("invalid.prom", b"x \xff\n"), self.directory]:
            with self.subTest(path=path):
                result = self.run_cli("examples/promdiff/before.prom", path, "--format", "json")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stderr)["status"], "error")
                self.assertEqual(result.stdout, "")

    def test_input_size_limit(self):
        path = self.make_file("oversize.prom", "x 1\n")
        with patch("otelwrench.promdiff.cli.MAX_INPUT_BYTES", 3), self.assertRaisesRegex(InputError, "exceeds"):
            read_text(str(path))

    def test_huge_timestamp_has_diagnostic(self):
        path = self.make_file("huge.prom", "x 1 " + "9" * 5000 + "\n")
        result = self.run_cli(path, path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("int64", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_console_entrypoint_installed(self):
        command = Path(sys.executable).parent / "otelwrench"
        if not command.is_file():
            command = Path(shutil.which("otelwrench") or command)
        self.assertTrue(command.is_file())
        result = subprocess.run([str(command), "promdiff", "examples/promdiff/before.prom", "examples/promdiff/compatible.prom"],
                                cwd=ROOT, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unsupported_cli_args_exit_two(self):
        for args in [("--dry-run",), ("examples/promdiff/before.prom",),
                     ("examples/promdiff/before.prom", "examples/promdiff/compatible.prom", "--format", "yaml")]:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)

    def test_inputs_and_config_never_modified(self):
        before = self.make_file("before.prom", "x 1\n")
        after = self.make_file("after.prom", "x 2\n")
        config = self.make_file("policy.json", "{}\n")
        originals = {p: p.read_bytes() for p in [before, after, config]}
        self.assertEqual(self.run_cli(before, after, "--config", config).returncode, 0)
        self.assertEqual(originals, {p: p.read_bytes() for p in originals})


if __name__ == "__main__":
    unittest.main()
