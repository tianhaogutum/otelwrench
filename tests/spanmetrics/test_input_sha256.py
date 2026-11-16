# SPDX-License-Identifier: Apache-2.0
"""Conversion gates exact fully consumed capture bytes before aggregation."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import MagicMock, Mock, patch

from otelwrench.spanmetrics import cli
from . import test_conversion_dimensions as conversion
from . import test_dimension_config as pairs
from .test_jsonl import jsonl, split_requests


class InputSha256Tests(unittest.TestCase):
    setUp = conversion.ConversionDimensionTests.setUp
    save = pairs.DimensionConfigTests.save
    flags = conversion.ConversionDimensionTests.flags
    assert_metrics = conversion.ConversionDimensionTests.assert_metrics

    def digest(self, data):
        return hashlib.sha256(data).hexdigest()

    def run_cli(self, *options, source=pairs.CAPTURE, data=None, expected=None,
                named=False, default_report=False):
        gate = [] if expected is None else ['--expect-input-sha256', expected]
        return subprocess.run(
            [*pairs.CLI, *(['--input', str(source)] if named else [str(source)]),
             '--output', str(self.output),
             *([] if default_report else ['--report', str(self.report)]),
             *gate, *map(str, options)], input=data, capture_output=True, timeout=60,
            cwd=self.directory)

    def pair_options(self, side='candidate', provenance=False):
        return ['--dimensions-file', self.config, '--dimensions-side', side,
                '--expect-dimension-sha256', self.digest(self.config.read_bytes()),
                *(['--dimension-provenance'] if provenance else [])]

    def test_matching_json_preserves_exact_outputs(self):
        data = pairs.CAPTURE.read_bytes()
        for options in ([], self.flags('candidate'), self.pair_options('baseline'),
                        self.pair_options('candidate', True)):
            for dry in (False, True):
                with self.subTest(options=options, dry=dry):
                    extra = ['--force', *options, *(['--dry-run'] if dry else [])]
                    default = self.run_cli(*extra)
                    self.assertEqual(default.returncode, 0, default.stderr)
                    before = {p: p.read_bytes() for p in self.directory.iterdir()}
                    matched = self.run_cli(*extra, expected=self.digest(data))
                    self.assertEqual(matched.returncode, 0, matched.stderr)
                    self.assertEqual(matched.stderr, b'')
                    self.assertEqual(matched.stdout, default.stdout)
                    self.assertEqual({p: p.read_bytes() for p in self.directory.iterdir()}, before)
                    report = json.loads(matched.stdout)
                    self.assertEqual(report['input_sha256'], self.digest(data))
                    self.assertNotIn('expect_input_sha256', report)
                    if '--dimensions-side' in options and not dry:
                        self.assert_metrics(options[options.index('--dimensions-side') + 1])

    def test_jsonl_file_stdin_and_input_forms_exact_output_parity(self):
        data = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes())))
        capture = self.directory / 'capture.jsonl'
        for ending in (b'\n', b'\r\n', b''):
            raw = data.rstrip(b'\n').replace(b'\n', b'\r\n' if ending == b'\r\n' else b'\n') + ending
            capture.write_bytes(raw)
            for source in (capture, '-'):
                for named in (False, True):
                    with self.subTest(ending=ending, source=source, named=named):
                        opts = ['--input-format', 'jsonl', '--force', *self.pair_options('candidate', True)]
                        kw = dict(source=source, data=raw if source == '-' else None, named=named)
                        default = self.run_cli(*opts, **kw)
                        self.assertEqual(default.returncode, 0, default.stderr)
                        metrics, manifest = self.output.read_bytes(), self.report.read_bytes()
                        matched = self.run_cli(*opts, **kw, expected=self.digest(raw))
                        self.assertEqual(matched.returncode, 0, matched.stderr)
                        self.assertEqual(matched.stdout, default.stdout)
                        self.assertEqual(self.output.read_bytes(), metrics)
                        self.assertEqual(self.report.read_bytes(), manifest)
                        self.assertEqual(json.loads(matched.stdout)['input_sha256'], self.digest(raw))
        # JSON also accepts the named input and full equals gate forms.
        matched = self.run_cli('--dry-run', '--expect-input-sha256=' + self.digest(pairs.CAPTURE.read_bytes()), named=True)
        self.assertEqual(matched.returncode, 0, matched.stderr)

    def assert_pre_read_error(self, options, diagnostic):
        stdin = Mock()
        stdin.buffer.read.side_effect = AssertionError('stdin consumed')
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli.sys, 'stdin', stdin), \
             patch.object(cli, 'read_dimension_pair', side_effect=AssertionError('pair read')), \
             patch.object(cli, 'read_input', side_effect=AssertionError('capture read')), \
             patch.object(cli, 'convert', side_effect=AssertionError('conversion started')), \
             patch.object(cli, 'write_files', side_effect=AssertionError('publication started')), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(['-', '--input-format', 'jsonl', '--output', str(self.output),
                                 '--force', *map(str, options)])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 2, stderr.getvalue())
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn(diagnostic, stderr.getvalue())
        stdin.buffer.read.assert_not_called()

    def test_invalid_incomplete_abbreviated_and_incompatible_before_reads(self):
        pair = ['--dimensions-file', self.config, '--dimensions-side', 'baseline']
        for value in ('', 'a' * 63, 'a' * 65, 'A' * 64, 'g' * 64, '0x' + 'a' * 62,
                      ' ' + 'a' * 64, 'a' * 64 + ' ', 'a' * 64 + '\n', '０' * 64, 'é' * 64):
            self.assert_pre_read_error([*pair, '--expect-input-sha256', value], '64 lowercase hexadecimal')
        self.assert_pre_read_error([*pair, '--expect-input-sha256'], 'expected one argument')
        for option in ('--expect', '--expect-input', '--expect-input-sha25'):
            for suffix in ([], ['=' + '0' * 64]):
                tokens = [option + suffix[0]] if suffix else [option, '0' * 64]
                self.assert_pre_read_error([*pair, *tokens], 'unrecognized arguments')
        gate = ['--expect-input-sha256', '0' * 64]
        for options, diagnostic in (
                (['--dimensions-file', self.config], 'requires --dimensions-side'),
                (['--dimensions-side', 'candidate'], 'requires --dimensions-file'),
                ([*pair, '--dimension', 'span:x=x'], 'cannot combine'),
                (['--dimension-provenance'], 'requires --dimensions-file'),
                (['--expect-dimension-sha256', '0' * 64], 'requires --dimensions-file'),
                (['--max-input-bytes', '0'], 'expected positive integer'),
                (['--input-format', 'unknown'], 'invalid choice'),
                (['--input', '-'], 'exactly one positional input')):
            self.assert_pre_read_error([*gate, *options], diagnostic)

    def test_mismatch_preserves_all_files_force_default_report_and_dry_run(self):
        original = pairs.CAPTURE.read_bytes()
        # Semantically identical JSON with different exact bytes.
        raw = json.dumps(json.loads(original), ensure_ascii=False, separators=(',', ':')).encode() + b' \r\n'
        capture = self.directory / 'capture.json'
        capture.write_bytes(raw)
        self.assertNotEqual(self.digest(raw), self.digest(original))
        self.output.write_bytes(b'old metrics')
        self.report.write_bytes(b'old manifest')
        Path(str(self.output) + '.report.json').write_bytes(b'old default manifest')
        before = {p: p.read_bytes() for p in self.directory.iterdir()}
        for options in ([], self.flags('candidate'), self.pair_options('baseline'), self.pair_options('candidate', True)):
            for dry in (False, True):
                for default_report in (False, True):
                    proc = self.run_cli('--force', *options, *(['--dry-run'] if dry else []),
                                        source=capture, expected=self.digest(original), default_report=default_report)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertEqual(proc.stdout, b'')
                    self.assertIn(f'expected {self.digest(original)}, got {self.digest(raw)}'.encode(), proc.stderr)
                    self.assertEqual({p: p.read_bytes() for p in self.directory.iterdir()}, before)

    def test_jsonl_stdin_mismatch_consumes_eof_before_aggregation_or_publication(self):
        raw = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes())))
        for named in (False, True):
            stream = io.BytesIO(raw)
            stdin = Mock(buffer=stream)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(cli.sys, 'stdin', stdin), \
                 patch.object(cli, 'convert', side_effect=AssertionError('aggregation started')), \
                 patch.object(cli, 'write_files', side_effect=AssertionError('publication started')), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main([*(['--input', '-'] if named else ['-']), '--input-format', 'jsonl',
                                 '--output', str(self.output), '--expect-input-sha256', '0' * 64])
            self.assertEqual(code, 2, stderr.getvalue())
            self.assertEqual(stdout.getvalue(), '')
            self.assertIn(f'expected {"0" * 64}, got {self.digest(raw)}', stderr.getvalue())
            self.assertEqual(stream.tell(), len(raw))
            self.assertFalse(stream.closed)
            self.assertFalse(self.output.exists())
            self.assertFalse(self.report.exists())

    def test_single_short_bounded_read_uses_loaded_bytes_without_reopening(self):
        capture = self.directory / 'capture.json'
        raw = pairs.CAPTURE.read_bytes()
        capture.write_bytes(raw)
        for matches in (True, False):
            capture.write_bytes(raw)
            stream, sizes = io.BytesIO(raw), []
            wrapped = MagicMock()
            def short_read(size):
                sizes.append(size)
                chunk = stream.read(min(size, 7))
                if not chunk:
                    capture.write_bytes(b'later invalid bytes')
                return chunk
            wrapped.__enter__.return_value.read.side_effect = short_read
            real_open = Path.open
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(Path, 'open', autospec=True, side_effect=lambda path, *a, **kw:
                              wrapped if path == capture and a == ('rb',) else real_open(path, *a, **kw)) as opening, \
                 patch.object(cli, 'read_input', wraps=cli.read_input) as read, \
                 patch.object(cli, 'convert', wraps=cli.convert) as convert, \
                 patch.object(cli, 'write_files', side_effect=AssertionError('publication attempted')), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main([str(capture), '--output', str(self.output), '--dry-run',
                                 '--max-input-bytes', str(len(raw)), '--expect-input-sha256',
                                 self.digest(raw) if matches else '0' * 64])
            self.assertEqual(code, 0 if matches else 2, stderr.getvalue())
            read.assert_called_once_with(capture, len(raw))
            self.assertEqual(sum(c.args == (capture, 'rb') for c in opening.call_args_list), 1)
            self.assertGreater(len(sizes), 2)
            self.assertTrue(all(0 < n <= 65536 for n in sizes))
            if matches:
                self.assertEqual(convert.call_args.args[0], raw)
                self.assertEqual(convert.call_count, 1)
                self.assertEqual(json.loads(stdout.getvalue())['input_sha256'], self.digest(raw))
            else:
                convert.assert_not_called()
                self.assertEqual(stdout.getvalue(), '')

    def test_pair_validation_and_pair_gate_still_precede_capture_gate(self):
        original = self.config.read_bytes()
        for invalid in (False, True):
            self.config.write_bytes(b'{"schema_version":1,"baseline":[],"candidate":[{}]}' if invalid else original)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(cli, 'read_input', side_effect=AssertionError('capture read')), \
                 patch.object(cli, 'convert', side_effect=AssertionError('aggregation started')), \
                 patch.object(cli, 'write_files', side_effect=AssertionError('publication started')), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main([str(pairs.CAPTURE), '--output', str(self.output),
                                 '--dimensions-file', str(self.config), '--dimensions-side', 'baseline',
                                 '--expect-dimension-sha256', '0' * 64, '--expect-input-sha256', '0' * 64])
            self.assertEqual(code, 2)
            self.assertEqual(stdout.getvalue(), '')
            self.assertIn('candidate' if invalid else 'dimension configuration SHA-256 mismatch', stderr.getvalue())
            self.assertNotIn('input SHA-256 mismatch', stderr.getvalue())

    def test_matching_rejections_retain_reports_metrics_and_limits(self):
        raw = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes())))
        cases = [(raw, ['--input-format', 'jsonl', '--max-line-bytes', '10'], 'line byte cap'),
                 (raw, ['--input-format', 'jsonl', '--max-spans', '1'], 'span cap'),
                 (raw, ['--input-format', 'jsonl', '--max-series', '8'], 'series cap'),
                 (raw + b'{\n', ['--input-format', 'jsonl'], 'line'),
                 (b'{', [], 'JSON'), (b'\xff', [], 'utf-8'),
                 (b'', ['--input-format', 'jsonl'], 'empty JSONL')]
        capture = self.directory / 'capture'
        self.output.write_bytes(b'old metrics')
        for data, options, diagnostic in cases:
            capture.write_bytes(data)
            for source in (capture, '-') if '--input-format' in options else (capture,):
                with self.subTest(options=options, source=source, diagnostic=diagnostic):
                    kw = dict(source=source, data=data if source == '-' else None)
                    opts = ['--force', *self.pair_options('candidate', True), *options]
                    default = self.run_cli(*opts, **kw)
                    self.assertEqual(default.returncode, 1, default.stderr)
                    manifest = self.report.read_bytes()
                    matched = self.run_cli(*opts, **kw, expected=self.digest(data))
                    self.assertEqual(matched.returncode, 1, matched.stderr)
                    self.assertEqual(matched.stdout, default.stdout)
                    self.assertEqual(matched.stderr, default.stderr)
                    self.assertEqual(self.report.read_bytes(), manifest)
                    report = json.loads(matched.stdout)
                    self.assertEqual(report['input_sha256'], self.digest(data))
                    self.assertTrue(any(diagnostic in d for d in report['diagnostics']), report['diagnostics'])
                    self.assertFalse(report['metrics_emitted'])
                    self.assertEqual(self.output.read_bytes(), b'old metrics')
            # Mismatch wins even when the fully loaded capture cannot parse.
            mismatch = self.run_cli('--force', *options, source=capture, expected='0' * 64)
            self.assertEqual(mismatch.returncode, 2, mismatch.stderr)
            self.assertEqual(mismatch.stdout, b'')
            self.assertEqual(self.report.read_bytes(), manifest)

    def test_exact_caps_match_and_overflow_never_hashes_or_compares_prefix(self):
        raw = (pairs.ROOT / 'examples/spanmetrics/traces.jsonl').read_bytes()
        for source in ('-', self.directory / 'capture.jsonl'):
            if source != '-':
                source.write_bytes(raw)
            opts = ['--input-format', 'jsonl', '--max-input-bytes', str(len(raw)),
                    '--max-line-bytes', str(max(map(len, raw.splitlines()))),
                    '--max-spans', '2', '--max-series', '16', '--dry-run']
            proc = self.run_cli(*opts, source=source, data=raw if source == '-' else None, expected=self.digest(raw))
            self.assertEqual(proc.returncode, 0, proc.stderr)
        # The expected value deliberately matches the returned cap+1 prefix.
        cap = 10
        stdout, stderr = io.StringIO(), io.StringIO()
        stdin = Mock(buffer=io.BytesIO(raw))
        expected = self.digest(raw[:cap + 1])
        with patch.object(cli.sys, 'stdin', stdin), \
             patch.object(cli.hashlib, 'sha256', side_effect=AssertionError('prefix hashed')), \
             patch.object(cli, 'convert', side_effect=AssertionError('prefix aggregated')), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(['-', '--input-format', 'jsonl', '--output', str(self.output),
                             '--max-input-bytes', str(cap), '--expect-input-sha256', expected])
        self.assertEqual(code, 1, stderr.getvalue())
        report = json.loads(stdout.getvalue())
        self.assertEqual(stdin.buffer.tell(), cap + 1)
        self.assertFalse(report['record_counts_complete'])
        self.assertNotIn('input_sha256', report)
        self.assertIn('not fully read', report['diagnostics'][0])
        self.assertNotIn('SHA-256 mismatch', stderr.getvalue())
        self.assertFalse(self.output.exists())
        self.assertEqual(Path(str(self.output) + '.report.json').read_text(), stdout.getvalue())

    def test_overflow_retains_existing_file_and_stdin_publication_parity(self):
        raw = (pairs.ROOT / 'examples/spanmetrics/traces.jsonl').read_bytes()
        capture = self.directory / 'capture.jsonl'
        capture.write_bytes(raw)
        self.output.write_bytes(b'old metrics')
        for source in (capture, '-'):
            kw = dict(source=source, data=raw if source == '-' else None)
            opts = ['--force', '--input-format', 'jsonl', '--max-input-bytes', '10', *self.pair_options('baseline', True)]
            default = self.run_cli(*opts, **kw)
            self.assertEqual(default.returncode, 1, default.stderr)
            for expected in ('0' * 64, self.digest(raw[:11]), self.digest(raw)):
                proc = self.run_cli(*opts, **kw, expected=expected)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(proc.stdout, default.stdout)
                self.assertEqual(proc.stderr, default.stderr)
                self.assertEqual(self.output.read_bytes(), b'old metrics')
                self.assertEqual(self.report.read_bytes(), proc.stdout)

    def test_alias_preflight_and_read_errors_preserve_files_before_gate(self):
        raw = pairs.CAPTURE.read_bytes()
        capture = self.directory / 'capture.json'
        capture.write_bytes(raw)
        for protected in (capture, self.config):
            for alias_kind in ('same', 'symlink', 'hardlink'):
                alias = self.directory / 'alias'
                alias.unlink(missing_ok=True)
                if alias_kind == 'symlink':
                    alias.symlink_to(protected)
                elif alias_kind == 'hardlink':
                    os.link(protected, alias)
                else:
                    alias = protected
                for target in ('--output', '--report'):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with patch.object(cli, 'read_input', side_effect=AssertionError('capture read')), \
                         redirect_stdout(stdout), redirect_stderr(stderr):
                        code = cli.main([str(capture), '--output', str(self.output), '--report', str(self.report),
                                         '--force', *map(str, self.pair_options()), target, str(alias),
                                         '--expect-input-sha256', self.digest(raw)])
                    self.assertEqual(code, 2, stderr.getvalue())
                    self.assertEqual(stdout.getvalue(), '')
                self.assertEqual(capture.read_bytes(), raw)
        for source in (self.directory / 'missing', self.directory):
            proc = self.run_cli('--dry-run', source=source, expected='0' * 64)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(proc.stdout, b'')
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli, 'read_input', side_effect=OSError('read failure')), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main([str(capture), '--output', str(self.output), '--expect-input-sha256', self.digest(raw)])
        self.assertEqual(code, 2)
        self.assertIn('read failure', stderr.getvalue())
        self.assertEqual(stdout.getvalue(), '')
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_other_commands_do_not_accept_input_gate_and_legacy_options_remain(self):
        for command in ('validate-dimensions',):
            proc = subprocess.run([*pairs.CLI, command, '--dimensions-file', str(self.config),
                                   '--expect-input-sha256', self.digest(pairs.CAPTURE.read_bytes())],
                                  capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, b'')
            self.assertIn(b'unrecognized arguments', proc.stderr)
        proc = self.run_cli('--out=' + str(self.output), '--dry', '--expect-input-sha256=' + self.digest(pairs.CAPTURE.read_bytes()))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)['dry_run'])


    def test_jsonl_formatting_mismatch_file_and_stdin_with_named_input(self):
        original = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes())))
        raw = original.replace(b'\n', b'\r\n')
        capture = self.directory / 'capture.jsonl'
        capture.write_bytes(raw)
        self.output.write_bytes(b'old metrics')
        self.report.write_bytes(b'old manifest')
        before = {p: p.read_bytes() for p in self.directory.iterdir()}
        for source in (capture, '-'):
            for named in (False, True):
                proc = self.run_cli('--force', '--input-format', 'jsonl', *self.flags('baseline'),
                                    source=source, data=raw if source == '-' else None,
                                    expected=self.digest(original), named=named)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, b'')
                self.assertIn(f'expected {self.digest(original)}, got {self.digest(raw)}'.encode(), proc.stderr)
                self.assertEqual({p: p.read_bytes() for p in self.directory.iterdir()}, before)

    def test_redirected_stdin_aliases_reject_before_gate_without_mutation(self):
        raw = (pairs.ROOT / 'examples/spanmetrics/traces.jsonl').read_bytes()
        capture = self.directory / 'capture.jsonl'
        capture.write_bytes(raw)
        for target in ('--output', '--report'):
            for kind in ('same', 'hardlink', 'symlink', 'default-report'):
                alias = self.directory / 'redirected-alias'
                alias.unlink(missing_ok=True)
                if kind == 'same':
                    alias = capture
                elif kind == 'hardlink':
                    os.link(capture, alias)
                elif kind == 'symlink':
                    alias.symlink_to(capture)
                else:
                    alias = Path(str(self.output) + '.report.json')
                    alias.unlink(missing_ok=True)
                    os.link(capture, alias)
                opts = [target, str(alias)] if kind != 'default-report' else []
                with capture.open('rb') as stream:
                    proc = subprocess.run([*pairs.CLI, '-', '--input-format', 'jsonl',
                                           '--output', str(self.output), '--force', *opts,
                                           '--expect-input-sha256', '0' * 64], stdin=stream,
                                          capture_output=True, timeout=60)
                    self.assertEqual(stream.tell(), 0)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, b'')
                self.assertIn(b'must not overwrite input', proc.stderr)
                self.assertEqual(capture.read_bytes(), raw)
                if kind == 'default-report':
                    alias.unlink()


if __name__ == '__main__':
    unittest.main()
