# SPDX-License-Identifier: Apache-2.0
"""Complete-pair provenance for comparison, with unchanged default reports."""
from contextlib import redirect_stderr, redirect_stdout
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
from .test_jsonl import jsonl, split_requests


class ComparisonProvenanceTests(unittest.TestCase):
    setUp = pairs.DimensionConfigTests.setUp
    save = pairs.DimensionConfigTests.save

    def run_cli(self, *options, source=pairs.CAPTURE, file=True, data=None):
        return subprocess.run(
            [*pairs.CLI, 'compare', str(source), '--output', str(self.output),
             *(['--dimensions-file', str(self.config)] if file else []), *map(str, options)],
            input=data, capture_output=True, text=True, timeout=60, cwd=self.directory)

    def provenance(self, data=None):
        return {'schema_version': 1, 'sha256': hashlib.sha256(
            self.config.read_bytes() if data is None else data).hexdigest()}

    def test_opt_in_exact_pair_bytes_and_default_flag_parity(self):
        original = self.config.read_bytes()
        default = self.run_cli()
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertNotIn('dimension_pair_provenance', json.loads(default.stdout))
        opted = self.run_cli('--dimension-provenance', '--force')
        self.assertEqual(opted.returncode, 0, opted.stderr)
        self.assertEqual(opted.stderr, '')
        self.assertEqual(self.output.read_bytes(), opted.stdout.encode())
        report = json.loads(opted.stdout)
        self.assertEqual(report.pop('dimension_pair_provenance'), self.provenance(original))
        self.assertEqual(report, json.loads(default.stdout))
        self.assertEqual((report['configurations']['baseline']['groups'],
                          report['configurations']['candidate']['groups'], report['series_delta']), (2, 5, 24))
        flags = []
        for side in ('baseline', 'candidate'):
            for d in pairs.PAIR[side]:
                flags += [f'--{side}-dimension', f'{d["source"]}:{d["key"]}={d["label"]}']
        expanded = self.run_cli(*flags, '--force', file=False)
        self.assertEqual(expanded.returncode, 0, expanded.stderr)
        self.assertEqual(expanded.stdout, default.stdout)
        self.assertEqual(self.config.read_bytes(), original)
        doc = copy.deepcopy(pairs.PAIR)
        doc['candidate'].reverse()
        self.config.write_bytes(json.dumps(doc, indent=4).encode() + b'\n ')
        changed = self.config.read_bytes()
        reformatted = self.run_cli('--dimension-provenance', '--force')
        self.assertEqual(reformatted.returncode, 0, reformatted.stderr)
        result = json.loads(reformatted.stdout)
        digest = result.pop('dimension_pair_provenance')
        self.assertEqual(digest, self.provenance(changed))
        self.assertNotEqual(digest, self.provenance(original))
        self.assertEqual(result, report)
        self.assertEqual(self.config.read_bytes(), changed)

    def test_empty_selections_are_explicit_and_canonical(self):
        for empty in ('baseline', 'candidate'):
            doc = copy.deepcopy(pairs.PAIR)
            doc[empty] = []
            self.save(doc)
            proc = self.run_cli('--dimension-provenance', '--dry-run')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report['dimension_pair_provenance'], self.provenance())
            for side in ('baseline', 'candidate'):
                self.assertEqual(report['configurations'][side]['dimensions'][4:],
                                 sorted(doc[side], key=lambda d: d['label']))
            self.assertFalse(self.output.exists())

    def test_single_bounded_pair_read_ties_digest_to_loaded_selections(self):
        original = self.config.read_bytes()
        real_open = Path.open
        stream = io.BytesIO(original)
        sizes = []
        def short_read(size):
            sizes.append(size)
            return stream.read(min(size, 7))
        wrapped = MagicMock()
        wrapped.__enter__.return_value.read.side_effect = short_read
        read_input = cli.read_input
        def mutate_then_read(*args):
            self.config.write_bytes(b'invalid later contents')
            return read_input(*args)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(Path, 'open', autospec=True, side_effect=lambda path, *a, **kw:
                          wrapped if path == self.config and a == ('rb',) else real_open(path, *a, **kw)) as opening, \
             patch.object(cli, 'read_input', side_effect=mutate_then_read) as capture, \
             patch.object(cli, 'parse_dimension_config', wraps=cli.parse_dimension_config) as parse, \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(['compare', str(pairs.CAPTURE), '--output', str(self.output), '--dry-run',
                             '--dimensions-file', str(self.config), '--dimension-provenance'])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(sum(c.args[0] == self.config and c.args[1:] == ('rb',)
                             for c in opening.call_args_list), 1)
        parse.assert_called_once_with(original)
        capture.assert_called_once()
        self.assertTrue(all(0 < size <= 65536 for size in sizes))
        self.assertGreater(len(sizes), 2)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report['dimension_pair_provenance'], self.provenance(original))
        for side in ('baseline', 'candidate'):
            self.assertEqual(report['configurations'][side]['dimensions'][4:],
                             sorted(pairs.PAIR[side], key=lambda d: d['label']))
        self.assertFalse(self.output.exists())

    def test_jsonl_stdin_dry_run_and_rejection_suppress_counts(self):
        original = self.config.read_bytes()
        data = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes()))).decode()
        good = self.run_cli('--dimension-provenance', '--input-format', 'jsonl', source='-', data=data)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(json.loads(good.stdout)['input_sha256'], hashlib.sha256(data.encode()).hexdigest())
        saved = self.output.read_bytes()
        for content, options, code in ((data, ('--dry-run',), 0),
                                       (data + '{\n', (), 1),
                                       (data, ('--max-input-bytes', '10'), 1),
                                       (data, ('--max-line-bytes', '10'), 1),
                                       (data, ('--max-spans', '1'), 1),
                                       (data, ('--max-series', '16'), 1)):
            with self.subTest(options=options, code=code):
                proc = self.run_cli('--dimension-provenance', '--input-format', 'jsonl', '--force',
                                    *options, source='-', data=content)
                self.assertEqual(proc.returncode, code, proc.stderr)
                report = json.loads(proc.stdout)
                self.assertEqual(report['dimension_pair_provenance'], self.provenance(original))
                self.assertFalse(report['metrics_emitted'])
                if code == 1:
                    self.assertFalse(report['comparison_complete'])
                    self.assertIsNone(report['unique_records'])
                    self.assertIsNone(report['series_delta'])
                    self.assertEqual(report['partitions'], [])
                    for side in report['configurations'].values():
                        self.assertIsNone(side['groups'])
                        self.assertIsNone(side['series'])
                    self.assertEqual(self.output.read_bytes(), proc.stdout.encode())
                    if '--max-input-bytes' in options:
                        self.assertNotIn('input_sha256', report)
                    else:
                        self.assertEqual(report['input_sha256'], hashlib.sha256(content.encode()).hexdigest())
                else:
                    self.assertEqual(self.output.read_bytes(), saved)
                self.assertEqual(self.config.read_bytes(), original)
        # The baseline may be the failing side; comparison still suppresses both.
        self.save(dict(pairs.PAIR, baseline=pairs.PAIR['candidate'], candidate=pairs.PAIR['baseline']))
        proc = self.run_cli('--dimension-provenance', '--max-series', '16', '--force')
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report['dimension_pair_provenance'], self.provenance())
        self.assertEqual(report['configurations']['baseline']['validation']['status'], 'rejected')
        self.assertEqual(report['configurations']['candidate']['validation']['status'], 'ok')
        self.assertTrue(all(c['series'] is None and c['groups'] is None for c in report['configurations'].values()))
        self.assertIsNone(report['series_delta'])
        self.assertEqual(report['partitions'], [])

    def assert_error_before_capture(self, options, diagnostic):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli, 'read_input', side_effect=AssertionError('capture consumed')), \
             patch.object(cli, 'write_files', side_effect=AssertionError('publication attempted')), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(['compare', '-', '--input-format', 'jsonl', '--output', str(self.output),
                                 '--force', '--dimension-provenance', *map(str, options)])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 2, stderr.getvalue())
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn(diagnostic, stderr.getvalue())

    def test_invalid_options_reject_before_capture_or_publication(self):
        self.output.write_bytes(b'reviewed comparison')
        original = self.config.read_bytes()
        for options, diagnostic in (([], 'requires --dimensions-file'),
                                    (['--baseline-dimension', 'span:x=x'], 'requires --dimensions-file'),
                                    (['--candidate-dimension', 'span:x=x'], 'requires --dimensions-file'),
                                    (['--dimensions-file'], 'expected one argument'),
                                    (['--dimensions-file', self.config, '--dimensions-side', 'baseline'], 'unrecognized arguments')):
            with self.subTest(options=options):
                self.assert_error_before_capture(options, diagnostic)
        for side in ('baseline', 'candidate'):
            self.assert_error_before_capture(['--dimensions-file', self.config,
                                              f'--{side}-dimension', 'span:x=x'], 'cannot combine')
        self.assertEqual(self.output.read_bytes(), b'reviewed comparison')
        self.assertEqual(self.config.read_bytes(), original)

    def test_invalid_complete_pair_rejects_before_capture_or_publication(self):
        self.output.write_bytes(b'reviewed comparison')
        cases = [(b'{', 'JSON'), (b' ' * (pairs.MAX_BYTES + 1), '65536')]
        for side in ('baseline', 'candidate'):
            cases.append((json.dumps(dict(pairs.PAIR, **{side: [
                {'source': 'span', 'key': 'x', 'label': 'le'}]})).encode(), side))
        for data, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                self.config.write_bytes(data)
                self.assert_error_before_capture(['--dimensions-file', self.config], diagnostic)
                self.assertEqual(self.config.read_bytes(), data)
        self.config.unlink()
        self.assert_error_before_capture(['--dimensions-file', self.config], 'regular file')
        self.config.mkdir()
        self.assert_error_before_capture(['--dimensions-file', self.config], 'regular file')
        self.assertEqual(self.output.read_bytes(), b'reviewed comparison')

    def test_pair_aliases_preserved_before_capture_or_publication(self):
        original = self.config.read_bytes()
        for kind in ('path', 'hardlink', 'symlink'):
            with self.subTest(kind=kind):
                alias = self.directory / 'alias'
                if kind == 'hardlink':
                    os.link(self.config, alias)
                elif kind == 'symlink':
                    alias.symlink_to(self.config)
                target = self.config if kind == 'path' else alias
                self.assert_error_before_capture(['--dimensions-file', self.config, '--output', target],
                                                 'overwrite input')
                self.assertEqual(self.config.read_bytes(), original)
                alias.unlink(missing_ok=True)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
