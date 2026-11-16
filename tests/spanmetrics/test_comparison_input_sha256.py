# SPDX-License-Identifier: Apache-2.0
"""Comparison gates exact fully consumed capture bytes before either aggregation."""
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
from . import test_dimension_config as pairs
from .test_jsonl import jsonl, split_requests


class ComparisonInputSha256Tests(unittest.TestCase):
    setUp = pairs.DimensionConfigTests.setUp
    save = pairs.DimensionConfigTests.save

    def digest(self, data):
        return hashlib.sha256(data).hexdigest()

    def flags(self):
        return [token for side in ('baseline', 'candidate') for d in pairs.PAIR[side]
                for token in (f'--{side}-dimension', f'{d["source"]}:{d["key"]}={d["label"]}')]

    def pair_options(self, provenance=False):
        return ['--dimensions-file', self.config,
                '--expect-dimension-sha256', self.digest(self.config.read_bytes()),
                *(['--dimension-provenance'] if provenance else [])]

    def run_cli(self, *options, source=pairs.CAPTURE, data=None, expected=None, named=False):
        return subprocess.run(
            [*pairs.CLI, 'compare', *(['--input', str(source)] if named else [str(source)]),
             '--output', str(self.output),
             *([] if expected is None else ['--expect-input-sha256', expected]), *map(str, options)],
            input=data, capture_output=True, timeout=60, cwd=self.directory)

    def assert_suppressed(self, report):
        self.assertFalse(report['comparison_complete'])
        self.assertFalse(report['metrics_emitted'])
        self.assertIsNone(report['unique_records'])
        self.assertIsNone(report['series_delta'])
        self.assertEqual(report['partitions'], [])
        for side in report['configurations'].values():
            self.assertIsNone(side['groups'])
            self.assertIsNone(side['series'])

    def test_matching_json_retains_exact_report_and_publication(self):
        raw = pairs.CAPTURE.read_bytes()
        for options in ([], self.flags(), self.pair_options(), self.pair_options(True)):
            for dry in (False, True):
                with self.subTest(options=options, dry=dry):
                    opts = ['--force', *options, *(['--dry-run'] if dry else [])]
                    default = self.run_cli(*opts)
                    self.assertEqual(default.returncode, 0, default.stderr)
                    before = {p: p.read_bytes() for p in self.directory.iterdir()}
                    matched = self.run_cli(*opts, expected=self.digest(raw))
                    self.assertEqual(matched.returncode, 0, matched.stderr)
                    self.assertEqual(matched.stderr, b'')
                    self.assertEqual(matched.stdout, default.stdout)
                    self.assertEqual({p: p.read_bytes() for p in self.directory.iterdir()}, before)
                    report = json.loads(matched.stdout)
                    self.assertEqual(report['input_sha256'], self.digest(raw))
                    self.assertNotIn('expect_input_sha256', report)
                    if options:
                        self.assertEqual((report['unique_records'], report['series_delta']), (6, 24))
                        self.assertEqual([report['configurations'][s]['series']
                                          for s in ('baseline', 'candidate')], [16, 40])
                    if '--dimension-provenance' in options:
                        self.assertEqual(report['dimension_pair_provenance'],
                                         {'schema_version': 1, 'sha256': self.digest(self.config.read_bytes())})

    def test_jsonl_file_stdin_both_input_forms_and_line_endings(self):
        data = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes())))
        capture = self.directory / 'capture.jsonl'
        for ending in (b'\n', b'\r\n', b''):
            raw = data.rstrip(b'\n').replace(b'\n', b'\r\n' if ending == b'\r\n' else b'\n') + ending
            capture.write_bytes(raw)
            for source in (capture, '-'):
                for named in (False, True):
                    with self.subTest(ending=ending, source=source, named=named):
                        opts = ['--input-format', 'jsonl', '--force', *self.pair_options(True)]
                        kw = dict(source=source, data=raw if source == '-' else None, named=named)
                        default = self.run_cli(*opts, **kw)
                        self.assertEqual(default.returncode, 0, default.stderr)
                        matched = self.run_cli(*opts, **kw, expected=self.digest(raw))
                        self.assertEqual(matched.returncode, 0, matched.stderr)
                        self.assertEqual(matched.stdout, default.stdout)
                        self.assertEqual(self.output.read_bytes(), matched.stdout)
                        self.assertEqual(json.loads(matched.stdout)['input_sha256'], self.digest(raw))
        matched = self.run_cli('--dry-run', '--expect-input-sha256=' + self.digest(pairs.CAPTURE.read_bytes()),
                               named=True)
        self.assertEqual(matched.returncode, 0, matched.stderr)

    def assert_pre_read_error(self, options, diagnostic):
        stdin = Mock()
        stdin.buffer.read.side_effect = AssertionError('stdin consumed')
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli.sys, 'stdin', stdin), \
             patch.object(cli, 'read_dimension_pair', side_effect=AssertionError('pair read')), \
             patch.object(cli, 'read_input', side_effect=AssertionError('capture read')), \
             patch.object(cli, 'compare', side_effect=AssertionError('comparison started')), \
             patch.object(cli, 'write_files', side_effect=AssertionError('publication started')), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(['compare', '-', '--input-format', 'jsonl', '--output', str(self.output),
                                 '--force', *map(str, options)])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 2, stderr.getvalue())
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn(diagnostic, stderr.getvalue())
        stdin.buffer.read.assert_not_called()

    def test_invalid_incomplete_abbreviated_and_incompatible_before_reads(self):
        pair = ['--dimensions-file', self.config]
        for value in ('', 'a' * 63, 'a' * 65, 'A' * 64, 'g' * 64, '0x' + 'a' * 62,
                      ' ' + 'a' * 64, 'a' * 64 + ' ', 'a' * 64 + '\n', '０' * 64, 'é' * 64):
            self.assert_pre_read_error([*pair, '--expect-input-sha256', value], '64 lowercase hexadecimal')
        self.assert_pre_read_error([*pair, '--expect-input-sha256'], 'expected one argument')
        for option in ('--expect', '--expect-input', '--expect-input-sha25'):
            self.assert_pre_read_error([*pair, option, '0' * 64], 'unrecognized arguments')
            self.assert_pre_read_error([*pair, option + '=' + '0' * 64], 'unrecognized arguments')
        gate = ['--expect-input-sha256', '0' * 64]
        for options, diagnostic in (
                ([*pair, '--baseline-dimension', 'span:x=x'], 'cannot combine'),
                ([*pair, '--candidate-dimension', 'span:x=x'], 'cannot combine'),
                (['--dimension-provenance'], 'requires --dimensions-file'),
                (['--expect-dimension-sha256', '0' * 64], 'requires --dimensions-file'),
                (['--dimensions-side', 'baseline'], 'unrecognized arguments'),
                (['--max-input-bytes', '0'], 'expected positive integer'),
                (['--input-format', 'unknown'], 'invalid choice'),
                (['--input', '-'], 'exactly one positional input')):
            self.assert_pre_read_error([*gate, *options], diagnostic)

    def test_formatting_sensitive_mismatch_preserves_files_force_and_dry_run(self):
        original = pairs.CAPTURE.read_bytes()
        raw = json.dumps(json.loads(original), ensure_ascii=False, separators=(',', ':')).encode() + b' \r\n'
        capture = self.directory / 'capture.json'
        capture.write_bytes(raw)
        self.assertNotEqual(self.digest(raw), self.digest(original))
        self.output.write_bytes(b'reviewed comparison')
        before = {p: p.read_bytes() for p in self.directory.iterdir()}
        for options in ([], self.flags(), self.pair_options(), self.pair_options(True)):
            for dry in (False, True):
                proc = self.run_cli('--force', *options, *(['--dry-run'] if dry else []),
                                    source=capture, expected=self.digest(original))
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, b'')
                self.assertIn(f'expected {self.digest(original)}, got {self.digest(raw)}'.encode(), proc.stderr)
                self.assertEqual({p: p.read_bytes() for p in self.directory.iterdir()}, before)

    def test_jsonl_formatting_mismatch_file_stdin_and_named_input(self):
        original = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes())))
        raw = original.replace(b'\n', b'\r\n')
        capture = self.directory / 'capture.jsonl'
        capture.write_bytes(raw)
        self.output.write_bytes(b'reviewed comparison')
        before = {p: p.read_bytes() for p in self.directory.iterdir()}
        for source in (capture, '-'):
            for named in (False, True):
                proc = self.run_cli('--force', '--input-format', 'jsonl', *self.flags(),
                                    source=source, data=raw if source == '-' else None,
                                    expected=self.digest(original), named=named)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, b'')
                self.assertIn(f'expected {self.digest(original)}, got {self.digest(raw)}'.encode(), proc.stderr)
                self.assertEqual({p: p.read_bytes() for p in self.directory.iterdir()}, before)

    def test_mismatch_fully_consumes_stdin_before_comparison_or_publication(self):
        raw = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes())))
        for named in (False, True):
            stream = io.BytesIO(raw)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(cli.sys, 'stdin', Mock(buffer=stream)), \
                 patch.object(cli, 'compare', side_effect=AssertionError('comparison started')), \
                 patch.object(cli, 'write_files', side_effect=AssertionError('publication started')), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(['compare', *(['--input', '-'] if named else ['-']),
                                 '--input-format', 'jsonl', '--output', str(self.output),
                                 '--expect-input-sha256', '0' * 64])
            self.assertEqual(code, 2, stderr.getvalue())
            self.assertEqual(stdout.getvalue(), '')
            self.assertIn(f'expected {"0" * 64}, got {self.digest(raw)}', stderr.getvalue())
            self.assertEqual(stream.tell(), len(raw))
            self.assertFalse(stream.closed)
            self.assertFalse(self.output.exists())

    def test_single_bounded_short_read_checks_loaded_bytes_without_reopening(self):
        capture = self.directory / 'capture.json'
        raw = pairs.CAPTURE.read_bytes()
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
                 patch.object(cli, 'compare', wraps=cli.compare) as compare, \
                 patch.object(cli, 'write_files', side_effect=AssertionError('publication attempted')), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(['compare', str(capture), '--output', str(self.output), '--dry-run',
                                 '--max-input-bytes', str(len(raw)), '--expect-input-sha256',
                                 self.digest(raw) if matches else '0' * 64])
            self.assertEqual(code, 0 if matches else 2, stderr.getvalue())
            read.assert_called_once_with(capture, len(raw))
            self.assertEqual(sum(c.args == (capture, 'rb') for c in opening.call_args_list), 1)
            self.assertGreater(len(sizes), 2)
            self.assertTrue(all(0 < n <= 65536 for n in sizes))
            if matches:
                self.assertEqual(compare.call_args.args[0], raw)
                self.assertEqual(compare.call_count, 1)
                self.assertEqual(json.loads(stdout.getvalue())['input_sha256'], self.digest(raw))
            else:
                compare.assert_not_called()
                self.assertEqual(stdout.getvalue(), '')

    def test_complete_pair_validation_and_gate_precede_capture_load(self):
        original = self.config.read_bytes()
        cases = [(original, 'dimension configuration SHA-256 mismatch')]
        for side in ('baseline', 'candidate'):
            cases.append((json.dumps(dict(pairs.PAIR, **{side: [{}]})).encode(), side))
        for raw, diagnostic in cases:
            self.config.write_bytes(raw)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(cli, 'read_input', side_effect=AssertionError('capture read')), \
                 patch.object(cli, 'compare', side_effect=AssertionError('comparison started')), \
                 patch.object(cli, 'write_files', side_effect=AssertionError('publication started')), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(['compare', '-', '--input-format', 'jsonl', '--output', str(self.output),
                                 '--dimensions-file', str(self.config), '--expect-dimension-sha256', '0' * 64,
                                 '--expect-input-sha256', '0' * 64])
            self.assertEqual(code, 2)
            self.assertEqual(stdout.getvalue(), '')
            self.assertIn(diagnostic, stderr.getvalue())
            self.assertNotIn('input SHA-256 mismatch', stderr.getvalue())

    def test_matching_rejections_retain_reports_and_suppress_counts(self):
        raw = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes())))
        cases = [(raw, ['--input-format', 'jsonl', '--max-line-bytes', '10'], 'line byte cap'),
                 (raw, ['--input-format', 'jsonl', '--max-spans', '1'], 'span cap'),
                 (raw, ['--input-format', 'jsonl', '--max-series', '16'], 'series cap'),
                 (raw + b'{\n', ['--input-format', 'jsonl'], 'line'),
                 (b'{', [], 'JSON'), (b'\xff', [], 'utf-8'),
                 (b'', ['--input-format', 'jsonl'], 'empty JSONL')]
        capture = self.directory / 'capture'
        for data, options, diagnostic in cases:
            capture.write_bytes(data)
            for source in (capture, '-') if '--input-format' in options else (capture,):
                with self.subTest(options=options, source=source):
                    kw = dict(source=source, data=data if source == '-' else None)
                    opts = ['--force', *self.pair_options(True), *options]
                    default = self.run_cli(*opts, **kw)
                    self.assertEqual(default.returncode, 1, default.stderr)
                    matched = self.run_cli(*opts, **kw, expected=self.digest(data))
                    self.assertEqual(matched.returncode, 1, matched.stderr)
                    self.assertEqual(matched.stdout, default.stdout)
                    self.assertEqual(matched.stderr, default.stderr)
                    self.assertEqual(self.output.read_bytes(), matched.stdout)
                    report = json.loads(matched.stdout)
                    self.assertEqual(report['input_sha256'], self.digest(data))
                    self.assert_suppressed(report)
                    self.assertTrue(any(diagnostic in d for c in report['configurations'].values()
                                        for d in c['validation']['diagnostics']))
                    saved = self.output.read_bytes()
                    dry = self.run_cli(*opts, '--dry-run', **kw, expected=self.digest(data))
                    self.assertEqual(dry.returncode, 1, dry.stderr)
                    self.assertEqual(self.output.read_bytes(), saved)
            # Even malformed fully loaded input must mismatch before parsing/publication.
            mismatch = self.run_cli('--force', *options, source=capture, expected='0' * 64)
            self.assertEqual(mismatch.returncode, 2, mismatch.stderr)
            self.assertEqual(mismatch.stdout, b'')
            self.assertEqual(self.output.read_bytes(), saved)

    def test_exact_limits_and_overflow_never_hashes_or_compares_prefix(self):
        raw = (pairs.ROOT / 'examples/spanmetrics/traces.jsonl').read_bytes()
        capture = self.directory / 'capture.jsonl'
        capture.write_bytes(raw)
        for source in (capture, '-'):
            proc = self.run_cli('--input-format', 'jsonl', '--max-input-bytes', len(raw),
                                '--max-line-bytes', max(map(len, raw.splitlines())),
                                '--max-spans', '2', '--max-series', '16', '--dry-run',
                                source=source, data=raw if source == '-' else None, expected=self.digest(raw))
            self.assertEqual(proc.returncode, 0, proc.stderr)
        cap = 10
        expected = self.digest(raw[:cap + 1])
        stdout, stderr = io.StringIO(), io.StringIO()
        stdin = Mock(buffer=io.BytesIO(raw))
        with patch.object(cli.sys, 'stdin', stdin), \
             patch.object(cli.hashlib, 'sha256', side_effect=AssertionError('prefix hashed')), \
             patch.object(cli, 'compare', side_effect=AssertionError('prefix compared')), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(['compare', '-', '--input-format', 'jsonl', '--output', str(self.output),
                             '--max-input-bytes', str(cap), '--expect-input-sha256', expected])
        self.assertEqual(code, 1, stderr.getvalue())
        report = json.loads(stdout.getvalue())
        self.assertEqual(stdin.buffer.tell(), cap + 1)
        self.assertNotIn('input_sha256', report)
        self.assert_suppressed(report)
        for side in report['configurations'].values():
            self.assertFalse(side['validation']['record_counts_complete'])
            self.assertIn('not fully read', side['validation']['diagnostics'][0])
        self.assertNotIn('SHA-256 mismatch', stderr.getvalue())
        self.assertEqual(self.output.read_text(), stdout.getvalue())

    def test_overflow_publication_matches_legacy_file_and_stdin(self):
        raw = (pairs.ROOT / 'examples/spanmetrics/traces.jsonl').read_bytes()
        capture = self.directory / 'capture.jsonl'
        capture.write_bytes(raw)
        for source in (capture, '-'):
            kw = dict(source=source, data=raw if source == '-' else None)
            opts = ['--force', '--input-format', 'jsonl', '--max-input-bytes', '10', *self.pair_options(True)]
            default = self.run_cli(*opts, **kw)
            self.assertEqual(default.returncode, 1, default.stderr)
            for expected in ('0' * 64, self.digest(raw[:11]), self.digest(raw)):
                proc = self.run_cli(*opts, **kw, expected=expected)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertEqual(proc.stdout, default.stdout)
                self.assertEqual(proc.stderr, default.stderr)
                self.assertEqual(self.output.read_bytes(), proc.stdout)

    def test_aliases_existing_output_and_read_errors_preserve_files(self):
        raw = pairs.CAPTURE.read_bytes()
        capture = self.directory / 'capture.json'
        capture.write_bytes(raw)
        pair_bytes = self.config.read_bytes()
        for protected in (capture, self.config):
            for kind in ('same', 'symlink', 'hardlink'):
                alias = self.directory / 'alias'
                alias.unlink(missing_ok=True)
                if kind == 'symlink':
                    alias.symlink_to(protected)
                elif kind == 'hardlink':
                    os.link(protected, alias)
                target = protected if kind == 'same' else alias
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(cli, 'read_input', side_effect=AssertionError('capture read')), \
                     redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli.main(['compare', str(capture), '--output', str(target), '--force',
                                     *map(str, self.pair_options()), '--expect-input-sha256', self.digest(raw)])
                self.assertEqual(code, 2, stderr.getvalue())
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn('refusing symlink destination' if protected == capture and kind == 'symlink'
                              else 'must not overwrite input', stderr.getvalue())
                self.assertEqual(capture.read_bytes(), raw)
                self.assertEqual(self.config.read_bytes(), pair_bytes)
        self.output.write_bytes(b'reviewed comparison')
        proc = self.run_cli(expected=self.digest(raw))
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, b'')
        self.assertIn(b'destination exists', proc.stderr)
        self.assertEqual(self.output.read_bytes(), b'reviewed comparison')
        for source in (self.directory / 'missing', self.directory):
            proc = self.run_cli('--dry-run', source=source, expected='0' * 64)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(proc.stdout, b'')
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli, 'read_input', side_effect=OSError('read failure')), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(['compare', str(capture), '--output', str(self.output), '--force',
                             '--expect-input-sha256', self.digest(raw)])
        self.assertEqual(code, 2)
        self.assertIn('read failure', stderr.getvalue())
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(self.output.read_bytes(), b'reviewed comparison')

    def test_redirected_stdin_alias_preflight_preserves_capture(self):
        raw = (pairs.ROOT / 'examples/spanmetrics/traces.jsonl').read_bytes()
        capture = self.directory / 'capture.jsonl'
        capture.write_bytes(raw)
        for kind in ('same', 'hardlink', 'symlink'):
            alias = self.directory / 'redirected-alias'
            alias.unlink(missing_ok=True)
            if kind == 'hardlink':
                os.link(capture, alias)
            elif kind == 'symlink':
                alias.symlink_to(capture)
            target = capture if kind == 'same' else alias
            with capture.open('rb') as stream:
                proc = subprocess.run([*pairs.CLI, 'compare', '-', '--input-format', 'jsonl',
                                       '--output', str(target), '--force', '--expect-input-sha256', '0' * 64],
                                      stdin=stream, capture_output=True, timeout=60)
                self.assertEqual(stream.tell(), 0)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(proc.stdout, b'')
            self.assertIn(b'must not overwrite input', proc.stderr)
            self.assertEqual(capture.read_bytes(), raw)

    def test_legacy_abbreviations_and_inspection_exclusion(self):
        proc = self.run_cli('--out=' + str(self.output), '--dry',
                            '--expect-input-sha256=' + self.digest(pairs.CAPTURE.read_bytes()))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)['dry_run'])
        proc = subprocess.run([*pairs.CLI, 'validate-dimensions', '--dimensions-file', str(self.config),
                               '--expect-input-sha256', self.digest(pairs.CAPTURE.read_bytes())],
                              capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b'')
        self.assertIn(b'unrecognized arguments', proc.stderr)


if __name__ == '__main__':
    unittest.main()
