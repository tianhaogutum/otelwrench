# SPDX-License-Identifier: Apache-2.0
"""Exact loaded-pair provenance for capture-free inspection."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

from otelwrench.spanmetrics import cli
from . import test_dimension_config as pairs
from . import test_validate_dimensions as validation


class ValidateDimensionProvenanceTests(unittest.TestCase):
    setUp = pairs.DimensionConfigTests.setUp
    save = pairs.DimensionConfigTests.save

    def run_cli(self, *options):
        return subprocess.run(
            [*pairs.CLI, 'validate-dimensions', '--dimensions-file', str(self.config),
             '--dimension-provenance', *map(str, options)],
            capture_output=True, text=True, timeout=60, cwd=self.directory)

    def expected(self, doc, data):
        report = validation.ValidateDimensionTests.expected(self, doc)
        report['dimension_pair_provenance'] = {'schema_version': 1, 'sha256': hashlib.sha256(data).hexdigest()}
        return report

    def test_exact_bytes_default_parity_and_formatting_sensitive_digest(self):
        original = self.config.read_bytes()
        default = subprocess.run(
            [*pairs.CLI, 'validate-dimensions', '--dimensions-file', str(self.config)],
            capture_output=True, text=True, timeout=60, cwd=self.directory)
        self.assertEqual(default.returncode, 0, default.stderr)
        expected_default = validation.ValidateDimensionTests.expected(self, pairs.PAIR)
        self.assertEqual(default.stdout, json.dumps(expected_default, ensure_ascii=True, indent=2, sort_keys=True) + '\n')
        first = self.run_cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, '')
        self.assertEqual(first.stdout, json.dumps(self.expected(pairs.PAIR, original),
                                               ensure_ascii=True, indent=2, sort_keys=True) + '\n')
        # Pure formatting and entry-order changes preserve selections, but not the raw-byte digest.
        reordered = copy.deepcopy(pairs.PAIR)
        reordered['candidate'].reverse()
        encoded = json.dumps(reordered, indent=4).replace('\n', '\r\n').encode() + b' \r\n'
        self.config.write_bytes(encoded)
        again = self.run_cli()
        self.assertEqual(again.returncode, 0, again.stderr)
        report = json.loads(again.stdout)
        self.assertEqual(report, self.expected(reordered, encoded))
        self.assertNotEqual(report['dimension_pair_provenance'], json.loads(first.stdout)['dimension_pair_provenance'])
        del report['dimension_pair_provenance']
        self.assertEqual(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + '\n', default.stdout)
        self.assertEqual(self.config.read_bytes(), encoded)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['pair.json'])

    def inspect_without_capture(self, options):
        stdin = Mock()
        stdin.buffer.read.side_effect = AssertionError('stdin consumed')
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli.sys, 'stdin', stdin), \
             patch.object(cli, 'read_input', side_effect=AssertionError('capture consumed')), \
             patch.object(cli, 'convert', side_effect=AssertionError('conversion')), \
             patch.object(cli, 'compare', side_effect=AssertionError('comparison')), \
             patch.object(cli, 'write_files', side_effect=AssertionError('publication')), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(['validate-dimensions', *map(str, options)])
            except SystemExit as exc:
                code = exc.code
        stdin.buffer.read.assert_not_called()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_one_bounded_read_and_parse_without_capture_stdin_or_publication(self):
        original = self.config.read_bytes()
        stream, sizes = io.BytesIO(original), []
        real_open, real_parse = Path.open, cli.parse_dimension_config

        def short_read(size):
            sizes.append(size)
            return stream.read(min(size, 7))

        wrapped = unittest.mock.MagicMock()
        wrapped.__enter__.return_value.read.side_effect = short_read

        def parse_then_mutate(data):
            result = real_parse(data)
            self.config.write_bytes(b'later invalid contents')
            return result

        with patch.object(Path, 'open', autospec=True, side_effect=lambda path, *a, **kw:
                          wrapped if path == self.config and a == ('rb',) else real_open(path, *a, **kw)) as opening, \
             patch.object(cli, 'read_dimension_pair', wraps=cli.read_dimension_pair) as loader, \
             patch.object(cli, 'parse_dimension_config', side_effect=parse_then_mutate) as parse:
            code, stdout, stderr = self.inspect_without_capture(
                ['--dimensions-file', self.config, '--dimension-provenance'])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, '')
        self.assertEqual(json.loads(stdout), self.expected(pairs.PAIR, original))
        loader.assert_called_once_with(self.config)
        parse.assert_called_once_with(original)
        self.assertEqual(sum(c.args == (self.config, 'rb') for c in opening.call_args_list), 1)
        self.assertGreater(len(sizes), 2)
        self.assertTrue(all(0 < n <= 65536 for n in sizes))
        # Mutation is by this test, after loading: provenance must describe the loaded pair.
        self.assertEqual(self.config.read_bytes(), b'later invalid contents')

    def test_empty_selections_source_identity_and_unicode(self):
        for doc in ({'schema_version': 1, 'baseline': [], 'candidate': []},
                    {'schema_version': 1, 'baseline': [{'source': 'span', 'key': 'zone', 'label': 'zone'}],
                     'candidate': [{'source': 'resource', 'key': 'zone', 'label': 'zone'},
                                   {'source': 'span', 'key': '区域:🚀', 'label': 'unicode'}]}):
            self.config.write_bytes(json.dumps(doc, ensure_ascii=False).encode('utf-8'))
            original = self.config.read_bytes()
            proc = self.run_cli()
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), self.expected(doc, original))
            self.assertEqual(self.config.read_bytes(), original)

    def test_invalid_options_reject_before_loading_or_any_capture_io(self):
        original = self.config.read_bytes()
        commands = [['--dimension-provenance'], ['--dimension-provenance', '--dimensions-file']]
        for options in (['capture.json'], ['-'], ['--input', '-'], ['--input-format', 'jsonl'],
                        ['--output', self.output], ['--report', self.config], ['--force'], ['--dry-run'],
                        ['--dimensions-side', 'baseline'], ['--dimensions-side', 'candidate'],
                        ['--dimension', 'span:x=x'], ['--baseline-dimension', 'span:x=x'],
                        ['--candidate-dimension', 'span:x=x'], ['--buckets', '0.1'],
                        ['--max-series', '1'], ['--max-input-bytes', '1'], ['--max-spans', '1'],
                        ['--max-line-bytes', '1'], ['--dimension-provenance=true'],
                        ['--dimension-prov'], ['--dimensions-f', self.config]):
            commands.append(['--dimensions-file', self.config, '--dimension-provenance', *options])
        self.output.write_bytes(b'reviewed output sentinel')
        for options in commands:
            with self.subTest(options=options), \
                 patch.object(cli, 'read_dimension_pair', side_effect=AssertionError('pair loaded')):
                code, stdout, stderr = self.inspect_without_capture(options)
                self.assertEqual(code, 2, stderr)
                self.assertEqual(stdout, '')
                self.assertIn('error:', stderr)
                self.assertEqual(self.config.read_bytes(), original)
                self.assertEqual(self.output.read_bytes(), b'reviewed output sentinel')
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['comparison.json', 'pair.json'])
        help_result = self.run_cli('--help')
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('--dimension-provenance', help_result.stdout)
        self.assertNotIn('--output', help_result.stdout)

    def assert_invalid(self, expected):
        original = self.config.read_bytes()
        self.output.write_bytes(b'reviewed output sentinel')
        code, stdout, stderr = self.inspect_without_capture(
            ['--dimensions-file', self.config, '--dimension-provenance'])
        self.assertEqual(code, 2, stderr)
        self.assertEqual(stdout, '')
        self.assertIn(expected, stderr)
        self.assertNotIn('Traceback', stderr)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.output.read_bytes(), b'reviewed output sentinel')
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['comparison.json', 'pair.json'])

    test_schema_fields_and_types_are_strict = pairs.DimensionConfigTests.test_schema_fields_and_types_are_strict
    test_sources_keys_and_labels_are_validated_without_lossy_coercion = pairs.DimensionConfigTests.test_sources_keys_and_labels_are_validated_without_lossy_coercion
    test_json_syntax_duplicate_keys_constants_and_encoding = pairs.DimensionConfigTests.test_json_syntax_duplicate_keys_constants_and_encoding

    def test_byte_dimension_and_collision_boundaries_on_both_sides(self):
        valid = self.config.read_bytes()
        self.config.write_bytes(valid + b' ' * (pairs.MAX_BYTES - len(valid)))
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), self.expected(pairs.PAIR, self.config.read_bytes()))
        self.config.write_bytes(self.config.read_bytes() + b' ')
        self.assert_invalid('65536')
        for side in ('baseline', 'candidate'):
            entries = [{'source': 'span', 'key': f'x{i}', 'label': f'd{i}'} for i in range(33)]
            self.save(dict(pairs.PAIR, **{side: entries[:32]}))
            proc = self.run_cli()
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), self.expected(json.loads(self.config.read_bytes()), self.config.read_bytes()))
            self.save(dict(pairs.PAIR, **{side: entries}))
            self.assert_invalid('at most 32')
            self.save(dict(pairs.PAIR, **{side: [entries[0], dict(entries[1], label='d0')]}))
            self.assert_invalid('label collision')

    def test_nonregular_paths_preserve_files_and_do_not_consume_stdin(self):
        original = self.config.read_bytes()
        fifo = self.directory / 'fifo'
        os.mkfifo(fifo)
        for path in (self.directory / 'missing.json', self.directory, fifo, self.directory / '-'):
            code, stdout, stderr = self.inspect_without_capture(
                ['--dimensions-file', path, '--dimension-provenance'])
            self.assertEqual(code, 2, stderr)
            self.assertEqual(stdout, '')
            self.assertIn('regular file', stderr)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['fifo', 'pair.json'])


if __name__ == '__main__':
    unittest.main()
