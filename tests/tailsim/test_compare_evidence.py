"""Saved paired evidence through the CLI; fixture facts never use saved assertions."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from . import test_comparison_evidence as oracle

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'examples/tailsim/decision-matrix'
FIELDS = ('schema_version', 'mode', 'fingerprints', 'assumptions', 'byte_measure',
          'baseline', 'candidate', 'comparison')
SIDES = ('baseline', 'candidate')


class CompareEvidence(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source, self.baseline, self.candidate, self.saved = [self.root / name for name in
            ('traces.json', 'baseline.yaml', 'candidate.yaml', 'report.json')]
        for path in (self.source, self.baseline, self.candidate):
            path.write_bytes((FIXTURE / path.name).read_bytes())
        self.expected = json.loads((FIXTURE / 'expected.json').read_text())
        self.produce()

    def command(self, mode='verify-compare-evidence', *extra):
        cli = [os.environ['TAILSIM_TEST_CLI']] if 'TAILSIM_TEST_CLI' in os.environ else [sys.executable, '-m', 'otelwrench', 'tailsim']
        args = [*cli, mode, str(self.source), '--baseline', str(self.baseline), '--candidate', str(self.candidate)]
        if mode != 'compare':
            args += ['--report', str(self.saved)]
        return subprocess.run([*args, *extra], capture_output=True, text=True)

    def produce(self):
        result = self.command('compare')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.original = json.loads(result.stdout)
        self.saved.write_text(result.stdout)

    def save(self, value):
        self.saved.write_text(json.dumps(value, ensure_ascii=False))

    def check(self, mismatches=()):
        result = self.command()
        self.assertEqual(result.returncode, int(bool(mismatches)), result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertEqual(json.loads(result.stdout), {
            'schema_version': 1, 'mode': 'comparison_evidence_verification',
            'matches': not bool(mismatches),
            'checks': {key: {'matches': key not in mismatches} for key in FIELDS}})
        self.assertNotIn(str(self.root), result.stdout)

    def invalid(self, *extra):
        result = self.command('verify-compare-evidence', *extra)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_saved_comparison_agrees_with_independent_matrix_and_bytes(self):
        oracle.ComparisonEvidence.assert_matrix(self, self.original)
        # Independently filter raw OTLP by the hand-labelled expected decisions.
        def size(value):
            return len(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())
        raw = json.loads(self.source.read_bytes())
        sizes = {}
        for side in SIDES:
            ids = {row['trace_id'] for row in self.expected['traces'] if row[side]['decision'] == 'sample'}
            kept = copy.deepcopy(raw)
            for resource in kept['resourceSpans']:
                for scope in resource['scopeSpans']:
                    scope['spans'] = [span for span in scope['spans'] if span['traceId'].lower() in ids]
                resource['scopeSpans'] = [s for s in resource['scopeSpans'] if s['spans']]
            kept['resourceSpans'] = [r for r in kept['resourceSpans'] if r['scopeSpans']]
            sizes[side] = size(kept)
            summary = self.original[side]['summary']
            self.assertEqual(summary['serialized_kept_bytes'], sizes[side])
            self.assertEqual(summary['serialized_total_bytes'], size(raw))
            self.assertEqual(summary['serialized_removed_bytes'], size(raw) - sizes[side])
        self.assertEqual(self.original['comparison']['serialized_kept_byte_delta'], sizes['candidate'] - sizes['baseline'])
        self.check()

    def test_every_identity_and_evidence_field_independently_and_combined(self):
        changes = [
            ('assumptions', lambda r: r.update(assumptions={})),
            ('byte_measure', lambda r: r.update(byte_measure='claimed transport bytes')),
            ('comparison', lambda r: r['comparison'].update(serialized_kept_byte_delta=0)),
            ('comparison', lambda r: r['comparison']['changed_traces'][0].update(candidate_reason='invented')),
            ('comparison', lambda r: r['comparison']['changed_traces'][0]['candidate_policies'][0].update(matched=None)),
        ]
        for section in ('fingerprints', *SIDES):
            block = self.original['fingerprints'] if section == 'fingerprints' else self.original[section]['fingerprints']
            for key in block.keys() - {'version', 'algorithm'}:
                def mutate(r, section=section, key=key):
                    block = r['fingerprints'] if section == 'fingerprints' else r[section]['fingerprints']
                    block[key] = '0' * 64
                changes.append((section, mutate))
        for side in SIDES:
            changes += [
                (side, lambda r, s=side: r[s].update(assumptions={})),
                (side, lambda r, s=side: r[s].update(byte_measure='altered')),
                (side, lambda r, s=side: r[s]['summary'].update(kept_traces=-1)),
                (side, lambda r, s=side: r[s]['traces'][0].update(decision='invented')),
                (side, lambda r, s=side: r[s]['traces'][0]['policies'][0].update(reason='invented'))]
        for field, mutate in changes:
            changed = copy.deepcopy(self.original)
            mutate(changed)
            self.assertNotEqual(changed, self.original)
            self.save(changed)
            with self.subTest(field=field):
                self.check([field])
        changed = copy.deepcopy(self.original)
        for _, mutate in changes:
            mutate(changed)
        self.save(changed)
        self.check(['fingerprints', 'assumptions', 'byte_measure', *SIDES, 'comparison'])

    def test_nested_json_types_keys_and_array_order_are_significant(self):
        changes = [('comparison', lambda r: r['comparison']['changed_traces'].reverse()),
                   ('comparison', lambda r: r['comparison'].update(changed_traces=[None])),
                   ('comparison', lambda r: r['comparison'].pop('error_traces_gained')),
                   ('comparison', lambda r: r['comparison'].update(extra={'arbitrary': [True, 1, None]})),
                   ('assumptions', lambda r: r['assumptions'].update(production_savings_estimated=0))]
        for value in (True, 1.0, '1', None, []):
            changes.append(('comparison', lambda r, v=value: r['comparison'].update(newly_kept_traces=v)))
        for side in SIDES:
            changes += [
                (side, lambda r, s=side: r[s]['traces'].reverse()),
                (side, lambda r, s=side: r[s]['traces'][0].update(error=int(r[s]['traces'][0]['error']))),
                (side, lambda r, s=side: r[s]['traces'][0]['policies'][0].update(matched=int(r[s]['traces'][0]['policies'][0]['matched']))),
                (side, lambda r, s=side: r[s]['summary'].update(kept_traces=float(r[s]['summary']['kept_traces']))),
                (side, lambda r, s=side: r[s].update(traces=[None]))]
        for field, mutate in changes:
            changed = copy.deepcopy(self.original)
            mutate(changed)
            self.save(changed)
            with self.subTest(field=field):
                self.check([field])

    def test_object_order_unicode_equivalent_configs_equal_and_reverse(self):
        def reverse_keys(value):
            if isinstance(value, dict):
                return {k: reverse_keys(value[k]) for k in reversed(value)}
            if isinstance(value, list):
                return [reverse_keys(v) for v in value]
            return value
        self.baseline.write_text(self.baseline.read_text().replace('name: all', 'name: all-λ'))
        self.produce()
        self.saved.write_text(json.dumps(reverse_keys(self.original), ensure_ascii=True))
        self.baseline.write_text('{"processors":{"tail_sampling":{"policies":[{"always_sample":{},"type":"always_sample","name":"all-\\u03bb"}]}}}')
        self.check()
        self.baseline, self.candidate = self.candidate, self.baseline
        self.check(['fingerprints', 'baseline', 'candidate', 'comparison'])
        self.produce()
        # Restore the oracle's policy name before checking the reversed fixture.
        report = copy.deepcopy(self.original)
        for row in report['candidate']['traces']:
            row['matched_policy_names'] = ['all']
            row['policies'][0]['name'] = 'all'
        for row in report['comparison']['changed_traces']:
            row['candidate_matched_policy_names'] = ['all']
            row['candidate_policies'][0]['name'] = 'all'
        oracle.ComparisonEvidence.assert_matrix(self, report, reverse=True)
        self.check()
        self.candidate.write_bytes(self.baseline.read_bytes())
        self.produce()
        self.assertEqual(self.original['baseline'], self.original['candidate'])
        self.assertEqual(self.original['comparison']['changed_traces'], [])
        self.check()

    def test_supplied_inputs_empty_corpus_and_config_changes(self):
        self.source.write_bytes(self.source.read_bytes() + b'\n')
        self.check(['fingerprints', 'baseline', 'candidate'])
        self.source.write_bytes((FIXTURE / 'traces.json').read_bytes())
        self.baseline.write_bytes(self.candidate.read_bytes())
        self.check(['fingerprints', 'baseline', 'comparison'])
        self.baseline.write_bytes((FIXTURE / 'baseline.yaml').read_bytes())
        self.candidate.write_bytes(self.baseline.read_bytes())
        self.check(['fingerprints', 'candidate', 'comparison'])
        self.source.write_bytes(b'{"resourceSpans":[]}')
        self.produce()
        self.check()

    def test_wrapper_and_each_embedded_envelope_metadata_fail_atomically(self):
        for section in (None, *SIDES):
            original = self.original if section is None else self.original[section]
            variants = [None, [], {}]
            for key in original:
                variants.append({k: v for k, v in original.items() if k != key})
            for key, value in [('schema_version', True), ('schema_version', 1.0), ('schema_version', '1'),
                               ('schema_version', 2), ('mode', 'arrival_order_offline'), ('mode', None),
                               ('extra', 1), ('fingerprints', []), ('assumptions', []), ('byte_measure', {})]:
                variants.append({**original, key: value})
            for key, value in ([('comparison', []), ('baseline', None), ('candidate', {})] if section is None
                               else [('summary', []), ('traces', {})]):
                variants.append({**original, key: value})
            fp = original['fingerprints']
            for key in fp:
                variants.append({**original, 'fingerprints': {k: v for k, v in fp.items() if k != key}})
            for key, value in [('version', True), ('version', 1.0), ('version', '1'), ('version', 2),
                               ('algorithm', 'SHA256'), ('algorithm', 'sha512'), ('extra', 1)]:
                variants.append({**original, 'fingerprints': {**fp, key: value}})
            for key in fp.keys() - {'version', 'algorithm'}:
                for value in ['A' * 64, 'g' * 64, '0' * 63, None, 1, True]:
                    variants.append({**original, 'fingerprints': {**fp, key: value}})
            for i, variant in enumerate(variants):
                self.save(variant if section is None else {**self.original, section: variant})
                with self.subTest(section=section, variant=i):
                    self.invalid()

    def test_duplicate_keys_nonfinite_and_invalid_json_anywhere_fail_atomically(self):
        raw = json.dumps(self.original)
        variants = [b'\xff', b'{', b'{"schema_version":1,"schema_version":1}']
        # Replace occurrences individually: wrapper, each replay, changed explanations.
        for marker in ('"assumptions": {', '"summary": {', '"fingerprints": {',
                       '"comparison": {', '"policies": [{', '"candidate_policies": [{'):
            start = 0
            while (pos := raw.find(marker, start)) >= 0:
                end = pos + len(marker)
                for token in ('NaN', 'Infinity', '-Infinity', '1e999', '-1e999'):
                    variants.append((raw[:end] + '"bad":' + token + ',' + raw[end:]).encode())
                variants.append((raw[:end] + '"bad":1,"bad":2,' + raw[end:]).encode())
                start = end
        for i, value in enumerate(variants):
            self.saved.write_bytes(value)
            with self.subTest(variant=i):
                self.invalid()

    def test_invalid_corpus_either_config_files_and_arguments_fail_atomically(self):
        for path in (self.source, self.baseline, self.candidate):
            original = path.read_bytes()
            variants = [b'\xff', b'{']
            if path == self.source:
                variants += [b'{"resourceSpans":[],"resourceSpans":[]}', b'{"resourceSpans":[],"extra":1}',
                             b'{"resourceSpans":[{"scopeSpans":[{"spans":[{}]}]}]}']
            else:
                variants += [b'processors: {}', b'processors: &x {tail_sampling: *x}',
                             b'processors: {tail_sampling: {decision_wait: 1, policies: []}}',
                             b'processors: {tail_sampling: {policies: [{name: x, name: y, type: always_sample}]}}',
                             b'processors: {tail_sampling: {policies: [{name: x, type: latency, latency: {threshold_ms: -1}}]}}']
            for value in variants:
                path.write_bytes(value)
                with self.subTest(path=path.name, data=value):
                    self.invalid()
            path.write_bytes(original)
        for path in (self.source, self.baseline, self.candidate, self.saved):
            raw = path.read_bytes()
            path.unlink()
            self.invalid()
            path.write_bytes(raw)
        self.invalid('--config', str(self.baseline))
        self.invalid('--decision-wait-ms', '100')
        self.invalid('--report', str(self.root))
        args = [sys.executable, '-m', 'otelwrench', 'tailsim', 'verify-compare-evidence', str(self.source)]
        for missing in ('--report', '--baseline', '--candidate'):
            result = subprocess.run(args + [v for k, p in [('--report', self.saved), ('--baseline', self.baseline),
                ('--candidate', self.candidate)] if k != missing for v in (k, str(p))], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')

    def test_provenance_remains_opaque(self):
        changed = copy.deepcopy(self.original)
        changed.update(assumptions={}, byte_measure='untrusted', comparison={})
        for side in SIDES:
            changed[side].update(assumptions={}, byte_measure='untrusted', summary={}, traces=[None])
        self.save(changed)
        result = self.command('verify-compare')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['matches'])
        self.check(['assumptions', 'byte_measure', 'baseline', 'candidate', 'comparison'])
