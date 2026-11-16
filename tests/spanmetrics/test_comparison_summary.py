# SPDX-License-Identifier: Apache-2.0
"""Summary output omits observed values without weakening comparison validation."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from otelwrench.spanmetrics import cli
from . import test_dimension_config as pairs
from . import test_comparison_input_sha256 as gates
from .test_jsonl import jsonl, split_requests


class ComparisonSummaryTests(unittest.TestCase):
    setUp = pairs.DimensionConfigTests.setUp
    save = pairs.DimensionConfigTests.save
    run_cli = gates.ComparisonInputSha256Tests.run_cli
    flags = gates.ComparisonInputSha256Tests.flags
    pair_options = gates.ComparisonInputSha256Tests.pair_options
    digest = gates.ComparisonInputSha256Tests.digest
    assert_pre_read_error = gates.ComparisonInputSha256Tests.assert_pre_read_error

    def assert_summary(self, full, summary):
        expected = copy.deepcopy(full)
        del expected['partitions']
        expected['summary_only'] = True
        expected['attribution'] = {'status': 'omitted', 'reason': 'summary_only'}
        expected['summary_omissions'] = ['partitions', 'validation_diagnostic_messages']
        for config in expected['configurations'].values():
            validation = config['validation']
            validation['diagnostics_omitted'] += len(validation['diagnostics'])
            validation['diagnostics'] = []
        self.assertEqual(summary, expected)
        self.assertNotIn('partitions', summary)

    def test_opt_in_summary_retains_hand_counts_and_resolved_definitions(self):
        for options in ([], self.flags(), self.pair_options(True)):
            with self.subTest(options=options):
                full = self.run_cli('--dry-run', *options)
                summary = self.run_cli('--dry-run', '--summary-only', *options)
                self.assertEqual(summary.returncode, 0, summary.stderr)
                self.assert_summary(json.loads(full.stdout), json.loads(summary.stdout))
                self.assertEqual(summary.stdout, self.run_cli('--dry-run', '--summary-only', *options).stdout)
                if options:
                    report = json.loads(summary.stdout)
                    self.assertEqual((report['unique_records'], report['series_delta']), (6, 24))
                    self.assertEqual([(report['configurations'][s]['groups'], report['configurations'][s]['series'])
                                      for s in ('baseline', 'candidate')], [(2, 16), (5, 40)])
                    for side in report['configurations'].values():
                        self.assertEqual(side['validation']['duplicate_records'], 1)

    def test_observed_values_omitted_including_mandatory_and_typed_labels(self):
        doc = json.loads(pairs.CAPTURE.read_bytes())
        marker = 'PRIVATE-observed-雪-"\\\n'
        for resource in doc['resourceSpans']:
            for attr in resource['resource']['attributes']:
                if 'stringValue' in attr['value']:
                    attr['value']['stringValue'] = marker
            for scope in resource['scopeSpans']:
                for span in scope['spans']:
                    span['name'] = marker
                    for attr in span['attributes']:
                        if 'stringValue' in attr['value']:
                            attr['value']['stringValue'] = marker
        capture = self.directory / 'private.json'
        capture.write_text(json.dumps(doc))
        full = self.run_cli('--dry-run', *self.flags(), source=capture)
        summary = self.run_cli('--dry-run', '--summary-only', *self.flags(), source=capture)
        self.assertEqual(summary.returncode, 0, summary.stderr)
        report = json.loads(summary.stdout)
        self.assert_summary(json.loads(full.stdout), report)
        # Search parsed strings, including all nested metadata, rather than encoded spelling.
        def strings(value):
            if isinstance(value, dict):
                return list(value) + [s for item in value.values() for s in strings(item)]
            if isinstance(value, list):
                return [s for item in value for s in strings(item)]
            return [value] if isinstance(value, str) else []
        self.assertTrue(any(marker == s for s in strings(json.loads(full.stdout))))
        self.assertFalse(any(marker in s for s in strings(report)))
        self.assertEqual(report['configurations']['candidate']['dimensions'],
                         json.loads(full.stdout)['configurations']['candidate']['dimensions'])

    def test_jsonl_file_stdin_gates_provenance_budget_and_publication(self):
        raw = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes())))
        capture = self.directory / 'capture.jsonl'
        capture.write_bytes(raw)
        for source in (capture, '-'):
            for named in (False, True):
                for maximum, code in ((24, 0), (23, 1)):
                    options = ['--input-format', 'jsonl', '--force', '--max-series-increase', maximum,
                               *self.pair_options(True)]
                    kw = dict(source=source, named=named, data=raw if source == '-' else None,
                              expected=self.digest(raw))
                    full = self.run_cli(*options, **kw)
                    summary = self.run_cli('--summary-only', *options, **kw)
                    self.assertEqual(summary.returncode, code, summary.stderr)
                    self.assert_summary(json.loads(full.stdout), json.loads(summary.stdout))
                    self.assertEqual(self.output.read_bytes(), summary.stdout)
                    report = json.loads(summary.stdout)
                    self.assertEqual(report['series_delta'], 24)
                    self.assertEqual(report['input_sha256'], self.digest(raw))
                    self.assertEqual(report['dimension_pair_provenance']['sha256'], self.digest(self.config.read_bytes()))
                    self.assertEqual(report['series_increase_budget']['status'], 'exceeded' if code else 'within_budget')
                    self.assertIn(b'budget exceeded', summary.stderr) if code else self.assertEqual(summary.stderr, b'')

    def test_empty_zero_and_negative_delta(self):
        for variant, delta in (('empty', 0), ('same', 0), ('reverse', -24)):
            pair = copy.deepcopy(pairs.PAIR)
            source = pairs.CAPTURE
            if variant == 'empty':
                source = self.directory / 'empty.json'
                source.write_bytes(b'{"resourceSpans":[]}')
            elif variant == 'same':
                pair['candidate'] = pair['baseline']
            else:
                pair['baseline'], pair['candidate'] = pair['candidate'], pair['baseline']
            self.save(pair)
            options = ['--dry-run', *self.pair_options(), '--max-series-increase', '0']
            full = self.run_cli(*options, source=source)
            summary = self.run_cli('--summary-only', *options, source=source)
            self.assertEqual(summary.returncode, 0, summary.stderr)
            self.assert_summary(json.loads(full.stdout), json.loads(summary.stdout))
            self.assertEqual(json.loads(summary.stdout)['series_delta'], delta)

    def test_late_rejection_and_caps_suppress_counts_but_keep_validation_totals(self):
        raw = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes())))
        for data, limits in ((raw + b'{\n', []), (raw, ['--max-input-bytes', '10']),
                             (raw, ['--max-line-bytes', '10']), (raw, ['--max-spans', '1']),
                             (raw, ['--max-series', '16'])):
            for dry in (False, True):
                options = ['--input-format', 'jsonl', '--force', *self.pair_options(True),
                           '--max-series-increase', '0', *limits, *(['--dry-run'] if dry else [])]
                full = self.run_cli(*options, source='-', data=data, expected=self.digest(data))
                summary = self.run_cli('--summary-only', *options, source='-', data=data, expected=self.digest(data))
                self.assertEqual(summary.returncode, 1, summary.stderr)
                report = json.loads(summary.stdout)
                self.assert_summary(json.loads(full.stdout), report)
                self.assertIsNone(report['series_delta'])
                self.assertIsNone(report['unique_records'])
                self.assertEqual(report['series_increase_budget']['status'], 'unavailable')
                for config in report['configurations'].values():
                    self.assertIsNone(config['groups'])
                    self.assertIsNone(config['series'])
                    if config['validation']['status'] == 'rejected':
                        self.assertGreater(config['validation']['diagnostics_omitted'], 0)
                    else:
                        self.assertEqual(config['validation']['diagnostics_omitted'], 0)
                if not dry:
                    self.assertEqual(self.output.read_bytes(), summary.stdout)

    def test_capture_controlled_diagnostic_text_is_withheld_and_counted_exactly(self):
        # A rejected JSON key may itself repeat an observed label/attribute value.
        marker = 'private-capture-value'
        raw = ('{"resourceSpans":[],"' + marker + '":0,"' + marker + '":1}').encode()
        capture = self.directory / 'bad.json'
        capture.write_bytes(raw)
        full = self.run_cli('--dry-run', source=capture)
        summary = self.run_cli('--summary-only', '--dry-run', source=capture)
        self.assertEqual(summary.returncode, 1, summary.stderr)
        self.assertIn(marker.encode(), full.stdout)
        self.assertNotIn(marker.encode(), summary.stdout + summary.stderr)
        self.assert_summary(json.loads(full.stdout), json.loads(summary.stdout))
        # More than the diagnostic message cap: no loss of exact diagnostic totals.
        doc = json.loads(pairs.CAPTURE.read_bytes())
        spans = doc['resourceSpans'][0]['scopeSpans'][0]['spans']
        bad = copy.deepcopy(spans[0]); bad['name'] = ''
        spans[:] = [copy.deepcopy(bad) for _ in range(105)]
        capture.write_text(json.dumps(doc))
        full = self.run_cli('--dry-run', source=capture)
        summary = self.run_cli('--summary-only', '--dry-run', source=capture)
        self.assertEqual(summary.returncode, 1, summary.stderr)
        self.assert_summary(json.loads(full.stdout), json.loads(summary.stdout))
        self.assertEqual(json.loads(summary.stdout)['configurations']['baseline']['validation']['diagnostics_omitted'], 105)

    def test_one_sided_validation_rejection_preserved(self):
        doc = json.loads(pairs.CAPTURE.read_bytes())
        for resource in doc['resourceSpans']:
            for scope in resource['scopeSpans']:
                for span in scope['spans']:
                    span['attributes'].append({'key': 'complex', 'value': {'arrayValue': {}}})
        capture = self.directory / 'complex.json'
        capture.write_text(json.dumps(doc))
        for side in ('baseline', 'candidate'):
            options = ['--dry-run', f'--{side}-dimension', 'span:complex=x']
            full = self.run_cli(*options, source=capture)
            summary = self.run_cli('--summary-only', *options, source=capture)
            self.assertEqual(summary.returncode, 1, summary.stderr)
            report = json.loads(summary.stdout)
            self.assert_summary(json.loads(full.stdout), report)
            self.assertEqual(report['configurations'][side]['validation']['status'], 'rejected')
            other = 'baseline' if side == 'candidate' else 'candidate'
            self.assertEqual(report['configurations'][other]['validation']['status'], 'ok')
            self.assertIsNone(report['configurations'][other]['series'])

    def test_exact_flag_rejects_partial_or_valued_options_before_reads(self):
        for option in ('--s', '--sum', '--summary', '--summary-onl', '--summary-onlyx',
                       '--summary-only=true', '--summary-only=false', '--summary-only=', '--no-summary-only'):
            self.assert_pre_read_error(['--dimensions-file', self.config, option], 'error:')
        # It is a switch: a following value is an unsupported argument, not a Boolean.
        self.assert_pre_read_error(['--dimensions-file', self.config, '--summary-only', 'true'], 'unrecognized arguments')
        for command in ([], ['validate-dimensions', '--dimensions-file', str(self.config)]):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(cli, 'read_dimension_pair', side_effect=AssertionError('pair read')), \
                 patch.object(cli, 'read_input', side_effect=AssertionError('capture read')), \
                 redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as exc:
                cli.main([*command, '--summary-only', *([] if command else [str(pairs.CAPTURE), '--output', str(self.output)])])
            self.assertEqual(exc.exception.code, 2)
            self.assertEqual(stdout.getvalue(), '')
            self.assertIn('unrecognized arguments', stderr.getvalue())
        full = self.run_cli('--dry', '--max-ser', '100', '--out=' + str(self.output))
        summary = self.run_cli('--summary-only', '--dry', '--max-ser', '100', '--out=' + str(self.output))
        self.assertEqual(summary.returncode, 0, summary.stderr)
        self.assert_summary(json.loads(full.stdout), json.loads(summary.stdout))

    def test_pair_and_capture_gate_precedence_no_publication(self):
        self.output.write_bytes(b'reviewed sentinel')
        for dry in (False, True):
            opts = ['--summary-only', '--force', '--max-series-increase', '0', *(['--dry-run'] if dry else [])]
            self.config.write_bytes(b'{')
            proc = self.run_cli(*opts, '--dimensions-file', self.config, source='missing', expected='0' * 64)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn(b'dimension configuration', proc.stderr)
            self.assertEqual(proc.stdout, b'')
            self.save(pairs.PAIR)
            proc = self.run_cli(*opts, '--dimensions-file', self.config, '--expect-dimension-sha256', '0' * 64,
                                source='missing', expected='0' * 64)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn(b'dimension configuration SHA-256 mismatch', proc.stderr)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(cli, 'compare', side_effect=AssertionError('compared before gate')), \
                 patch.object(cli, 'write_files', side_effect=AssertionError('published before gate')), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(['compare', str(pairs.CAPTURE), '--output', str(self.output),
                                 *map(str, opts), *map(str, self.pair_options(True)), '--expect-input-sha256', '0' * 64])
            self.assertEqual(code, 2, stderr.getvalue())
            self.assertEqual(stdout.getvalue(), '')
            self.assertIn('input SHA-256 mismatch', stderr.getvalue())
            self.assertEqual(self.output.read_bytes(), b'reviewed sentinel')

    def test_existing_force_dry_run_and_alias_protection(self):
        self.output.write_bytes(b'reviewed sentinel')
        proc = self.run_cli('--summary-only', *self.flags())
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(self.output.read_bytes(), b'reviewed sentinel')
        for maximum, code in ((23, 1), (24, 0)):
            proc = self.run_cli('--summary-only', '--dry-run', *self.flags(), '--max-series-increase', maximum)
            self.assertEqual(proc.returncode, code, proc.stderr)
            self.assertEqual(self.output.read_bytes(), b'reviewed sentinel')
        proc = self.run_cli('--summary-only', '--force', *self.flags(), '--max-series-increase', '23')
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.output.read_bytes(), proc.stdout)
        capture = self.directory / 'capture.json'
        capture.write_bytes(pairs.CAPTURE.read_bytes())
        for source in (self.config, capture):
            before = source.read_bytes()
            for kind in ('same', 'hardlink', 'symlink'):
                alias = source if kind == 'same' else self.directory / (kind + '.json')
                if kind == 'hardlink':
                    os.link(source, alias)
                elif kind == 'symlink':
                    alias.symlink_to(source)
                proc = self.run_cli('--summary-only', '--force', *self.pair_options(), '--output', alias, source=capture)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, b'')
                self.assertEqual(source.read_bytes(), before)
                if alias != source:
                    alias.unlink()
        raw = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes())))
        capture.write_bytes(raw)
        with capture.open('rb') as stream:
            proc = subprocess.run([*pairs.CLI, 'compare', '-', '--input-format', 'jsonl', '--summary-only',
                                   '--force', '--output', str(capture)], stdin=stream, capture_output=True,
                                  cwd=self.directory, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn(b'must not overwrite input', proc.stderr)
        self.assertEqual(capture.read_bytes(), raw)
        self.assertFalse(any(p.name.startswith('.spanmetrics-') for p in self.directory.iterdir()))
