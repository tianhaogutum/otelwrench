# SPDX-License-Identifier: Apache-2.0
"""Expected exact-pair digest gate on capture-free inspection."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from otelwrench.spanmetrics import cli
from . import test_dimension_config as pairs
from . import test_validate_dimensions as validation
from . import test_validate_dimension_provenance as provenance


class ValidateDimensionSha256Tests(unittest.TestCase):
    setUp = pairs.DimensionConfigTests.setUp
    save = pairs.DimensionConfigTests.save
    expected = validation.ValidateDimensionTests.expected
    inspect_without_capture = provenance.ValidateDimensionProvenanceTests.inspect_without_capture

    def digest(self):
        return hashlib.sha256(self.config.read_bytes()).hexdigest()

    def run_cli(self, *options, expected=None):
        return subprocess.run(
            [*pairs.CLI, 'validate-dimensions', '--dimensions-file', str(self.config),
             '--expect-dimension-sha256', self.digest() if expected is None else expected, *map(str, options)],
            capture_output=True, text=True, timeout=60, cwd=self.directory)

    def test_exact_match_preserves_default_and_opt_in_json(self):
        original = self.config.read_bytes()
        for opted in (False, True):
            options = ['--dimension-provenance'] if opted else []
            default = validation.ValidateDimensionTests.run_cli(self, *options)
            matched = self.run_cli(*options)
            self.assertEqual(default.returncode, 0, default.stderr)
            self.assertEqual(matched.returncode, 0, matched.stderr)
            self.assertEqual(matched.stderr, '')
            self.assertEqual(matched.stdout, default.stdout)
            report = self.expected(pairs.PAIR)
            if opted:
                report['dimension_pair_provenance'] = {'schema_version': 1, 'sha256': self.digest()}
            self.assertEqual(matched.stdout, json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + '\n')
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['pair.json'])

    def test_formatting_mismatch_preserves_input_and_existing_destinations(self):
        expected = self.digest()
        doc = copy.deepcopy(pairs.PAIR)
        doc['candidate'].reverse()
        changed = json.dumps(doc, indent=4).replace('\n', '\r\n').encode() + b' \r\n'
        self.config.write_bytes(changed)
        self.output.write_bytes(b'reviewed output sentinel')
        capture = self.directory / 'capture.json'
        capture.write_bytes(b'capture sentinel')
        report = self.directory / 'out.prom.report.json'
        report.write_bytes(b'report sentinel')
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        for opted in (False, True):
            options = ['--dimension-provenance'] if opted else []
            proc = self.run_cli(*options, expected=expected)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(proc.stdout, '')
            self.assertEqual(proc.stderr, 'otelwrench spanmetrics: dimension configuration SHA-256 mismatch: '
                             f'expected {expected}, got {self.digest()}\n')
            accepted = self.run_cli(*options)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            result = json.loads(accepted.stdout)
            result.pop('dimension_pair_provenance', None)
            self.assertEqual(result, self.expected(pairs.PAIR))
        self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir()}, before)

    def test_invalid_options_reject_before_pair_capture_stdin_or_publication(self):
        digest = self.digest()
        invalid = ['', 'a' * 63, 'a' * 65, 'A' * 64, 'g' * 64, '0x' + 'a' * 64,
                   ' ' + digest, digest + ' ', digest + '\n', '０' * 64, 'é' * 64]
        commands = [(['--dimensions-file', self.config, '--expect-dimension-sha256', value],
                     'expected exactly 64 lowercase hexadecimal digits') for value in invalid]
        commands += [(['--expect-dimension-sha256', digest], '--dimensions-file'),
                     (['--dimensions-file', self.config, '--expect-dimension-sha256'], 'expected one argument'),
                     (['--dimensions-file', '--expect-dimension-sha256', digest], 'expected one argument')]
        base = ['--dimensions-file', self.config, '--expect-dimension-sha256', digest]
        for options in (['capture.json'], ['-'], ['--input', '-'], ['--input-format', 'jsonl'],
                        ['--output', self.output], ['--report', self.config], ['--force'], ['--dry-run'],
                        ['--dimensions-side', 'baseline'], ['--dimensions-side', 'candidate'],
                        ['--dimension', 'span:x=x'], ['--baseline-dimension', 'span:x=x'],
                        ['--candidate-dimension', 'span:x=x'], ['--buckets', '0.1'],
                        ['--max-series', '1'], ['--max-input-bytes', '1'], ['--max-spans', '1'],
                        ['--max-line-bytes', '1'], ['--dimension-prov'], ['--dimensions-f', self.config],
                        ['--expect-dimension-sha', digest], ['--dimension-provenance=true']):
            commands.append((base + options, 'error:'))
        self.output.write_bytes(b'reviewed output sentinel')
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        for options, diagnostic in commands:
            with self.subTest(options=options), \
                 patch.object(cli, 'read_dimension_pair', side_effect=AssertionError('pair read')):
                code, stdout, stderr = self.inspect_without_capture(options)
                self.assertEqual(code, 2, stderr)
                self.assertEqual(stdout, '')
                self.assertIn(diagnostic, stderr)
        self.assertEqual({p.name: p.read_bytes() for p in self.directory.iterdir()}, before)
        help_result = self.run_cli('--help')
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('--expect-dimension-sha256', help_result.stdout)
        self.assertNotIn('--output', help_result.stdout)

    def test_single_complete_bounded_load_and_parse_gate_loaded_bytes(self):
        original = self.config.read_bytes()
        digest = self.digest()
        real_open, real_parse = Path.open, cli.parse_dimension_config
        # Short reads and mutation after parsing expose reopening or revalidation.
        for expected in (digest, '0' * 64):
            for opted in (False, True):
                self.config.write_bytes(original)
                stream, sizes = io.BytesIO(original), []
                def short_read(size):
                    sizes.append(size)
                    return stream.read(min(size, 7))
                wrapped = MagicMock()
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
                        ['--dimensions-file', self.config, '--expect-dimension-sha256', expected,
                         *(['--dimension-provenance'] if opted else [])])
                self.assertEqual(code, 0 if expected == digest else 2, stderr)
                if code == 0:
                    report = self.expected(pairs.PAIR)
                    if opted:
                        report['dimension_pair_provenance'] = {'schema_version': 1, 'sha256': digest}
                    self.assertEqual(json.loads(stdout), report)
                    self.assertEqual(stderr, '')
                else:
                    self.assertEqual(stdout, '')
                    self.assertIn(f'expected {expected}, got {digest}', stderr)
                loader.assert_called_once_with(self.config)
                parse.assert_called_once_with(original)
                self.assertEqual(sum(c.args == (self.config, 'rb') for c in opening.call_args_list), 1)
                self.assertGreater(len(sizes), 2)
                self.assertTrue(all(0 < n <= 65536 for n in sizes))
                self.assertEqual(self.config.read_bytes(), b'later invalid contents')

    def test_empty_canonical_and_unicode_selections(self):
        docs = [{'schema_version': 1, 'baseline': [], 'candidate': []},
                {'schema_version': 1, 'baseline': [], 'candidate': pairs.PAIR['candidate'][::-1]},
                {'schema_version': 1, 'baseline': pairs.PAIR['baseline'], 'candidate': []},
                {'schema_version': 1, 'baseline': [{'source': 'span', 'key': 'zone', 'label': 'zone'}],
                 'candidate': [{'source': 'resource', 'key': 'zone', 'label': 'zone'},
                               {'source': 'span', 'key': '区域:🚀', 'label': 'unicode'}]}]
        for doc in docs:
            self.config.write_bytes(json.dumps(doc, ensure_ascii=False).encode())
            original = self.config.read_bytes()
            proc = self.run_cli()
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), self.expected(doc))
            self.assertEqual(self.config.read_bytes(), original)

    def assert_invalid(self, expected):
        original = self.config.read_bytes()
        self.output.write_bytes(b'reviewed output sentinel')
        # Matching invalid bytes and mismatching bytes both require strict validation first.
        for digest in (self.digest(), '0' * 64):
            code, stdout, stderr = self.inspect_without_capture(
                ['--dimensions-file', self.config, '--expect-dimension-sha256', digest])
            self.assertEqual(code, 2, stderr)
            self.assertEqual(stdout, '')
            self.assertIn(expected, stderr)
            self.assertNotIn('SHA-256 mismatch', stderr)
            self.assertNotIn('Traceback', stderr)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.output.read_bytes(), b'reviewed output sentinel')
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['comparison.json', 'pair.json'])

    test_schema_fields_and_types_are_strict = pairs.DimensionConfigTests.test_schema_fields_and_types_are_strict
    test_sources_keys_and_labels_are_validated_without_lossy_coercion = pairs.DimensionConfigTests.test_sources_keys_and_labels_are_validated_without_lossy_coercion
    test_json_syntax_duplicate_keys_constants_and_encoding = pairs.DimensionConfigTests.test_json_syntax_duplicate_keys_constants_and_encoding
    test_byte_dimension_and_collision_boundaries_on_both_sides = validation.ValidateDimensionTests.test_byte_dimension_and_collision_boundaries_on_both_sides

    def test_nonregular_files_and_literal_dash_do_not_read_stdin(self):
        original = self.config.read_bytes()
        fifo = self.directory / 'fifo'
        os.mkfifo(fifo)
        for path in (self.directory / 'missing.json', self.directory, fifo):
            code, stdout, stderr = self.inspect_without_capture(
                ['--dimensions-file', path, '--expect-dimension-sha256', self.digest()])
            self.assertEqual(code, 2, stderr)
            self.assertEqual(stdout, '')
            self.assertIn('regular file', stderr)
        fifo.unlink()
        for name, kind in (('hardlink', 'hardlink'), ('symlink', 'symlink'), ('-', 'hardlink')):
            alias = self.directory / name
            if kind == 'symlink':
                alias.symlink_to(self.config)
            else:
                os.link(self.config, alias)
            proc = self.run_cli('--dimensions-file', alias)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), self.expected(pairs.PAIR))
            self.assertEqual(alias.read_bytes(), original)
            alias.unlink()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['pair.json'])


if __name__ == '__main__':
    unittest.main()
