# SPDX-License-Identifier: Apache-2.0
"""Exact reviewed-pair digest gate before comparison capture consumption."""
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
from .test_jsonl import jsonl, split_requests


class ComparisonSha256Tests(unittest.TestCase):
    setUp = pairs.DimensionConfigTests.setUp
    save = pairs.DimensionConfigTests.save

    def digest(self):
        return hashlib.sha256(self.config.read_bytes()).hexdigest()

    def run_cli(self, *options, source=pairs.CAPTURE, expected=None, data=None):
        return subprocess.run(
            [*pairs.CLI, 'compare', str(source), '--output', str(self.output),
             '--dimensions-file', str(self.config), '--expect-dimension-sha256',
             self.digest() if expected is None else expected, *map(str, options)],
            input=data, capture_output=True, text=True, timeout=60, cwd=self.directory)

    def test_exact_match_preserves_default_and_opt_in_publication(self):
        original = self.config.read_bytes()
        for opted in (False, True):
            options = ['--dimension-provenance'] if opted else []
            default = pairs.DimensionConfigTests.run_cli(self, *options, '--force')
            self.assertEqual(default.returncode, 0, default.stderr)
            matched = self.run_cli(*options, '--force')
            self.assertEqual(matched.returncode, 0, matched.stderr)
            self.assertEqual(matched.stderr, '')
            self.assertEqual(matched.stdout, default.stdout)
            self.assertEqual(self.output.read_bytes(), matched.stdout.encode())
            report = json.loads(matched.stdout)
            self.assertEqual((report['unique_records'], report['series_delta']), (6, 24))
            self.assertEqual([report['configurations'][s]['series'] for s in ('baseline', 'candidate')], [16, 40])
            if opted:
                self.assertEqual(report['dimension_pair_provenance'], {'schema_version': 1, 'sha256': self.digest()})
            else:
                self.assertNotIn('dimension_pair_provenance', report)
        self.assertEqual(self.config.read_bytes(), original)

    def assert_pre_capture_error(self, options, diagnostic, before_pair=False, source=None):
        stdin = Mock()
        stdin.buffer.read.side_effect = AssertionError('stdin consumed')
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli.sys, 'stdin', stdin), \
             patch.object(cli, 'read_dimension_pair', wraps=cli.read_dimension_pair) as loader, \
             patch.object(cli, 'read_input', side_effect=AssertionError('capture consumed')), \
             patch.object(cli, 'compare', side_effect=AssertionError('comparison started')), \
             patch.object(cli, 'write_files', side_effect=AssertionError('publication attempted')), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(['compare', *(['-', '--input-format', 'jsonl'] if source is None else [str(source)]),
                                 '--output', str(self.output), '--force', *map(str, options)])
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
        self.output.write_bytes(b'reviewed output')
        original = self.config.read_bytes()
        gate = ['--expect-dimension-sha256', self.digest()]
        file = ['--dimensions-file', self.config]
        cases = [(gate, 'requires --dimensions-file'),
                 (gate + ['--baseline-dimension', 'span:x=x'], 'requires --dimensions-file'),
                 (gate + ['--candidate-dimension', 'span:x=x'], 'requires --dimensions-file'),
                 (file + gate + ['--baseline-dimension', 'span:x=x'], 'cannot combine'),
                 (file + gate + ['--candidate-dimension', 'span:x=x'], 'cannot combine'),
                 (['--dimensions-file'], 'expected one argument'),
                 (file + ['--expect-dimension-sha256'], 'expected one argument'),
                 (file + gate + ['--dimensions-side', 'baseline'], 'unrecognized arguments')]
        for value in ('', 'a' * 63, 'a' * 65, 'A' * 64, 'g' * 64, '0x' + 'a' * 62,
                      ' ' + 'a' * 64, 'a' * 64 + '\n', 'é' * 64):
            cases.append((file + ['--expect-dimension-sha256', value], '64 lowercase hexadecimal'))
        for option in ('--expect', '--expect-dimension', '--expect-dimension-sha25'):
            cases += [(file + [option, self.digest()], 'unrecognized arguments'),
                      (file + [option + '=' + self.digest()], 'unrecognized arguments')]
        for options, diagnostic in cases:
            with self.subTest(options=options):
                self.assert_pre_capture_error(options, diagnostic, before_pair=True)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.output.read_bytes(), b'reviewed output')

    def test_formatting_mismatch_and_dry_run_preserve_files(self):
        expected = self.digest()
        doc = copy.deepcopy(pairs.PAIR)
        doc['candidate'].reverse()
        changed = json.dumps(doc, indent=4).replace('\n', '\r\n').encode() + b' \r\n'
        self.config.write_bytes(changed)
        self.output.write_bytes(b'reviewed output')
        for options in ([], ['--dry-run'], ['--dimension-provenance']):
            stderr = self.assert_pre_capture_error(
                ['--dimensions-file', self.config, '--expect-dimension-sha256', expected, *options],
                f'expected {expected}, got {self.digest()}')
            self.assertIn('SHA-256 mismatch', stderr)
            self.assertEqual(self.output.read_bytes(), b'reviewed output')
        # New digest succeeds with the same resolved comparison, including dry-run.
        matched = self.run_cli('--dry-run')
        default = pairs.DimensionConfigTests.run_cli(self, '--dry-run')
        self.assertEqual(matched.returncode, 0, matched.stderr)
        self.assertEqual(matched.stdout, default.stdout)
        self.assertEqual(json.loads(matched.stdout)['series_delta'], 24)
        self.assertEqual(self.output.read_bytes(), b'reviewed output')
        self.assertEqual(self.config.read_bytes(), changed)

    def test_mismatch_does_not_wait_for_blocked_stdin(self):
        self.output.write_bytes(b'reviewed output')
        with subprocess.Popen(
                [*pairs.CLI, 'compare', '-', '--input-format', 'jsonl', '--output', str(self.output),
                 '--force', '--dimensions-file', str(self.config), '--expect-dimension-sha256', '0' * 64],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.directory) as proc:
            # Leave the producer open, with no EOF or data: a capture read would hang.
            try:
                code = proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.fail('digest mismatch waited for stdin')
            self.assertEqual(code, 2)
            self.assertEqual(proc.stdout.read(), b'')
            self.assertIn(b'SHA-256 mismatch', proc.stderr.read())
        self.assertEqual(self.output.read_bytes(), b'reviewed output')

    def test_single_bounded_load_and_validation_tied_to_loaded_bytes(self):
        original = self.config.read_bytes()
        expected = self.digest()
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
                code = cli.main(['compare', str(pairs.CAPTURE), '--output', str(self.output), '--dry-run',
                                 '--dimensions-file', str(self.config), '--dimension-provenance',
                                 '--expect-dimension-sha256', expected if matches else '0' * 64])
            self.assertEqual(code, 0 if matches else 2, stderr.getvalue())
            if matches:
                capture.assert_called_once()
                report = json.loads(stdout.getvalue())
                self.assertEqual(report['dimension_pair_provenance'], {'schema_version': 1, 'sha256': expected})
                for side in ('baseline', 'candidate'):
                    self.assertEqual(report['configurations'][side]['dimensions'][4:],
                                     sorted(pairs.PAIR[side], key=lambda d: d['label']))
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
        self.output.write_bytes(b'reviewed output')
        for digest in (self.digest(), '0' * 64):
            stderr = self.assert_pre_capture_error(
                ['--dimensions-file', self.config, '--expect-dimension-sha256', digest], diagnostic)
            self.assertNotIn('SHA-256 mismatch', stderr)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.output.read_bytes(), b'reviewed output')

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
            proc = self.run_cli('--dry-run')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(json.loads(proc.stdout)['configurations'][side]['dimensions'][4:]), 32)
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
            proc = self.run_cli('--dry-r')
            default = pairs.DimensionConfigTests.run_cli(self, '--dry-run')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, default.stdout)
            self.assertNotIn('dimension_pair_provenance', json.loads(proc.stdout))
            for side in ('baseline', 'candidate'):
                self.assertEqual(json.loads(proc.stdout)['configurations'][side]['dimensions'][4:],
                                 sorted(doc[side], key=lambda d: d['label']))
        self.assertFalse(self.output.exists())

    def test_nonregular_pair_paths_and_local_pair_aliases(self):
        original = self.config.read_bytes()
        fifo = self.directory / 'fifo'
        os.mkfifo(fifo)
        for path in (self.directory / 'missing.json', self.directory, fifo):
            self.assert_pre_capture_error(
                ['--dimensions-file', path, '--expect-dimension-sha256', self.digest()], 'regular file')
        fifo.unlink()
        for kind in ('hardlink', 'symlink'):
            alias = self.directory / kind
            if kind == 'hardlink':
                os.link(self.config, alias)
            else:
                alias.symlink_to(self.config)
            proc = self.run_cli('--dimensions-file', alias, '--dry-run')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(alias.read_bytes(), original)
            alias.unlink()
        self.assertEqual(self.config.read_bytes(), original)

    def test_destination_capture_config_aliases_and_existing_output_preserved(self):
        capture = self.directory / 'capture.json'
        capture.write_bytes(pairs.CAPTURE.read_bytes())
        original = {p: p.read_bytes() for p in (self.config, capture)}
        gate = ['--dimensions-file', self.config, '--expect-dimension-sha256', self.digest()]
        for source in (self.config, capture):
            for kind in ('path', 'symlink', 'hardlink'):
                alias = self.directory / 'alias'
                if kind == 'symlink':
                    alias.symlink_to(source)
                elif kind == 'hardlink':
                    os.link(source, alias)
                target = source if kind == 'path' else alias
                diagnostic = 'refusing symlink' if source == capture and kind == 'symlink' else 'overwrite input'
                self.assert_pre_capture_error([*gate, '--output', target], diagnostic, source=capture)
                self.assertEqual(source.read_bytes(), original[source])
                alias.unlink(missing_ok=True)
        # Stdin redirected from a regular file retains alias protection too.
        with capture.open('rb') as stdin:
            proc = subprocess.run(
                [*pairs.CLI, 'compare', '-', '--input-format', 'jsonl', *map(str, gate),
                 '--output', str(capture), '--force'], stdin=stdin, capture_output=True, text=True,
                cwd=self.directory, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, '')
        self.assertIn('overwrite input', proc.stderr)
        self.output.write_bytes(b'reviewed output')
        proc = self.run_cli(source=capture)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, '')
        self.assertIn('destination exists', proc.stderr)
        self.assertEqual(self.output.read_bytes(), b'reviewed output')
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)

    def test_jsonl_file_stdin_dry_run_and_rejected_capture_contracts(self):
        data = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes()))).decode()
        source = self.directory / 'capture.jsonl'
        source.write_text(data)
        for capture in (source, '-'):
            proc = self.run_cli('--input-format', 'jsonl', '--force', source=capture, data=data if capture == '-' else None)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)['input_sha256'], hashlib.sha256(data.encode()).hexdigest())
        explicit = subprocess.run(
            [*pairs.CLI, 'compare', '--input', '-', '--input-format', 'jsonl',
             '--dimensions-file', str(self.config), '--expect-dimension-sha256=' + self.digest(),
             '--output', str(self.output), '--force'], input=data, capture_output=True, text=True,
            cwd=self.directory, timeout=60)
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual(explicit.stdout, proc.stdout)
        saved = self.output.read_bytes()
        for content, options, code in ((data, ('--dry-run',), 0), (data + '{\n', (), 1),
                                       (data, ('--max-input-bytes', '10'), 1),
                                       (data, ('--max-line-bytes', '10'), 1),
                                       (data, ('--max-spans', '1'), 1), (data, ('--max-series', '16'), 1)):
            proc = self.run_cli('--input-format', 'jsonl', '--dimension-provenance', '--force',
                                *options, source='-', data=content)
            self.assertEqual(proc.returncode, code, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report['dimension_pair_provenance'], {'schema_version': 1, 'sha256': self.digest()})
            if code == 1:
                self.assertFalse(report['comparison_complete'])
                self.assertIsNone(report['unique_records'])
                self.assertIsNone(report['series_delta'])
                self.assertEqual(report['partitions'], [])
                self.assertTrue(all(c['series'] is None and c['groups'] is None for c in report['configurations'].values()))
                self.assertEqual(self.output.read_bytes(), proc.stdout.encode())
            else:
                self.assertEqual(self.output.read_bytes(), saved)


if __name__ == '__main__':
    unittest.main()
