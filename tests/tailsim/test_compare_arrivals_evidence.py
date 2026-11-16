"""Real CLI paired regeneration with independent full-report arrival arithmetic."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from . import test_arrivals_evidence as arrival_oracle

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'examples/tailsim/arrival-order'
FIELDS = ('schema_version', 'mode', 'fingerprints', 'assumptions', 'baseline', 'candidate', 'comparison')
SIDES = ('baseline', 'candidate')


class CompareArrivalsEvidence(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source, self.config, self.arrivals, self.saved = [self.root / n for n in
            ('traces.json', 'policy.yaml', 'arrivals.json', 'report.json')]
        for path in (self.source, self.config, self.arrivals):
            path.write_bytes((FIXTURE / path.name).read_bytes())
        self.produce()

    def command(self, mode='verify-compare-arrivals-evidence', baseline='100', candidate='50', *extra):
        cli = [os.environ['TAILSIM_TEST_CLI']] if 'TAILSIM_TEST_CLI' in os.environ else [sys.executable, '-m', 'otelwrench', 'tailsim']
        args = [*cli, mode, str(self.source), '--config', str(self.config), '--arrivals', str(self.arrivals),
                '--baseline-wait-ms', str(baseline), '--candidate-wait-ms', str(candidate)]
        if mode != 'compare-arrivals':
            args += ['--report', str(self.saved)]
        result = subprocess.run([*args, *extra], capture_output=True, text=True)
        if directory := os.environ.get('TAILSIM_TEST_LOG_DIR'):
            log = Path(directory)
            log.mkdir(parents=True, exist_ok=True)
            name = f'{len(list(log.glob("*.json"))):04d}'
            (log / (name + '.stdout')).write_text(result.stdout)
            (log / (name + '.stderr')).write_text(result.stderr)
            (log / (name + '.json')).write_text(json.dumps({'command': [*args, *extra],
                'exitCode': result.returncode, 'stdout': str(log / (name + '.stdout')),
                'stderr': str(log / (name + '.stderr'))}, indent=2))
        return result

    def produce(self, baseline='100', candidate='50'):
        result = self.command('compare-arrivals', baseline, candidate)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.original = json.loads(result.stdout)
        self.saved.write_text(result.stdout)

    def save(self, value):
        self.saved.write_text(json.dumps(value, ensure_ascii=False))

    def check(self, mismatches=(), baseline='100', candidate='50'):
        result = self.command(baseline=baseline, candidate=candidate)
        self.assertEqual(result.returncode, int(bool(mismatches)), result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertEqual(json.loads(result.stdout), {'schema_version': 1,
            'mode': 'arrival_comparison_evidence_verification', 'matches': not bool(mismatches),
            'checks': {key: {'matches': key not in mismatches} for key in FIELDS}})
        self.assertNotIn(str(self.root), result.stdout)

    def invalid(self, baseline='100', candidate='50', *extra):
        result = self.command('verify-compare-arrivals-evidence', baseline, candidate, *extra)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def assert_oracle(self, baseline=100, candidate=50):
        # Each expected replay is built from raw OTLP/schedule arithmetic and
        # fixed policy semantics, with no production imports. Reuse that oracle,
        # then derive the pair from its expected rows, never from saved assertions.
        replays = {}
        for side, wait in zip(SIDES, (baseline, candidate)):
            probe = unittest.TestCase()
            probe.source, probe.arrivals, probe.original = self.source, self.arrivals, self.original[side]
            replays[side] = arrival_oracle.ArrivalsEvidence.assert_oracle(probe, wait)
        before, after = (replays[s] for s in SIDES)
        pairs = list(zip(before['traces'], after['traces']))
        decided = [(a, b) for a, b in pairs if a['state'] == b['state'] == 'decided']
        flips = [(a, b) for a, b in decided if a['decision_snapshot']['decision'] != b['decision_snapshot']['decision']]
        state = [(a, b) for a, b in pairs if a['state'] != b['state']]
        unresolved = [(a, b) for a, b in pairs if a['state'] != 'decided' or b['state'] != 'decided']
        def evidence(items):
            return [{'trace_id': a['trace_id'], 'baseline': a, 'candidate': b} for a, b in items]
        comparison = {'both_decided_trace_count': len(decided), 'changed_decision_count': len(flips),
            'changed_decisions': evidence(flips),
            'newly_sampled_traces': sum(b['decision_snapshot']['decision'] == 'sample' for a, b in flips),
            'newly_dropped_traces': sum(b['decision_snapshot']['decision'] == 'drop' for a, b in flips),
            'state_change_count': len(state), 'state_changes': evidence(state),
            'unresolved_trace_count': len(unresolved),
            'unresolved_traces': [{'trace_id': a['trace_id'], 'baseline_state': a['state'], 'candidate_state': b['state'],
                'baseline_decision': a['decision_snapshot']['decision'] if a['state'] == 'decided' else None,
                'candidate_decision': b['decision_snapshot']['decision'] if b['state'] == 'decided' else None} for a, b in unresolved],
            'kept_span_delta': after['summary']['kept_spans'] - before['summary']['kept_spans']}
        assumptions = {**before['assumptions'], 'comparison':
            'Decision flips require both waits to finalize; unresolved states are separate. Kept span delta includes state resolution and differing on-time subsets.'}
        expected = {'schema_version': 1, 'mode': 'arrival_order_offline_comparison',
            'fingerprints': {**{k: before['fingerprints'][k] for k in ('version', 'algorithm',
                'corpus_source_sha256', 'effective_config_sha256', 'arrival_schedule_sha256')},
                'baseline_effective_arrival_model_sha256': before['fingerprints']['effective_arrival_model_sha256'],
                'candidate_effective_arrival_model_sha256': after['fingerprints']['effective_arrival_model_sha256']},
            'assumptions': assumptions, **replays, 'comparison': comparison}
        self.assertEqual(set(self.original), set(FIELDS))
        for field in FIELDS:
            with self.subTest(oracle=field):
                self.assertEqual(self.original[field], expected[field])

    def test_saved_pair_agrees_with_independent_full_oracle(self):
        self.assert_oracle()
        self.check()

    def test_deadline_cutoff_ties_late_sampled_discard_equal_reverse_waits(self):
        # Default 100/50: ERROR at 50ms is excluded on candidate; trace 4's
        # candidate timer fires exactly at the cutoff. Other pairs exercise
        # state resolution, no finalized traces, equal and reversed labels.
        for a, b in [(100, 200), (50, 100), (100, 100), (100, 300)]:
            with self.subTest(waits=(a, b)):
                self.produce(a, b)
                self.assert_oracle(a, b)
                self.check(baseline=a, candidate=b)
        manifest = json.loads(self.arrivals.read_bytes())
        manifest['arrivals'][0]['at_unix_nano'] = '100000000'  # OK late after ERROR sampled
        manifest['arrivals'][1]['at_unix_nano'] = '0'
        manifest['arrivals'][3]['at_unix_nano'] = '100000000'  # ERROR deadline tie
        manifest['arrivals'][4]['at_unix_nano'] = '150000000'  # baseline timer at cutoff
        manifest['arrivals'][5]['at_unix_nano'] = '250000000'  # arrival at cutoff, pending
        self.arrivals.write_text(json.dumps(manifest))
        self.produce('100', '200')
        self.assert_oracle(100, 200)
        first = self.original['baseline']['traces'][0]
        self.assertEqual(first['decision_snapshot']['kept_span_count'], 1)
        self.assertEqual(first['late_span_ids'], ['0000000000000001'])
        self.check(baseline='100', candidate='200')

    def test_all_13_identities_models_and_nested_evidence_independently(self):
        changes = []
        for section in ('fingerprints', *SIDES):
            fp = self.original['fingerprints'] if section == 'fingerprints' else self.original[section]['fingerprints']
            for key in fp.keys() - {'version', 'algorithm'}:
                def mutate(r, s=section, k=key):
                    block = r['fingerprints'] if s == 'fingerprints' else r[s]['fingerprints']
                    block[k] = '0' * 64
                changes.append((section, mutate))
        self.assertEqual(len(changes), 13)
        changes += [('assumptions', lambda r: r.update(assumptions={})),
            ('comparison', lambda r: r['comparison'].update(kept_span_delta=999)),
            ('comparison', lambda r: r['comparison']['changed_decisions'][0]['baseline']['decision_snapshot']['policies'][0].update(reason='invented')),
            ('comparison', lambda r: r['comparison']['changed_decisions'][0]['candidate']['full_trace'].update(decision='invented')),
            ('comparison', lambda r: r['comparison']['state_changes'][0]['candidate'].update(on_time_span_ids=[])),
            ('comparison', lambda r: r['comparison']['unresolved_traces'][0].update(baseline_decision='drop'))]
        for side in SIDES:
            changes += [
                (side, lambda r, s=side: r[s]['model'].update(name='invented', decision_wait_ms=-1)),
                (side, lambda r, s=side: r[s]['model'].update(deadline_boundary='inclusive', late_span_handling='retain', end_of_input='flush')),
                (side, lambda r, s=side: r[s].update(assumptions={})),
                (side, lambda r, s=side: r[s]['summary'].update(kept_spans=999)),
                (side, lambda r, s=side: r[s]['traces'][0].update(deadline_unix_nano=0)),
                (side, lambda r, s=side: r[s]['traces'][1].update(on_time_span_ids=[])),
                (side, lambda r, s=side: r[s]['traces'][2].update(missing_span_ids=[], unseen_at_decision_span_ids=[])),
                (side, lambda r, s=side: r[s]['traces'][3].update(state='unobserved')),
                (side, lambda r, s=side: r[s]['traces'][4].update(decision_snapshot={})),
                (side, lambda r, s=side: r[s]['traces'][0]['decision_snapshot'].update(decision='invented')),
                (side, lambda r, s=side: r[s]['traces'][0]['full_trace']['policies'][0].update(children=[None]))]
        for section, mutate in changes:
            value = copy.deepcopy(self.original)
            mutate(value)
            self.assertNotEqual(value, self.original)
            self.save(value)
            with self.subTest(section=section):
                self.check([section])
        value = copy.deepcopy(self.original)
        for _, mutate in changes:
            mutate(value)
        self.save(value)
        self.check(['fingerprints', 'assumptions', 'baseline', 'candidate', 'comparison'])

    def test_recursive_types_array_order_and_arbitrary_finite_evidence(self):
        changes = [('comparison', lambda r: r['comparison']['unresolved_traces'].reverse()),
            ('comparison', lambda r: r['comparison'].pop('newly_dropped_traces')),
            ('comparison', lambda r: r['comparison'].update(extra={'arbitrary': [True, 1, None, 1.0]})),
            ('assumptions', lambda r: r['assumptions'].update(production_savings_estimated=0))]
        for value in (True, 1.0, '1', None, []):
            changes.append(('comparison', lambda r, v=value: r['comparison'].update(changed_decision_count=v)))
        for side in SIDES:
            changes += [(side, lambda r, s=side: r[s]['traces'].reverse()),
                (side, lambda r, s=side: r[s]['traces'][0]['full_trace']['statuses'].reverse()),
                (side, lambda r, s=side: r[s]['traces'][0]['full_trace'].update(error=1)),
                (side, lambda r, s=side: r[s]['traces'][0]['decision_snapshot']['policies'][0].update(matched=int(r[s]['traces'][0]['decision_snapshot']['policies'][0]['matched']))),
                (side, lambda r, s=side: r[s]['summary'].update(kept_spans=float(r[s]['summary']['kept_spans']))),
                (side, lambda r, s=side: r[s]['model'].update(decision_wait_ms=str(r[s]['model']['decision_wait_ms']))),
                (side, lambda r, s=side: r[s].update(traces=[None, True, 1, 'finite', []]))]
        for section, mutate in changes:
            value = copy.deepcopy(self.original)
            mutate(value)
            self.save(value)
            with self.subTest(section=section):
                self.check([section])

    def test_key_order_unicode_and_equivalent_config_schedule_encodings(self):
        def reverse_keys(value):
            if isinstance(value, dict):
                return {k: reverse_keys(value[k]) for k in reversed(value)}
            if isinstance(value, list):
                return [reverse_keys(v) for v in value]
            return value
        # Non-numeric IDs make upper/lower case normalization observable.
        for path in (self.source, self.arrivals):
            path.write_text(path.read_text().replace('00000000000000000000000000000001', '0000000000000000000000000000000a')
                            .replace('0000000000000001"', '000000000000000a"'))
        self.config.write_text(self.config.read_text().replace('name: errors', 'name: errors-λ'))
        self.produce()
        self.saved.write_text(json.dumps(reverse_keys(self.original), ensure_ascii=True))
        self.config.write_text('{"processors":{"tail_sampling":{"policies":[{"status_code":{"status_codes":["ERROR","ERROR"]},"type":"status_code","name":"errors-\\u03bb"}]}}}')
        manifest = json.loads(self.arrivals.read_text())
        manifest['arrivals'].reverse()
        manifest['observation_end_unix_nano'] = int(manifest['observation_end_unix_nano'])
        for row in manifest['arrivals']:
            row['at_unix_nano'] = '000' + str(row['at_unix_nano'])
            row['trace_id'], row['span_id'] = row['trace_id'].upper(), row['span_id'].upper()
        self.arrivals.write_text(json.dumps(reverse_keys(manifest), indent=4))
        self.check(baseline='000100', candidate='00050')

    def test_supplied_inputs_and_either_wait_drive_regeneration(self):
        self.check(['fingerprints', 'baseline', 'comparison'], baseline='200')
        self.check(['fingerprints', 'candidate', 'comparison'], candidate='100')
        self.source.write_bytes(self.source.read_bytes() + b'\n')
        self.check(['fingerprints', 'baseline', 'candidate'])
        self.source.write_bytes((FIXTURE / 'traces.json').read_bytes())
        self.source.write_bytes(self.source.read_bytes().replace(b'"code": 2', b'"code": 1'))
        self.check(['fingerprints', 'baseline', 'candidate', 'comparison'])
        self.source.write_bytes((FIXTURE / 'traces.json').read_bytes())
        self.config.write_text('processors: {tail_sampling: {policies: [{name: all, type: always_sample}]}}')
        self.check(['fingerprints', 'baseline', 'candidate', 'comparison'])
        self.config.write_bytes((FIXTURE / 'policy.yaml').read_bytes())
        manifest = json.loads(self.arrivals.read_bytes())
        manifest['arrivals'][3]['at_unix_nano'] = '50000000'
        self.arrivals.write_text(json.dumps(manifest))
        self.check(['fingerprints', 'baseline', 'candidate', 'comparison'])
        manifest['observation_end_unix_nano'] = '300000000'
        self.arrivals.write_text(json.dumps(manifest))
        self.check(['fingerprints', 'baseline', 'candidate', 'comparison'])
        self.source.write_bytes(b'{"resourceSpans":[]}')
        self.arrivals.write_text('{"schema_version":1,"observation_end_unix_nano":0,"arrivals":[]}')
        self.produce()
        self.assert_oracle()
        self.check()

    def test_wrapper_and_either_embedded_envelope_metadata_fail_atomically(self):
        for section in (None, *SIDES):
            original = self.original if section is None else self.original[section]
            variants = [None, [], {}]
            variants += [{k: v for k, v in original.items() if k != key} for key in original]
            for key, value in [('schema_version', True), ('schema_version', 1.0), ('schema_version', '1'),
                               ('schema_version', 2), ('mode', 'complete_trace_offline'), ('mode', None),
                               ('extra', 1), ('fingerprints', []), ('assumptions', [])]:
                variants.append({**original, key: value})
            for key, value in ([('comparison', []), ('baseline', None), ('candidate', {}), ('mode', 'arrival_order_offline')]
                               if section is None else [('model', []), ('summary', []), ('traces', {}), ('mode', 'arrival_order_offline_comparison')]):
                variants.append({**original, key: value})
            fp = original['fingerprints']
            variants += [{**original, 'fingerprints': {k: v for k, v in fp.items() if k != key}} for key in fp]
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

    def test_duplicate_keys_nonfinite_anywhere_invalid_json_fail_atomically(self):
        raw = json.dumps(self.original)
        variants = [b'\xff', b'{', b'{"schema_version":1,"schema_version":1}']
        # Every occurrence spans wrapper, both replays and duplicated comparison
        # snapshots. Check exponent overflow too, beyond parse_json constants.
        for marker in ('"model": {', '"assumptions": {', '"summary": {', '"fingerprints": {',
                       '"comparison": {', '"decision_snapshot": {', '"full_trace": {', '"policies": [{'):
            start = 0
            while (pos := raw.find(marker, start)) >= 0:
                end = pos + len(marker)
                for token in ('NaN', 'Infinity', '-Infinity', '1e999', '-1e999'):
                    variants.append((raw[:end] + '"bad":[{"nested":' + token + '}],' + raw[end:]).encode())
                variants.append((raw[:end] + '"bad":1,"bad":2,' + raw[end:]).encode())
                start = end
        for i, value in enumerate(variants):
            self.saved.write_bytes(value)
            with self.subTest(variant=i):
                self.invalid()

    def test_invalid_sources_schedule_either_wait_deadline_files_arguments(self):
        for path in (self.source, self.config, self.arrivals):
            original = path.read_bytes()
            variants = [b'\xff', b'{']
            if path == self.source:
                variants += [b'{"resourceSpans":[],"resourceSpans":[]}', b'{"resourceSpans":[],"extra":1}',
                             b'{"resourceSpans":[{"scopeSpans":[{"spans":[{}]}]}]}']
            elif path == self.config:
                variants += [b'processors: {}', b'processors: &x {tail_sampling: *x}',
                             b'processors: {tail_sampling: {decision_wait: 1, policies: []}}',
                             b'processors: {tail_sampling: {policies: [{name: x, name: y, type: always_sample}]}}',
                             b'processors: {tail_sampling: {policies: [{name: x, type: latency, latency: {threshold_ms: -1}}]}}']
            else:
                valid = json.loads(original)
                schedules = [None, [], {**valid, 'extra': 1}, {**valid, 'arrivals': {}},
                             {**valid, 'arrivals': valid['arrivals'] + [valid['arrivals'][0]]}]
                for key, value in [('schema_version', True), ('schema_version', '1'), ('schema_version', 2),
                                   ('observation_end_unix_nano', -1), ('observation_end_unix_nano', 2**64)]:
                    schedules.append({**valid, key: value})
                for key, value in [('at_unix_nano', True), ('at_unix_nano', 0.0), ('at_unix_nano', -1),
                                   ('at_unix_nano', '250000001'), ('at_unix_nano', '18446744073709551616'),
                                   ('trace_id', 'f' * 32), ('span_id', 'f' * 16)]:
                    schedules.append({**valid, 'arrivals': [{**valid['arrivals'][0], key: value}]})
                variants += [json.dumps(s).encode() for s in schedules]
                variants += [original.replace(b'"schema_version": 1', b'"schema_version": 1,"schema_version": 1'),
                             original.replace(b'"at_unix_nano": "0"', b'"at_unix_nano": 1e999')]
            for value in variants:
                path.write_bytes(value)
                with self.subTest(path=path.name, data=value[:100]):
                    self.invalid()
            path.write_bytes(original)
        for wait in (0, -1, 'true', '1.5', (2**64 - 1) // 1_000_000 + 1):
            self.invalid(baseline=wait)
            self.invalid(candidate=wait)
        manifest = json.loads(self.arrivals.read_bytes())
        manifest['arrivals'] = [{**manifest['arrivals'][0], 'at_unix_nano': str(2**64 - 1 - 1_000_000)}]
        manifest['observation_end_unix_nano'] = str(2**64 - 1)
        self.arrivals.write_text(json.dumps(manifest))
        # Maximum deadline is valid. Only the baseline, then only candidate overflows.
        self.produce('1', '1')
        self.assert_oracle(1, 1)
        self.check(baseline='1', candidate='1')
        self.invalid(baseline='2', candidate='1')
        self.invalid(baseline='1', candidate='2')
        manifest['arrivals'][0]['at_unix_nano'] = str(2**64 - 1 - 1_000_000 + 1)
        self.arrivals.write_text(json.dumps(manifest))
        self.invalid(baseline='1', candidate='1')
        self.arrivals.write_bytes((FIXTURE / 'arrivals.json').read_bytes())
        self.produce()
        for path in (self.source, self.config, self.arrivals, self.saved):
            raw = path.read_bytes()
            path.unlink()
            self.invalid()
            path.write_bytes(raw)
        self.invalid('100', '50', '--decision-wait-ms', '100')
        self.invalid('100', '50', '--report', str(self.root))
        # Missing required options (the normal helper always supplies them).
        cli = [os.environ['TAILSIM_TEST_CLI']] if 'TAILSIM_TEST_CLI' in os.environ else [sys.executable, '-m', 'otelwrench', 'tailsim']
        for missing in ('--report', '--config', '--arrivals', '--baseline-wait-ms', '--candidate-wait-ms'):
            args = [*cli, 'verify-compare-arrivals-evidence', str(self.source)]
            for key, value in [('--report', self.saved), ('--config', self.config), ('--arrivals', self.arrivals),
                               ('--baseline-wait-ms', '100'), ('--candidate-wait-ms', '50')]:
                if key != missing:
                    args += [key, str(value)]
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')

    def test_provenance_stays_opaque_and_nested_evidence_is_data(self):
        value = copy.deepcopy(self.original)
        value.update(assumptions={}, comparison={})
        for side in SIDES:
            value[side].update(model={'untrusted': True}, assumptions={}, summary={}, traces=[None])
        self.save(value)
        result = self.command('verify-compare-arrivals')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['matches'])
        self.check(['assumptions', 'baseline', 'candidate', 'comparison'])
