# SPDX-License-Identifier: Apache-2.0
"""Exact complete-pair gate before selected-side conversion capture consumption."""
from contextlib import redirect_stderr, redirect_stdout
import copy
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
from . import test_conversion_dimensions as conversion
from .test_jsonl import jsonl, split_requests


class ConversionSha256Tests(unittest.TestCase):
    setUp = conversion.ConversionDimensionTests.setUp
    save = pairs.DimensionConfigTests.save
    flags = conversion.ConversionDimensionTests.flags
    assert_metrics = conversion.ConversionDimensionTests.assert_metrics

    def digest(self):
        return hashlib.sha256(self.config.read_bytes()).hexdigest()

    def run_cli(self, *options, source=pairs.CAPTURE, side='candidate', expected=None, data=None):
        return conversion.ConversionDimensionTests.run_cli(
            self, '--expect-dimension-sha256', self.digest() if expected is None else expected,
            *options, source=source, side=side, data=data)

    def gate(self, side='candidate', expected=None):
        return ['--dimensions-file', self.config, '--dimensions-side', side,
                '--expect-dimension-sha256', self.digest() if expected is None else expected]

    def test_exact_match_preserves_metrics_manifest_and_opt_in_publication(self):
        original = self.config.read_bytes()
        for side in ('baseline', 'candidate'):
            for opted in (False, True):
                options = ['--force', *(['--dimension-provenance'] if opted else [])]
                default = conversion.ConversionDimensionTests.run_cli(self, *options, side=side)
                self.assertEqual(default.returncode, 0, default.stderr)
                metrics, manifest = self.output.read_bytes(), self.report.read_bytes()
                matched = self.run_cli(*options, side=side)
                self.assertEqual(matched.returncode, 0, matched.stderr)
                self.assertEqual(matched.stderr, '')
                self.assertEqual(matched.stdout, default.stdout)
                self.assertEqual(self.output.read_bytes(), metrics)
                self.assertEqual(self.report.read_bytes(), manifest)
                self.assertEqual(matched.stdout.encode(), manifest)
                self.assert_metrics(side)
                report = json.loads(matched.stdout)
                if opted:
                    self.assertEqual(report['dimension_pair_provenance'],
                                     {'schema_version': 1, 'selected_side': side, 'sha256': self.digest()})
                else:
                    self.assertNotIn('dimension_pair_provenance', report)
                    flags = conversion.ConversionDimensionTests.run_cli(self, '--force', *self.flags(side), file=False)
                    self.assertEqual(flags.returncode, 0, flags.stderr)
                    self.assertEqual(flags.stdout, matched.stdout)
                    self.assertEqual(self.output.read_bytes(), metrics)
        self.assertEqual(self.config.read_bytes(), original)

    def assert_pre_capture_error(self, options, diagnostic, before_pair=False, source=None):
        stdin = Mock()
        stdin.buffer.read.side_effect = AssertionError('stdin consumed')
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli.sys, 'stdin', stdin), \
             patch.object(cli, 'read_dimension_pair', wraps=cli.read_dimension_pair) as loader, \
             patch.object(cli, 'read_input', side_effect=AssertionError('capture consumed')), \
             patch.object(cli, 'convert', side_effect=AssertionError('conversion started')), \
             patch.object(cli, 'write_files', side_effect=AssertionError('publication attempted')), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main([*(['-', '--input-format', 'jsonl'] if source is None else [str(source)]),
                                 '--output', str(self.output), '--report', str(self.report),
                                 '--force', *map(str, options)])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 2, stderr.getvalue())
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn(diagnostic, stderr.getvalue())
        self.assertNotIn('Traceback', stderr.getvalue())
        stdin.buffer.read.assert_not_called()
        if before_pair:
            loader.assert_not_called()
        return stderr.getvalue()

    def test_invalid_missing_mixed_and_abbreviated_options_before_pair_read(self):
        gate = ['--expect-dimension-sha256', self.digest()]
        file = ['--dimensions-file', self.config]
        side = ['--dimensions-side', 'baseline']
        cases = [(gate, 'requires --dimensions-file'),
                 (gate + ['--dimension', 'span:x=x'], 'requires --dimensions-file'),
                 (file + gate, 'requires --dimensions-side'),
                 (side + gate, 'requires --dimensions-file'),
                 (file + side + gate + ['--dimension', 'span:x=x'], 'cannot combine'),
                 (file + side + gate + ['--baseline-dimension', 'span:x=x'], 'unrecognized arguments'),
                 (file + side + gate + ['--candidate-dimension', 'span:x=x'], 'unrecognized arguments'),
                 (file + ['--dimensions-side', 'unknown'] + gate, 'invalid choice'),
                 (file + ['--dimensions-side'] + gate, 'expected one argument'),
                 (['--dimensions-file'], 'expected one argument'),
                 (file + side + ['--expect-dimension-sha256'], 'expected one argument')]
        for value in ('', 'a' * 63, 'a' * 65, 'A' * 64, 'g' * 64, '0x' + 'a' * 62,
                      ' ' + 'a' * 64, 'a' * 64 + ' ', 'a' * 64 + '\n', '０' * 64, 'é' * 64):
            cases.append((file + side + ['--expect-dimension-sha256', value], '64 lowercase hexadecimal'))
        for option in ('--expect', '--expect-dimension', '--expect-dimension-sha25'):
            cases += [(file + side + [option, self.digest()], 'unrecognized arguments'),
                      (file + side + [option + '=' + self.digest()], 'unrecognized arguments')]
        self.output.write_bytes(b'old metrics')
        self.report.write_bytes(b'old manifest')
        original = self.config.read_bytes()
        for options, diagnostic in cases:
            with self.subTest(options=options):
                self.assert_pre_capture_error(options, diagnostic, before_pair=True)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.output.read_bytes(), b'old metrics')
        self.assertEqual(self.report.read_bytes(), b'old manifest')

    def test_formatting_mismatch_and_dry_run_preserve_all_files(self):
        expected = self.digest()
        doc = copy.deepcopy(pairs.PAIR)
        doc['candidate'].reverse()
        self.config.write_bytes(json.dumps(doc, indent=4).replace('\n', '\r\n').encode() + b' \r\n')
        self.output.write_bytes(b'old metrics')
        self.report.write_bytes(b'old manifest')
        capture = self.directory / 'capture.json'
        capture.write_bytes(b'capture sentinel')
        before = {p: p.read_bytes() for p in self.directory.iterdir()}
        for side in ('baseline', 'candidate'):
            for options in ([], ['--dry-run'], ['--dimension-provenance']):
                self.assert_pre_capture_error([*self.gate(side, expected), *options],
                                              f'expected {expected}, got {self.digest()}', source=capture)
                self.assertEqual({p: p.read_bytes() for p in self.directory.iterdir()}, before)
            matched = self.run_cli('--dry-run', side=side)
            default = conversion.ConversionDimensionTests.run_cli(self, '--dry-run', side=side)
            self.assertEqual(matched.returncode, 0, matched.stderr)
            self.assertEqual(matched.stdout, default.stdout)
        self.assertEqual({p: p.read_bytes() for p in self.directory.iterdir()}, before)

    def test_mismatch_does_not_wait_for_open_blocked_stdin(self):
        self.output.write_bytes(b'old metrics')
        self.report.write_bytes(b'old manifest')
        default_report = Path(str(self.output) + '.report.json')
        default_report.write_bytes(b'old default manifest')
        for side in ('baseline', 'candidate'):
            with subprocess.Popen(
                    [*pairs.CLI, '-', '--input-format', 'jsonl', '--output', str(self.output),
                     '--force', *map(str, self.gate(side, '0' * 64))], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.directory) as proc:
                try:
                    code = proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    self.fail('digest mismatch waited for stdin')
                self.assertEqual(code, 2)
                self.assertEqual(proc.stdout.read(), b'')
                self.assertIn(b'SHA-256 mismatch', proc.stderr.read())
        self.assertEqual(self.output.read_bytes(), b'old metrics')
        self.assertEqual(self.report.read_bytes(), b'old manifest')
        self.assertEqual(default_report.read_bytes(), b'old default manifest')

    def test_single_bounded_load_validation_and_digest_use_same_bytes(self):
        original, expected = self.config.read_bytes(), self.digest()
        for side in ('baseline', 'candidate'):
            for matches in (True, False):
                self.config.write_bytes(original)
                stream, sizes = io.BytesIO(original), []
                real_open, real_parse = Path.open, cli.parse_dimension_config
                def short_read(size):
                    sizes.append(size)
                    return stream.read(min(size, 7))
                wrapped = MagicMock()
                wrapped.__enter__.return_value.read.side_effect = short_read
                def parse_then_mutate(data):
                    result = real_parse(data)
                    self.config.write_bytes(b'later invalid contents')
                    return result
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(Path, 'open', autospec=True, side_effect=lambda path, *a, **kw:
                                  wrapped if path == self.config and a == ('rb',) else real_open(path, *a, **kw)) as opening, \
                     patch.object(cli, 'parse_dimension_config', side_effect=parse_then_mutate) as parse, \
                     patch.object(cli, 'read_dimension_pair', wraps=cli.read_dimension_pair) as loader, \
                     patch.object(cli, 'read_input', wraps=cli.read_input) as capture, \
                     patch.object(cli, 'write_files', side_effect=AssertionError('publication attempted')), \
                     redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli.main([str(pairs.CAPTURE), '--output', str(self.output), '--dry-run',
                                     '--dimension-provenance', *map(str, self.gate(side, expected if matches else '0' * 64))])
                self.assertEqual(code, 0 if matches else 2, stderr.getvalue())
                if matches:
                    capture.assert_called_once()
                    report = json.loads(stdout.getvalue())
                    self.assertEqual(report['dimension_pair_provenance'],
                                     {'schema_version': 1, 'selected_side': side, 'sha256': expected})
                    self.assertEqual(report['dimensions'][4:], sorted(pairs.PAIR[side], key=lambda d: d['label']))
                else:
                    capture.assert_not_called()
                    self.assertEqual(stdout.getvalue(), '')
                    self.assertIn(f'expected {"0" * 64}, got {expected}', stderr.getvalue())
                loader.assert_called_once_with(self.config)
                parse.assert_called_once_with(original)
                self.assertEqual(sum(c.args == (self.config, 'rb') for c in opening.call_args_list), 1)
                self.assertGreater(len(sizes), 2)
                self.assertTrue(all(0 < n <= 65536 for n in sizes))
                self.assertFalse(self.output.exists())

    def assert_invalid(self, diagnostic):
        original = self.config.read_bytes()
        self.output.write_bytes(b'old metrics')
        self.report.write_bytes(b'old manifest')
        for side in ('baseline', 'candidate'):
            for digest in (self.digest(), '0' * 64):
                stderr = self.assert_pre_capture_error(self.gate(side, digest), diagnostic)
                self.assertNotIn('SHA-256 mismatch', stderr)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.output.read_bytes(), b'old metrics')
        self.assertEqual(self.report.read_bytes(), b'old manifest')

    test_schema_fields_and_types_are_strict = pairs.DimensionConfigTests.test_schema_fields_and_types_are_strict
    test_sources_keys_and_labels_are_validated_without_lossy_coercion = pairs.DimensionConfigTests.test_sources_keys_and_labels_are_validated_without_lossy_coercion
    test_json_syntax_duplicate_keys_constants_and_encoding = pairs.DimensionConfigTests.test_json_syntax_duplicate_keys_constants_and_encoding

    def test_byte_dimension_and_collision_boundaries_on_both_sides(self):
        original = self.config.read_bytes()
        self.config.write_bytes(original + b' ' * (pairs.MAX_BYTES - len(original)))
        proc = self.run_cli('--dry-run')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.config.write_bytes(self.config.read_bytes() + b' ')
        self.assert_invalid('65536')
        for side in ('baseline', 'candidate'):
            entries = [{'source': 'span', 'key': f'x{i}', 'label': f'd{i}'} for i in range(33)]
            self.save(dict(pairs.PAIR, **{side: entries[:32]}))
            proc = self.run_cli('--dry-run', side=side)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(json.loads(proc.stdout)['dimensions'][4:]), 32)
            self.save(dict(pairs.PAIR, **{side: entries}))
            self.assert_invalid('at most 32')
            for invalid, error in (([entries[0], dict(entries[1], label='d0')], 'label collision'),
                                   ([entries[0], dict(entries[0], label='other')], 'duplicate dimension source'),
                                   ([{'source': 'resource', 'key': 'service.name', 'label': 'svc'}], 'built-in')):
                self.save(dict(pairs.PAIR, **{side: invalid}))
                self.assert_invalid(error)

    def test_empty_canonical_unicode_and_legacy_option_abbreviations(self):
        for doc in ({'schema_version': 1, 'baseline': [], 'candidate': []},
                    dict(pairs.PAIR, baseline=[]), dict(pairs.PAIR, candidate=[]),
                    dict(pairs.PAIR, candidate=[{'source': 'span', 'key': '区域:🚀', 'label': 'unicode'},
                                                {'source': 'resource', 'key': 'zone', 'label': 'azone'}])):
            self.config.write_bytes(json.dumps(doc, ensure_ascii=False).encode())
            for side in ('baseline', 'candidate'):
                proc = self.run_cli('--dry-r', side=side)
                default = conversion.ConversionDimensionTests.run_cli(self, '--dry-run', side=side)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, default.stdout)
                self.assertNotIn('dimension_pair_provenance', json.loads(proc.stdout))
                self.assertEqual(json.loads(proc.stdout)['dimensions'][4:], sorted(doc[side], key=lambda d: d['label']))
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_nonregular_pair_paths_and_local_pair_aliases(self):
        original = self.config.read_bytes()
        fifo = self.directory / 'fifo'
        os.mkfifo(fifo)
        for path in (self.directory / 'missing.json', self.directory, fifo):
            self.assert_pre_capture_error([*self.gate(), '--dimensions-file', path], 'regular file')
        fifo.unlink()
        for kind in ('hardlink', 'symlink', '-'):
            alias = self.directory / kind
            if kind == 'symlink':
                alias.symlink_to(self.config)
            else:
                os.link(self.config, alias)
            proc = self.run_cli('--dimensions-file', alias, '--dry-run')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(alias.read_bytes(), original)
            alias.unlink()
        self.assertEqual(self.config.read_bytes(), original)

    def test_metric_report_capture_config_aliases_and_existing_files_preserved(self):
        capture = self.directory / 'capture.json'
        capture.write_bytes(pairs.CAPTURE.read_bytes())
        original = {p: p.read_bytes() for p in (self.config, capture)}
        for destination in ('--output', '--report'):
            for source in (self.config, capture):
                for kind in ('path', 'symlink', 'hardlink', 'parent_symlink'):
                    self.output.write_bytes(b'old metrics')
                    self.report.write_bytes(b'old manifest')
                    alias = self.directory / 'alias'
                    if kind == 'symlink':
                        alias.symlink_to(source)
                    elif kind == 'hardlink':
                        os.link(source, alias)
                    elif kind == 'parent_symlink':
                        alias.symlink_to(self.directory, target_is_directory=True)
                    target = (source if kind == 'path' else alias / source.name if kind == 'parent_symlink' else alias)
                    diagnostic = 'refusing symlink' if source == capture and kind == 'symlink' else 'overwrite input'
                    self.assert_pre_capture_error([*self.gate(), destination, target], diagnostic, source=capture)
                    self.assertEqual(source.read_bytes(), original[source])
                    self.assertEqual(self.output.read_bytes(), b'old metrics')
                    self.assertEqual(self.report.read_bytes(), b'old manifest')
                    alias.unlink(missing_ok=True)
            self.assert_pre_capture_error([*self.gate(), destination, self.report if destination == '--output' else self.output],
                                          'distinct paths', source=capture)
        default_report = Path(str(self.output) + '.report.json')
        for source in (self.config, capture):
            os.link(source, default_report)
            proc = subprocess.run([*pairs.CLI, str(capture), '--output', str(self.output), '--force',
                                   *map(str, self.gate())], capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(proc.stdout, '')
            self.assertIn('overwrite input', proc.stderr)
            self.assertEqual(default_report.read_bytes(), original[source])
            self.assertEqual(self.output.read_bytes(), b'old metrics')
            default_report.unlink()
        for destination in ('--output', '--report'):
            with capture.open('rb') as stdin:
                proc = subprocess.run([*pairs.CLI, '-', '--input-format', 'jsonl', '--output', str(self.output),
                                       '--report', str(self.report), '--force', *map(str, self.gate()), destination, str(capture)],
                                      stdin=stdin, capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(proc.stdout, '')
            self.assertIn('overwrite input', proc.stderr)
        proc = self.run_cli(source=capture)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, '')
        self.assertIn('destination exists', proc.stderr)
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.output.read_bytes(), b'old metrics')
        self.assertEqual(self.report.read_bytes(), b'old manifest')
        self.assertFalse(list(self.directory.glob('.spanmetrics-*')))

    def test_jsonl_file_stdin_dry_run_and_rejected_capture_contracts(self):
        data = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes()))).decode()
        source = self.directory / 'capture.jsonl'
        source.write_text(data)
        for side in ('baseline', 'candidate'):
            for capture in (source, '-'):
                options = ['--input-format', 'jsonl', '--force']
                default = conversion.ConversionDimensionTests.run_cli(self, *options, source=capture, side=side,
                                                           data=data if capture == '-' else None)
                metrics = self.output.read_bytes()
                proc = self.run_cli(*options, source=capture, side=side, data=data if capture == '-' else None)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, default.stdout)
                self.assertEqual(self.output.read_bytes(), metrics)
                self.assert_metrics(side)
            explicit = subprocess.run(
                [*pairs.CLI, '--input', '-', '--input-format', 'jsonl', '--dimensions-file', str(self.config),
                 '--dimensions-side', side, '--expect-dimension-sha256=' + self.digest(),
                 '--output', str(self.output), '--report', str(self.report), '--force'], input=data,
                capture_output=True, text=True, cwd=self.directory, timeout=60)
            self.assertEqual(explicit.returncode, 0, explicit.stderr)
            self.assertEqual(explicit.stdout, proc.stdout)
            for content, options, code in ((data, ('--dry-run',), 0), (data + '{\n', (), 1),
                                           (data, ('--max-input-bytes', '10'), 1),
                                           (data, ('--max-line-bytes', '10'), 1),
                                           (data, ('--max-spans', '1'), 1), (data, ('--max-series', '8'), 1)):
                saved = self.report.read_bytes()
                proc = self.run_cli('--input-format', 'jsonl', '--dimension-provenance', '--force',
                                    *options, source='-', side=side, data=content)
                self.assertEqual(proc.returncode, code, proc.stderr)
                report = json.loads(proc.stdout)
                self.assertEqual(report['dimension_pair_provenance'],
                                 {'schema_version': 1, 'selected_side': side, 'sha256': self.digest()})
                if code == 1:
                    self.assertEqual(report['status'], 'rejected')
                    self.assertNotIn('prometheus_sha256', report)
                    self.assertEqual(self.report.read_bytes(), proc.stdout.encode())
                else:
                    self.assertTrue(report['dry_run'])
                    self.assertEqual(self.report.read_bytes(), saved)
                self.assertFalse(report['metrics_emitted'])
                self.assertEqual(self.output.read_bytes(), metrics)
                self.assertFalse(list(self.directory.glob('.spanmetrics-*')))


if __name__ == '__main__':
    unittest.main()
