# SPDX-License-Identifier: Apache-2.0
"""Forbidden removals retain text and expose the first candidate's exact scope."""

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


def evidence(name, index, label, scope):
    return dict(code='protected_target_label_removal', candidate_name=name,
                candidate_index=index, label=label, scope_metric_names=scope)


class ProtectedRemovalDiagnosticTests(unittest.TestCase):
    def scrapes(self):
        # Context and exposition jointly provide effective pre-drop coverage.
        return [IdentifiedScrape('a', 'source', TIME, parse(
            'z{instance="a",debug="d"} 1\nx{instance="a",debug="d"} 2\n'
            'y{instance="a",debug="d"} 3\n'), {'job': 'api'})]

    def rejected(self, scrapes, plans, message, details, **options):
        with patch.object(analysis, 'summarize', side_effect=AssertionError('summarized')), \
                patch.object(analysis, '_analyze_validated', side_effect=AssertionError('projected')), \
                patch.object(analysis, '_union_plan', side_effect=AssertionError('union projected')):
            with self.assertRaises(InputError) as raised:
                analysis.report_scrapes(scrapes, plans, **options)
        exc = raised.exception
        self.assertIsInstance(exc, ValueError)
        self.assertEqual((str(exc), exc.args, exc.details), (message, (message,), details))

    def test_late_named_v2_evidence_precedes_all_summary_projection_and_ranking(self):
        plans = [['debug'], ['job', 'instance']]
        scopes = [['x'], ['z', 'y']]
        names = ['Debug', 'Late "β" identity']
        for ranked in (False, True):
            with self.subTest(ranked=ranked):
                self.rejected(self.scrapes(), plans,
                              f'candidate {names[1]!r}: cannot remove protected target label \'instance\'',
                              evidence(names[1], 2, 'instance', ['y', 'z']),
                              plan_names=names, plan_metrics=scopes, rank_plans=ranked,
                              protected_target_labels=['job', 'instance'])
        self.assertEqual((plans, scopes, names),
                         ([['debug'], ['job', 'instance']], [['x'], ['z', 'y']],
                          ['Debug', 'Late "β" identity']))

    def test_first_violation_uses_input_order_sorted_labels_and_resolved_scope(self):
        for ranked in (False, True):
            for reverse_labels in (False, True):
                labels = ['job', 'instance'] if reverse_labels else ['instance', 'job']
                for first, second, expected_label in [(labels, ['job'], 'instance'),
                                                       (['job'], labels, 'job')]:
                    with self.subTest(ranked=ranked, first=first, second=second):
                        self.rejected(self.scrapes(), [['debug'], first, second],
                                      f"candidate 'Z first': cannot remove protected target label {expected_label!r}",
                                      evidence('Z first', 2, expected_label, ['x', 'y', 'z']),
                                      plan_names=['Safe', 'Z first', 'A later'], rank_plans=ranked,
                                      protected_target_labels=labels)
            self.rejected(self.scrapes(), [['debug'], ['instance']],
                          "candidate 'plan-2': cannot remove protected target label 'instance'",
                          evidence('plan-2', 2, 'instance', ['y', 'z']),
                          metrics=['z', 'y'], rank_plans=ranked, protected_target_labels=['instance'])

    def test_validation_and_coverage_errors_keep_precedence_and_original_evidence(self):
        generic_cases = [
            ([['job'], ['debug']], dict(plan_metrics=[['x'], ['missing']]),
             "requested exact metric 'missing' is invalid or absent"),
            ([['job'], ['absent']], {}, 'requested label(s) absent from selected metrics: absent'),
            ([['job']], dict(plan_names=[' bad']),
             'candidate name must be 1–64 printable characters without surrounding whitespace'),
            ([['job'], ['debug']], dict(plan_names=['Same', 'Same']), "duplicate candidate name 'Same'"),
        ]
        for ranked in (False, True):
            for plans, options, message in generic_cases:
                with self.subTest(ranked=ranked, options=options, plans=plans):
                    self.rejected(self.scrapes(), plans, message, None,
                                  protected_target_labels=['job'], rank_plans=ranked, **options)
            for raw, reason in [('y{debug="d"} 1\n', 'absent'),
                                ('y{instance="",debug="d"} 1\n', 'empty')]:
                late = IdentifiedScrape('late', 'source', TIME, parse(raw), {'job': 'api'})
                self.rejected([*self.scrapes(), late], [['instance']],
                              "scrape 'late', line 1: required target label 'instance' is absent or empty",
                              dict(code='target_label_coverage', scrape_id='late', line=1,
                                   label='instance', reason=reason),
                              protected_target_labels=['instance'], rank_plans=ranked)
        conflict = IdentifiedScrape('conflict', 'source', TIME, parse('x{job="other"} 1\n'), {'job': 'api'})
        self.rejected([conflict], [['job']],
                      "scrape 'conflict', line 1: target label 'job' conflicts with exposition",
                      dict(code='target_label_conflict', scrape_id='conflict', line=1,
                           label='job', exposition_value='other', target_value='api'),
                      protected_target_labels=['job'])

    def test_cli_inline_v1_v2_exact_json_and_text_with_shared_and_default_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'capture.prom').write_text(
                'z{instance="a",debug="d"} 1\nx{instance="a",debug="d"} 2\ny{instance="a",debug="d"} 3\n')
            manifest = root / 'set.json'
            manifest.write_text(json.dumps(dict(schema_version=1, scrapes=[dict(
                id='a', source='source', captured_at=TIME, path='capture.prom', target_labels={'job': 'api'})])))
            candidates = root / 'candidates.json'
            for kind in ('inline', 'v1', 'v2'):
                for scoped in (False, True) if kind != 'v2' else (True,):
                    name = 'plan-2' if kind == 'inline' else 'Late "β" identity'
                    if kind == 'inline':
                        options = ['--drop-labels', 'debug', '--drop-labels', 'job,instance']
                    else:
                        entries = [dict(name='Debug', drop_labels=['debug']),
                                   dict(name=name, drop_labels=['job', 'instance'])]
                        if kind == 'v2':
                            entries[0]['metrics'] = ['x']
                            entries[1]['metrics'] = ['z', 'y']
                        candidates.write_text(json.dumps(dict(schema_version=int(kind[1]), candidates=entries)))
                        options = ['--candidate-file', str(candidates)]
                    if scoped and kind != 'v2':
                        options += ['--metric', 'z', '--metric', 'y']
                    scope = ['y', 'z'] if scoped else ['x', 'y', 'z']
                    message = f"candidate {name!r}: cannot remove protected target label 'instance'"
                    for ranked in (False, True):
                        args = [*CLI, '--scrape-set', str(manifest), *options,
                                '--protect-target-label', 'job', '--protect-target-label', 'instance']
                        if ranked:
                            args.append('--rank-plans')
                        with self.subTest(kind=kind, scoped=scoped, ranked=ranked):
                            proc = subprocess.run([*args, '--format', 'json'], cwd='/', capture_output=True, timeout=60)
                            self.assertEqual((proc.returncode, proc.stdout), (2, b''), proc.stderr)
                            self.assertEqual(json.loads(proc.stderr), dict(
                                error=message, error_type='invalid_or_unsupported_input',
                                details=evidence(name, 2, 'instance', scope)))
                            proc = subprocess.run(args, cwd='/', capture_output=True, timeout=60)
                            self.assertEqual((proc.returncode, proc.stdout, proc.stderr),
                                             (2, b'', f'otelwrench cardinality: {message}\n'.encode()))


if __name__ == '__main__':
    unittest.main()
