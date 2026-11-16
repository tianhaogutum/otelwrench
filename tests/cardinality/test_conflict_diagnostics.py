# SPDX-License-Identifier: Apache-2.0
"""Explicit unequal overlap exposes decoded evidence in existing traversal order."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from otelwrench.cardinality import analysis
from otelwrench.cardinality.parser import InputError, parse
from otelwrench.cardinality.scrapes import IdentifiedScrape, contextualize, load_scrape_set

CLI = [sys.executable, "-m", "otelwrench", "cardinality"]
TIME = '2026-11-11T08:00:00Z'
ID = 'late "β"\\capture'


def capture(id, raw, context):
    return IdentifiedScrape(id, 'provenance', TIME, parse(raw), context)


def quoted(value):
    return json.dumps(value, ensure_ascii=False)


def message(id, line, label):
    return f'scrape {id!r}, line {line}: target label {label!r} conflicts with exposition'


def evidence(id, line, label, raw, target):
    return dict(code='target_label_conflict', scrape_id=id, line=line, label=label,
                exposition_value=raw, target_value=target)


class ConflictDiagnosticTests(unittest.TestCase):
    def cases(self):
        good = capture('first', 'x{debug="d",job="api"} 1\n', {'job': 'api'})
        for raw, target in [('β "raw"\n\\path', '目標 "target"\n\\other'), ('', 'nonempty'), ('nonempty', '')]:
            late = capture(ID, '# capture\nx{debug="d"} 2\ny{job=' + quoted(raw) + '} 3\n', {'job': target})
            yield [good, late], raw, target

    def rejected(self, scrapes, expected, **options):
        with patch.object(analysis, 'summarize', side_effect=AssertionError('summarized')), \
                patch.object(analysis, '_analyze_validated', side_effect=AssertionError('projected')), \
                patch.object(analysis, '_union_plan', side_effect=AssertionError('union projected')), \
                patch.object(analysis, '_rank_safe_plans', side_effect=AssertionError('ranked')):
            with self.assertRaises(InputError) as raised:
                analysis.report_scrapes(scrapes, **options)
        exc = raised.exception
        self.assertIsInstance(exc, ValueError)
        self.assertEqual((str(exc), exc.args), (message(expected['scrape_id'], expected['line'], expected['label']),
                                               (message(expected['scrape_id'], expected['line'], expected['label']),)))
        self.assertEqual(exc.details, expected)

    def test_late_unselected_conflict_is_atomic_and_values_are_decoded(self):
        for scrapes, raw, target in self.cases():
            before = [dict(sample.labels) for item in scrapes for sample in item.scrape.samples]
            for ranked in (False, True):
                with self.subTest(raw=raw, target=target, ranked=ranked):
                    self.rejected(scrapes, evidence(ID, 3, 'job', raw, target), plans=[['debug']],
                                  plan_names=['Only x'], plan_metrics=[['x']], rank_plans=ranked,
                                  required_target_labels=['missing'], protected_target_labels=['debug'])
            with self.assertRaises(InputError) as raised:
                contextualize(scrapes[-1])
            self.assertEqual(raised.exception.details, evidence(ID, 3, 'job', raw, target))
            self.assertEqual([dict(sample.labels) for item in scrapes for sample in item.scrape.samples], before)

    def test_first_conflict_preserves_capture_sample_and_context_insertion_order(self):
        for context, label in [({'z': 'target-z', 'a': 'target-a'}, 'z'),
                               ({'a': 'target-a', 'z': 'target-z'}, 'a')]:
            first = capture('Z first', '# header\nx 1\ny{a="raw-a",z="raw-z"} 2\nz{a="later"} 3\n', context)
            later = capture('A later', 'x{a="later"} 1\n', {'a': 'target-a'})
            for ranked in (False, True):
                with self.subTest(label=label, ranked=ranked):
                    self.rejected([first, later], evidence('Z first', 3, label, 'raw-' + label, 'target-' + label),
                                  plans=[['a']], rank_plans=ranked)
                    self.rejected([later, first], evidence('A later', 1, 'a', 'later', 'target-a'),
                                  plans=[['a']], rank_plans=ranked)
            earlier = capture('sample order', 'z{z="early"} 1\ny{a="late"} 2\n', context)
            self.rejected([earlier], evidence('sample order', 1, 'z', 'early', 'target-z'))

    def manifest(self, root, scrapes):
        entries = []
        for i, item in enumerate(scrapes):
            lines = []
            for sample in item.scrape.samples:
                while len(lines) < sample.line - 1:
                    lines.append('# capture')
                labels = ','.join(f'{k}={quoted(v)}' for k, v in sample.labels)
                lines.append(f'{sample.metric}{{{labels}}} {sample.value}')
            path = root / f'{i}.prom'
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            entries.append(dict(id=item.id, source=item.source, captured_at=TIME,
                                path=path.name, target_labels=item.target_labels))
        manifest = root / 'set.json'
        manifest.write_text(json.dumps(dict(schema_version=1, scrapes=entries)), encoding='utf-8')
        return manifest

    def test_cli_baseline_inline_v1_v2_json_and_exact_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for scrapes, raw, target in self.cases():
                manifest = self.manifest(root, scrapes)
                with self.assertRaises(InputError) as raised:
                    load_scrape_set(manifest)
                self.assertEqual(raised.exception.details, evidence(ID, 3, 'job', raw, target))
                for kind in ('baseline', 'inline', 'v1', 'v2'):
                    if kind == 'baseline':
                        options = []
                    elif kind == 'inline':
                        options = ['--drop-labels', 'debug', '--metric', 'x']
                    else:
                        candidate = dict(name='Only x', drop_labels=['debug'])
                        if kind == 'v2':
                            candidate['metrics'] = ['x']
                        path = root / 'candidates.json'
                        path.write_text(json.dumps(dict(schema_version=int(kind[1]), candidates=[candidate])))
                        options = ['--candidate-file', str(path)]
                        if kind == 'v1':
                            options += ['--metric', 'x']
                    for ranked in (False, True) if kind != 'baseline' else (False,):
                        args = [*CLI, '--scrape-set', str(manifest), *options]
                        if ranked:
                            args.append('--rank-plans')
                        with self.subTest(raw=raw, target=target, kind=kind, ranked=ranked):
                            proc = subprocess.run([*args, '--format', 'json'], cwd='/', capture_output=True, timeout=60)
                            self.assertEqual((proc.returncode, proc.stdout), (2, b''), proc.stderr)
                            self.assertEqual(json.loads(proc.stderr), dict(
                                error=message(ID, 3, 'job'), error_type='invalid_or_unsupported_input',
                                details=evidence(ID, 3, 'job', raw, target)))
                            proc = subprocess.run(args, cwd='/', capture_output=True, timeout=60)
                            self.assertEqual((proc.returncode, proc.stdout, proc.stderr),
                                             (2, b'', f'otelwrench cardinality: {message(ID, 3, "job")}\n'.encode()))

    def test_malformed_context_and_duplicate_input_keep_precedence(self):
        cases = [
            (capture('a', 'x{job="raw"} 1\n', {'job': 'target', 'bad-name': 'x'}),
             "scrape 'a': invalid or protected target label 'bad-name'"),
            (capture('a', 'x{job="raw"} 1\n', {'job': 'target', 'later': 1}),
             "scrape 'a': target label 'later' requires a UTF-8 string without NUL/CR"),
            (capture('a', 'x{job="api"} 1\nx 2\ny{job="raw"} 3\n', {'job': 'api'}),
             "scrape 'a', line 2: duplicate input series after target labels; first seen on line 1"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for item, text in cases:
                with self.subTest(text=text):
                    with self.assertRaises(InputError) as raised:
                        analysis.report_scrapes([item], [['job']], protected_target_labels=['job'])
                    details = (dict(code='target_label_duplicate', scrape_id='a', line=2, first_seen_line=1,
                                    effective_key=dict(metric='x', labels={'job': 'api'}))
                               if 'duplicate input series' in text else None)
                    self.assertEqual((str(raised.exception), raised.exception.details), (text, details))
                    manifest = self.manifest(Path(directory), [item])
                    proc = subprocess.run([*CLI, '--scrape-set', str(manifest), '--format', 'json'],
                                          cwd='/', capture_output=True, timeout=60)
                    self.assertEqual((proc.returncode, proc.stdout), (2, b''))
                    expected = dict(error=text, error_type='invalid_or_unsupported_input')
                    if details is not None:
                        expected['details'] = details
                    self.assertEqual(json.loads(proc.stderr), expected)
        first_conflict = capture('a', 'x{job="raw"} 1\ny{job="api"} 2\ny 3\n', {'job': 'api'})
        self.rejected([first_conflict], evidence('a', 1, 'job', 'raw', 'api'))

    def test_equal_decoded_overlap_and_empty_or_omitted_context_preserve_full_reports(self):
        value = 'β "quoted"\n\\path'
        raw = 'x{job=' + quoted(value) + ',debug="a",zone=""} 1\nx{job=' + quoted(value) + ',debug="b"} 2\n'
        parsed = parse(raw)
        for ranked in (False, True):
            omitted = IdentifiedScrape('a', 'source', TIME, parsed)
            empty = IdentifiedScrape('a', 'source', TIME, parsed, {})
            self.assertEqual(analysis.report_scrapes([omitted], [['debug']], rank_plans=ranked),
                             analysis.report_scrapes([empty], [['debug']], rank_plans=ranked))
            # Equal overlap and absent empty labels are accepted; one effective job per sample.
            contextual = IdentifiedScrape('a', 'source', TIME, parsed, {'job': value, 'zone': ''})
            effective = contextualize(contextual)
            self.assertEqual([dict(s.labels) for s in effective.scrape.samples],
                             [dict(debug='a', job=value, zone=''), dict(debug='b', job=value, zone='')])
            report = analysis.report_scrapes([contextual], [['debug']], rank_plans=ranked)
            self.assertEqual(report['plans'][0]['label_pairs_removed'], 2)
            self.assertEqual([w['exposition_labels'] for w in report['plans'][0]['collisions'][0]['originals']],
                             [dict(s.labels) for s in parsed.samples])
            self.assertIs(contextual.scrape, parsed)


if __name__ == '__main__':
    unittest.main()
