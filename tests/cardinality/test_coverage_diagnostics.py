# SPDX-License-Identifier: Apache-2.0
"""Coverage gaps expose deterministic evidence without changing text diagnostics."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from otelwrench.cardinality import analysis
from otelwrench.cardinality.parser import InputError, parse
from otelwrench.cardinality.scrapes import IdentifiedScrape

CLI = [sys.executable, "-m", "otelwrench", "cardinality"]
TIME = '2026-11-11T08:00:00Z'


def capture(id, text, context=None):
    return IdentifiedScrape(id, 'job=api instance=provenance', TIME, parse(text), context or {})


def evidence(id, line, label, reason):
    return dict(code='target_label_coverage', scrape_id=id, line=line, label=label, reason=reason)


class CoverageDiagnosticTests(unittest.TestCase):
    def cli(self, *args):
        return subprocess.run([*CLI, *map(str, args)], cwd='/', capture_output=True, timeout=60)

    def manifest(self, directory, scrapes):
        entries = []
        for i, item in enumerate(scrapes):
            path = Path(directory) / f'{i}.prom'
            # Reconstruct these scalar fixtures without changing original sample line numbers.
            lines = []
            for sample in item.scrape.samples:
                while len(lines) < sample.line - 1:
                    lines.append('# capture')
                labels = ','.join(f'{k}={json.dumps(v)}' for k, v in sample.labels)
                lines.append(f'{sample.metric}{{{labels}}} {sample.value}')
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            entries.append(dict(id=item.id, source=item.source, captured_at=TIME,
                                path=path.name, target_labels=item.target_labels))
        path = Path(directory) / 'set.json'
        path.write_text(json.dumps(dict(schema_version=1, scrapes=entries)), encoding='utf-8')
        return path

    def cases(self):
        good = capture('first', 'x{job="api",instance="a",debug="d"} 1\n')
        for origin, raw, context, reason in [
                ('absent', 'y{job="api"} 2\n', {}, 'absent'),
                ('exposition', 'y{job="api",instance=""} 2\n', {}, 'empty'),
                ('context', 'y{job="api"} 2\n', {'instance': ''}, 'empty'),
                ('equal-overlap', 'y{job="api",instance=""} 2\n', {'instance': ''}, 'empty')]:
            yield origin, [good, capture('late "β"', '# capture\n# more\n' + raw, context)], reason

    def test_library_late_unselected_gap_evidence_and_atomicity(self):
        for origin, scrapes, reason in self.cases():
            for policy in (dict(required_target_labels=['instance']),
                           dict(protected_target_labels=['instance']),
                           dict(required_target_labels=['job', 'instance'], protected_target_labels=['instance'])):
                for ranked in (False, True):
                    with self.subTest(origin=origin, policy=policy, ranked=ranked):
                        with patch.object(analysis, '_analyze_validated', side_effect=AssertionError('projected')), \
                                patch.object(analysis, 'summarize', side_effect=AssertionError('summarized')):
                            with self.assertRaises(InputError) as raised:
                                analysis.report_scrapes(scrapes, [['debug']], plan_names=['Only x'],
                                                        plan_metrics=[['x']], rank_plans=ranked, **policy)
                        exc = raised.exception
                        message = 'scrape \'late "β"\', line 3: required target label \'instance\' is absent or empty'
                        self.assertIsInstance(exc, ValueError)
                        self.assertEqual((str(exc), exc.args), (message, (message,)))
                        self.assertEqual(getattr(exc, 'details', None), evidence('late "β"', 3, 'instance', reason))

    def test_cli_json_evidence_baseline_and_named_v2_ranking_text_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            candidates = Path(directory) / 'candidates.json'
            candidates.write_text(json.dumps(dict(schema_version=2, candidates=[
                dict(name='Only x', drop_labels=['debug'], metrics=['x'])])))
            for origin, scrapes, reason in self.cases():
                path = self.manifest(directory, scrapes)
                for flag in ('--require-target-label', '--protect-target-label'):
                    for options in ([], ['--candidate-file', candidates, '--rank-plans']):
                        with self.subTest(origin=origin, flag=flag, options=options):
                            args = ['--scrape-set', path, flag, 'instance', *options]
                            proc = self.cli(*args, '--format', 'json')
                            self.assertEqual((proc.returncode, proc.stdout), (2, b''), proc.stderr)
                            message = 'scrape \'late "β"\', line 3: required target label \'instance\' is absent or empty'
                            self.assertEqual(json.loads(proc.stderr), dict(
                                error=message, error_type='invalid_or_unsupported_input',
                                details=evidence('late "β"', 3, 'instance', reason)))
                            text = self.cli(*args)
                            self.assertEqual((text.returncode, text.stdout, text.stderr),
                                             (2, b'', f'otelwrench cardinality: {message}\n'.encode()))

    def test_first_gap_uses_capture_sample_and_sorted_union_order(self):
        # Empty values are canonically absent, but evidence must retain "empty".
        first = capture('first', '# header\nx{job="api",instance=" ",debug="d"} 1\n'
                        'y{job="",instance=""} 2\nz 3\n')
        late = capture('late', 'x 1\n')
        policies = [dict(required_target_labels=['job'], protected_target_labels=['job', 'instance']),
                    dict(required_target_labels=['job', 'instance'], protected_target_labels=['instance'])]
        for scrapes, expected in [([first, late], evidence('first', 3, 'instance', 'empty')),
                                  ([late, first], evidence('late', 1, 'instance', 'absent'))]:
            for policy in policies:
                with self.subTest(expected=expected, policy=policy), self.assertRaises(InputError) as raised:
                    analysis.report_scrapes(scrapes, **policy)
                self.assertEqual(getattr(raised.exception, 'details', None), expected)
        # The whitespace value above supplies coverage; context/exposition mix works.
        good = capture('good', 'x{instance=" ",debug="d"} 1\n', {'job': 'api'})
        plain = analysis.report_scrapes([good], [['instance']])
        required = analysis.report_scrapes([good], [['instance']], required_target_labels=['instance'])
        self.assertEqual({k: v for k, v in plain.items() if k != 'required_target_labels'},
                         {k: v for k, v in required.items() if k != 'required_target_labels'})
        self.assertTrue(required['plans'][0]['safe'])

    def test_generic_errors_keep_original_json_shape_and_library_default(self):
        exc = InputError('generic')
        self.assertEqual((str(exc), exc.args, getattr(exc, 'details', 'missing')), ('generic', ('generic',), None))
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(directory, [capture('a', 'x{job="api",instance="a"} 1\n')])
            for options, message in [
                    (['--protect-target-label', '__name__'], "invalid or protected protected target label '__name__'"),
                    (['--require-target-label', 'job', '--require-target-label', 'job'], "duplicate required target label 'job'"),
                    (['--protect-target-label', 'job', '--drop-labels', 'absent'], 'requested label(s) absent from selected metrics: absent'),
                    (['--metric', 'x'], '--metric requires a plan')]:
                with self.subTest(options=options):
                    proc = self.cli('--scrape-set', path, *options, '--format', 'json')
                    self.assertEqual((proc.returncode, proc.stdout), (2, b''), proc.stderr)
                    self.assertEqual(json.loads(proc.stderr), dict(error=message, error_type='invalid_or_unsupported_input'))
            path = self.manifest(directory, [capture('a', 'x{job="api"} 1\n', {'job': 'other'})])
            proc = self.cli('--scrape-set', path, '--protect-target-label', 'instance', '--format', 'json')
            self.assertEqual((proc.returncode, proc.stdout), (2, b''))
            self.assertEqual(json.loads(proc.stderr), dict(error="scrape 'a', line 1: target label 'job' conflicts with exposition",
                                                         error_type='invalid_or_unsupported_input',
                                                         details=dict(code='target_label_conflict', scrape_id='a', line=1,
                                                                      label='job', exposition_value='api', target_value='other')))


if __name__ == '__main__':
    unittest.main()
