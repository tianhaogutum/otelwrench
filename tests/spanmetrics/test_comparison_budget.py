# SPDX-License-Identifier: Apache-2.0
"""Optional CLI budget uses only complete capture-local comparison counts."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from otelwrench.spanmetrics import cli
from otelwrench.spanmetrics.comparison import compare
from otelwrench.spanmetrics.core import Config, parse_dimensions
from . import test_dimension_config as pairs
from . import test_comparison_input_sha256 as input_gates
from .test_jsonl import jsonl, split_requests


class ComparisonBudgetTests(unittest.TestCase):
    setUp = pairs.DimensionConfigTests.setUp
    save = pairs.DimensionConfigTests.save
    run_cli = input_gates.ComparisonInputSha256Tests.run_cli
    flags = input_gates.ComparisonInputSha256Tests.flags
    pair_options = input_gates.ComparisonInputSha256Tests.pair_options
    digest = input_gates.ComparisonInputSha256Tests.digest
    assert_pre_read_error = input_gates.ComparisonInputSha256Tests.assert_pre_read_error
    assert_suppressed = input_gates.ComparisonInputSha256Tests.assert_suppressed

    def budget(self, report, maximum, status):
        self.assertEqual(report['series_increase_budget'],
                         {'max_series_increase': maximum, 'status': status})

    def test_inclusive_threshold_and_exceeded_preserve_actual_counts(self):
        for maximum, status, code in ((24, 'within_budget', 0), (23, 'exceeded', 1),
                                      (25, 'within_budget', 0), (0, 'exceeded', 1),
                                      (2**63 - 1, 'within_budget', 0)):
            for options in (self.flags(), self.pair_options(True)):
                with self.subTest(maximum=maximum, options=options):
                    proc = self.run_cli('--max-series-increase', maximum, '--force', *options)
                    self.assertEqual(proc.returncode, code, proc.stderr)
                    report = json.loads(proc.stdout)
                    self.budget(report, maximum, status)
                    self.assertEqual(report['status'], 'ok')
                    self.assertTrue(report['comparison_complete'])
                    self.assertEqual(report['series_delta'], 24)
                    self.assertEqual([report['configurations'][s]['series'] for s in ('baseline', 'candidate')],
                                     [16, 40])
                    self.assertEqual([p['series_delta'] for p in report['partitions']], [16, 8])
                    self.assertEqual(self.output.read_bytes(), proc.stdout)
                    if code:
                        self.assertIn(b'series increase budget exceeded: series_delta 24 > 23' if maximum == 23
                                      else b'series increase budget exceeded: series_delta 24 > 0', proc.stderr)
                        self.assertNotIn(b'counts unavailable', proc.stderr)
                    else:
                        self.assertEqual(proc.stderr, b'')

    def test_zero_negative_and_empty_comparisons(self):
        for variant, delta in (('same', 0), ('reverse', -24), ('empty', 0)):
            pair = copy.deepcopy(pairs.PAIR)
            if variant == 'same':
                pair['candidate'] = pair['baseline']
            elif variant == 'reverse':
                pair['baseline'], pair['candidate'] = pair['candidate'], pair['baseline']
            self.save(pair)
            source = pairs.CAPTURE
            if variant == 'empty':
                source = self.directory / 'empty.json'
                source.write_bytes(b'{"resourceSpans":[]}')
            proc = self.run_cli('--max-series-increase=000', '--dry-run', *self.pair_options(), source=source)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report['series_delta'], delta)
            self.budget(report, 0, 'within_budget')
            self.assertFalse(self.output.exists())

    def test_invalid_incomplete_abbreviated_values_before_any_reads(self):
        for value in ('', '-1', '+1', ' 1', '1 ', '1\n', '1.0', '1e1', '1_0', '０', '١',
                      str(2**63), '9' * 5000):
            with self.subTest(value=value[:30]):
                self.assert_pre_read_error(['--dimensions-file', self.config, '--max-series-increase=' + value],
                                           'expected nonnegative integer <= 2**63-1')
        self.assert_pre_read_error(['--max-series-increase'], 'expected one argument')
        self.assert_pre_read_error(['--max-series-increase', '--dry-run'], 'expected one argument')
        for option in ('--max-series-', '--max-series-i', '--max-series-increas'):
            for spelling in ([option, '0'], [option + '=0']):
                self.assert_pre_read_error(spelling, 'unrecognized arguments')

    def test_existing_series_cap_abbreviations_and_defaults_remain(self):
        default = self.run_cli('--dry-run', *self.flags(), '--max-series', '100')
        self.assertEqual(default.returncode, 0, default.stderr)
        for option in ('--max-ser', '--max-seri', '--max-serie', '--max-series'):
            for spelling in ([option, '100'], [option + '=100']):
                proc = self.run_cli('--dry-run', *self.flags(), *spelling)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, default.stdout)
        self.assertNotIn('series_increase_budget', json.loads(default.stdout))
        result = compare(pairs.CAPTURE.read_bytes(),
                         Config(dimensions=parse_dimensions(['span:method=method'])),
                         Config(dimensions=parse_dimensions(['span:method=method', 'resource:zone=zone',
                                                             'span:customer=customer'])))
        report = json.loads(self.run_cli('--dry-run', *self.flags()).stdout)
        for key in ('dry_run', 'input', 'output'):
            report.pop(key)
        self.assertEqual(report, result)

    def test_jsonl_file_stdin_gates_provenance_and_final_delta(self):
        raw = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes())))
        capture = self.directory / 'capture.jsonl'
        capture.write_bytes(raw)
        for source in (capture, '-'):
            for named in (False, True):
                for maximum, code in ((24, 0), (23, 1)):
                    with self.subTest(source=source, named=named, maximum=maximum):
                        proc = self.run_cli('--input-format', 'jsonl', '--force', '--max-series-increase', maximum,
                                            *self.pair_options(True), source=source, named=named,
                                            data=raw if source == '-' else None, expected=self.digest(raw))
                        self.assertEqual(proc.returncode, code, proc.stderr)
                        report = json.loads(proc.stdout)
                        self.budget(report, maximum, 'exceeded' if code else 'within_budget')
                        self.assertEqual(report['series_delta'], 24)
                        self.assertEqual(report['input_sha256'], self.digest(raw))
                        self.assertEqual(report['dimension_pair_provenance'],
                                         {'schema_version': 1, 'sha256': self.digest(self.config.read_bytes())})
                        self.assertEqual(self.output.read_bytes(), proc.stdout)

    def test_rejections_always_unavailable_including_late_jsonl_and_caps(self):
        raw = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes())))
        for data, options in ((raw + b'{\n', []), (raw, ['--max-input-bytes', '10']),
                              (raw, ['--max-line-bytes', '10']), (raw, ['--max-spans', '1']),
                              (raw, ['--max-series', '16'])):
            for maximum in (0, 2**63 - 1):
                for dry in (False, True):
                    proc = self.run_cli('--input-format', 'jsonl', '--force', *self.pair_options(True),
                                        '--max-series-increase', maximum, *options, *(['--dry-run'] if dry else []),
                                        source='-', data=data, expected=self.digest(data))
                    self.assertEqual(proc.returncode, 1, proc.stderr)
                    report = json.loads(proc.stdout)
                    self.assert_suppressed(report)
                    self.assertEqual(report['status'], 'rejected')
                    self.budget(report, maximum, 'unavailable')
                    self.assertIn(b'comparison rejected; counts unavailable', proc.stderr)
                    self.assertNotIn(b'budget exceeded', proc.stderr)
                    if not dry:
                        self.assertEqual(self.output.read_bytes(), proc.stdout)
        # Reject only baseline, then only candidate, by selecting a complex attribute.
        doc = json.loads(pairs.CAPTURE.read_bytes())
        for resource in doc['resourceSpans']:
            for scope in resource['scopeSpans']:
                for span in scope['spans']:
                    span['attributes'].append({'key': 'complex', 'value': {'arrayValue': {}}})
        capture = self.directory / 'complex.json'
        capture.write_text(json.dumps(doc))
        for side in ('baseline', 'candidate'):
            proc = self.run_cli('--dry-run', '--max-series-increase', '0',
                                f'--{side}-dimension', 'span:complex=x', source=capture)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            report = json.loads(proc.stdout)
            self.assert_suppressed(report)
            self.budget(report, 0, 'unavailable')
            self.assertEqual(report['configurations'][side]['validation']['status'], 'rejected')
            other = 'candidate' if side == 'baseline' else 'baseline'
            self.assertEqual(report['configurations'][other]['validation']['status'], 'ok')

    def test_pair_and_capture_gates_precede_comparison_and_budget(self):
        self.output.write_bytes(b'reviewed comparison')
        for dry in (False, True):
            self.config.write_bytes(b'{')
            for expected_pair, diagnostic in ((None, b'dimension configuration'), ('0' * 64, b'dimension configuration')):
                proc = self.run_cli('--force', '--max-series-increase', '0', '--dimensions-file', self.config,
                                    *([] if expected_pair is None else ['--expect-dimension-sha256', expected_pair]),
                                    *(['--dry-run'] if dry else []), source='missing', expected='0' * 64)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, b'')
                self.assertIn(diagnostic, proc.stderr)
            self.save(pairs.PAIR)
            proc = self.run_cli('--force', '--max-series-increase', '0', '--dimensions-file', self.config,
                                '--expect-dimension-sha256', '0' * 64, *(['--dry-run'] if dry else []),
                                source='missing', expected='0' * 64)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn(b'dimension configuration SHA-256 mismatch', proc.stderr)
            self.assertEqual(proc.stdout, b'')
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(cli, 'compare', side_effect=AssertionError('compared before gate')), \
                 patch.object(cli, 'write_files', side_effect=AssertionError('published before gate')), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(['compare', str(pairs.CAPTURE), '--output', str(self.output), '--force',
                                 '--max-series-increase', '0', '--expect-input-sha256', '0' * 64,
                                 *map(str, self.pair_options(True)), *(['--dry-run'] if dry else [])])
            self.assertEqual(code, 2, stderr.getvalue())
            self.assertEqual(stdout.getvalue(), '')
            self.assertIn('input SHA-256 mismatch', stderr.getvalue())
            self.assertEqual(self.output.read_bytes(), b'reviewed comparison')

    def test_dry_run_existing_output_and_force_exceeded_publication(self):
        self.output.write_bytes(b'reviewed comparison')
        proc = self.run_cli('--max-series-increase', '23', *self.flags())
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, b'')
        self.assertEqual(self.output.read_bytes(), b'reviewed comparison')
        for maximum, code in ((23, 1), (24, 0)):
            for force in (False, True):
                proc = self.run_cli('--dry-run', '--max-series-increase', maximum, *self.flags(),
                                    *(['--force'] if force else []))
                self.assertEqual(proc.returncode, code, proc.stderr)
                self.assertEqual(self.output.read_bytes(), b'reviewed comparison')
        proc = self.run_cli('--force', '--max-series-increase', '23', *self.flags())
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.output.read_bytes(), proc.stdout)
        self.budget(json.loads(proc.stdout), 23, 'exceeded')
        self.assertFalse(any(p.name.startswith('.spanmetrics-') for p in self.directory.iterdir()))

    def test_alias_preflight_protects_pair_capture_and_redirected_stdin(self):
        capture = self.directory / 'capture.json'
        capture.write_bytes(pairs.CAPTURE.read_bytes())
        for source in (self.config, capture):
            for kind in ('same', 'hardlink', 'symlink'):
                alias = source if kind == 'same' else self.directory / (kind + '.json')
                if kind == 'hardlink':
                    os.link(source, alias)
                elif kind == 'symlink':
                    alias.symlink_to(source)
                before = source.read_bytes()
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(cli, 'read_input', side_effect=AssertionError('read before alias preflight')), \
                     redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli.main(['compare', str(capture), '--dimensions-file', str(self.config),
                                     '--output', str(alias), '--force', '--max-series-increase', '0'])
                self.assertEqual(code, 2, stderr.getvalue())
                self.assertEqual(stdout.getvalue(), '')
                self.assertEqual(source.read_bytes(), before)
                if kind != 'same':
                    alias.unlink()
        with capture.open('rb') as stdin:
            proc = subprocess.run([*pairs.CLI, 'compare', '-', '--input-format', 'jsonl',
                                   '--output', str(capture), '--force', '--max-series-increase', '0'],
                                  stdin=stdin, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn(b'must not overwrite input', proc.stderr)
        self.assertEqual(capture.read_bytes(), pairs.CAPTURE.read_bytes())

    def test_conversion_and_inspection_do_not_accept_budget(self):
        for argv in ([str(pairs.CAPTURE), '--output', str(self.output)],
                     ['validate-dimensions', '--dimensions-file', str(self.config)]):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(cli, 'read_input', side_effect=AssertionError('capture read')), \
                 patch.object(cli, 'read_dimension_pair', side_effect=AssertionError('pair read')), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as exc:
                    cli.main([*argv, '--max-series-increase', '0'])
            self.assertEqual(exc.exception.code, 2)
            self.assertEqual(stdout.getvalue(), '')
            self.assertIn('unrecognized arguments', stderr.getvalue())
