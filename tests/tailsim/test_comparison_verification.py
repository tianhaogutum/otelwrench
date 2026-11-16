"""Saved static paired provenance through real CLI and independent preimages."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from .test_fingerprints import config, digest

ROOT = Path(__file__).resolve().parents[2]
FIELDS = ('corpus_source_sha256', 'effective_config_sha256')
WRAPPER = (FIELDS[0], 'baseline_effective_config_sha256', 'candidate_effective_config_sha256')
SIDES = ('baseline', 'candidate')
LOCATIONS = [(s, k) for s in ('fingerprints', *SIDES)
             for k in (WRAPPER if s == 'fingerprints' else FIELDS)]


class ComparisonVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.baseline, self.candidate, self.saved = [
            self.root / name for name in ('traces.json', 'base.yaml', 'cand.yaml', 'report.json')]
        # CRLF exercises exact-byte corpus identity independently.
        raw = (ROOT / 'examples/tailsim/traces.json').read_bytes().replace(b'\n', b'\r\n')
        self.source.write_bytes(raw)
        self.baseline.write_text('''processors: {tail_sampling: {policies: [
          {name: errors, type: status_code, status_code: {status_codes: [ERROR, ERROR]}}
        ]}}''')
        self.candidate.write_text('''processors: {tail_sampling: {policies: [
          {name: all-λ, type: always_sample},
          {name: slow, type: latency, latency: {threshold_ms: 500}},
          {name: service, type: string_attribute, string_attribute: {key: service, values: [λ, api, api]}}
        ]}}''', encoding='utf-8')
        self.expected = {
            'baseline': config([{'name': 'errors', 'type': 'status_code',
                                 'status_code': {'status_codes': ['ERROR']}}]),
            'candidate': config([
                {'name': 'all-λ', 'type': 'always_sample', 'always_sample': {}},
                {'name': 'slow', 'type': 'latency', 'latency': {'threshold_ms': 500, 'upper_threshold_ms': 0}},
                {'name': 'service', 'type': 'string_attribute', 'string_attribute': {'key': 'service', 'values': ['api', 'λ']}}])}
        self.save_comparison()

    def command(self, mode='verify-compare', *extra):
        args = [sys.executable, '-m', 'otelwrench', 'tailsim', mode, str(self.source),
                '--baseline', str(self.baseline), '--candidate', str(self.candidate)]
        if mode == 'verify-compare':
            args += ['--report', str(self.saved)]
        return subprocess.run([*args, *extra], capture_output=True, text=True)

    def save_comparison(self):
        result = self.command('compare')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.original = json.loads(result.stdout)
        self.saved.write_text(result.stdout, encoding='utf-8')

    def report(self, mismatches=()):
        result = self.command()
        self.assertEqual(result.returncode, int(bool(mismatches)), result.stderr)
        self.assertEqual(result.stderr, '')
        corpus_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        supplied = {s: dict(zip(FIELDS, (corpus_hash, digest(self.expected[s])))) for s in SIDES}
        supplied['fingerprints'] = dict(zip(WRAPPER, (corpus_hash, digest(self.expected['baseline']),
                                                    digest(self.expected['candidate']))))
        expected_checks = {}
        for section in ('fingerprints', *SIDES):
            block = self.original[section] if section == 'fingerprints' else self.original[section]['fingerprints']
            expected_checks[section] = {}
            for field, actual in supplied[section].items():
                match = (section, field) not in mismatches
                self.assertEqual(block[field] == actual, match)
                expected_checks[section][field] = {'reported_sha256': block[field],
                                                   'supplied_sha256': actual, 'matches': match}
        self.assertEqual(json.loads(result.stdout), {
            'schema_version': 1, 'mode': 'comparison_provenance_verification',
            'fingerprint_version': 1, 'algorithm': 'sha256',
            'matches': not bool(mismatches), 'checks': expected_checks})
        self.assertNotIn(str(self.root), result.stdout)

    def assert_invalid(self, *extra):
        result = self.command('verify-compare', *extra)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertTrue(result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_saved_comparison_checks_seven_independent_identities(self):
        self.report()
        self.assertEqual(json.loads(self.saved.read_text()), self.original)

    def test_changed_supplied_corpus_and_either_config(self):
        for section in ('source', *SIDES):
            path = getattr(self, section)
            raw = path.read_bytes()
            expected = copy.deepcopy(self.expected)
            if section == 'source':
                path.write_bytes(raw + b'\n')
                mismatches = [(s, FIELDS[0]) for s in ('fingerprints', *SIDES)]
            else:
                path.write_bytes(raw.replace(b'name: ', b'name: changed-', 1))
                self.expected[section]['processors']['tail_sampling']['policies'][0]['name'] = 'changed-' + (
                    self.expected[section]['processors']['tail_sampling']['policies'][0]['name'])
                mismatches = [('fingerprints', section + '_effective_config_sha256'), (section, FIELDS[1])]
            with self.subTest(section=section):
                self.report(mismatches)
            path.write_bytes(raw)
            self.expected = expected
        # Combined changes must enumerate every mismatch, without short-circuiting.
        self.source.write_bytes(self.source.read_bytes() + b'\n')
        for side in SIDES:
            getattr(self, side).write_bytes(getattr(self, side).read_bytes().replace(b'name: ', b'name: x-', 1))
            self.expected[side]['processors']['tail_sampling']['policies'][0]['name'] = 'x-' + (
                self.expected[side]['processors']['tail_sampling']['policies'][0]['name'])
        self.report(LOCATIONS)

    def test_every_wrapper_and_embedded_digest_is_checked_independently(self):
        original = copy.deepcopy(self.original)
        for changed in [[location] for location in LOCATIONS] + [LOCATIONS]:
            self.original = copy.deepcopy(original)
            for section, field in changed:
                block = self.original[section] if section == 'fingerprints' else self.original[section]['fingerprints']
                block[field] = '0' * 64
            self.saved.write_text(json.dumps(self.original))
            with self.subTest(changed=changed):
                self.report(changed)

    def test_equivalent_encodings_moved_inputs_equal_and_reversed_configs(self):
        for side in SIDES:
            path = getattr(self, side)
            path.write_text(json.dumps(self.expected[side], indent=4, sort_keys=True))
            moved = path.with_name('moved-' + path.name)
            path.rename(moved)
            setattr(self, side, moved)
        self.report()
        self.baseline, self.candidate = self.candidate, self.baseline
        self.expected['baseline'], self.expected['candidate'] = self.expected['candidate'], self.expected['baseline']
        mismatches = [('fingerprints', s + '_effective_config_sha256') for s in SIDES]
        mismatches += [(s, FIELDS[1]) for s in SIDES]
        self.report(mismatches)
        self.save_comparison()
        self.report()
        self.candidate = self.baseline
        self.expected['candidate'] = self.expected['baseline']
        self.save_comparison()
        self.report()
        self.assertEqual(self.original['baseline'], self.original['candidate'])
        self.assertEqual(self.original['comparison']['changed_trace_count'], 0)

    def test_opaque_evidence_and_empty_corpus(self):
        self.original.update(assumptions={}, byte_measure='untrusted', comparison={'invented': True})
        for side in SIDES:
            self.original[side].update(assumptions={}, byte_measure='untrusted', summary={}, traces=[None])
        self.saved.write_text(json.dumps(self.original))
        self.report()
        self.source.write_bytes(b'{"resourceSpans":[]}')
        self.save_comparison()
        self.report()

    def test_malformed_wrapper_and_both_embedded_envelopes_and_blocks(self):
        variants = [None, [], {}, self.original['baseline']]
        for section in (None, *SIDES):
            block = self.original if section is None else self.original[section]
            mutations = [None, [], {}]
            for key in block:
                changed = copy.deepcopy(block)
                del changed[key]
                mutations.append(changed)
            fields = WRAPPER if section is None else FIELDS
            wrong = [('schema_version', v) for v in (True, '1', 0, 2, 1.0)]
            wrong += [('mode', v) for v in ('unsupported', 'arrival_order_offline', None)]
            wrong += [('extra', 1), ('assumptions', []), ('byte_measure', {}), ('fingerprints', [])]
            wrong += [('comparison', [])] if section is None else [('summary', []), ('traces', {})]
            for key, value in wrong:
                mutations.append({**block, key: value})
            for key in block['fingerprints']:
                changed = copy.deepcopy(block)
                del changed['fingerprints'][key]
                mutations.append(changed)
            for key, value in [('version', True), ('version', '1'), ('version', 2), ('version', 1.0),
                               ('algorithm', 'SHA256'), ('algorithm', 'sha512'), ('algorithm', None), ('extra', 1)]:
                mutations.append({**block, 'fingerprints': {**block['fingerprints'], key: value}})
            for field in fields:
                for value in ['A' * 64, 'g' * 64, '0' * 63, '0' * 65, None, 0, True, [], {}]:
                    mutations.append({**block, 'fingerprints': {**block['fingerprints'], field: value}})
            variants += [mutation if section is None else {**self.original, section: mutation}
                         for mutation in mutations]
        for i, variant in enumerate(variants):
            self.saved.write_text(json.dumps(variant))
            with self.subTest(variant=i):
                self.assert_invalid()
        raw = json.dumps(self.original)
        invalid = [b'\xff', b'{', b'{"schema_version":1,"schema_version":1}']
        # Duplicate keys at wrapper, both embedded blocks, and opaque evidence depth.
        for section in ('fingerprints', *SIDES):
            duplicate = raw.replace('"' + section + '": {', '"' + section + '": {"x":1,"x":2,', 1)
            invalid.append(duplicate.encode())
        for token in ('NaN', 'Infinity', '-Infinity'):
            invalid.append(raw.replace('"comparison": {', '"comparison": {"x":' + token + ',').encode())
        for data in invalid:
            self.saved.write_bytes(data)
            self.assert_invalid()

    def test_invalid_source_either_config_files_and_arguments_fail_atomically(self):
        for path in (self.source, self.baseline, self.candidate):
            raw = path.read_bytes()
            variants = [b'\xff', b'{']
            if path == self.source:
                variants += [b'{"resourceSpans":[],"resourceSpans":[]}',
                             b'{"resourceSpans":[],"unsupported":false}',
                             b'{"resourceSpans":[{"scopeSpans":[{"spans":[{}]}]}]}']
            else:
                variants += [b'processors: {}', b'processors: &x {tail_sampling: *x}',
                             raw.replace(b'tail_sampling: {', b'tail_sampling: {decision_wait: 1,'),
                             raw.replace(b'name: ', b'name: duplicate, name: ', 1),
                             b'processors: {tail_sampling: {policies: [{name: bad, type: latency, latency: {threshold_ms: -1}}]}}']
            for data in variants:
                path.write_bytes(data)
                with self.subTest(path=path.name, data=data):
                    self.assert_invalid()
            path.write_bytes(raw)
        for path in (self.source, self.baseline, self.candidate, self.saved):
            raw = path.read_bytes()
            path.unlink()
            self.assert_invalid()
            path.write_bytes(raw)
        self.assert_invalid('--config', str(self.baseline))
        self.assert_invalid('--decision-wait-ms', '100')
        args = [sys.executable, '-m', 'otelwrench', 'tailsim', 'verify-compare', str(self.source),
                '--report', str(self.saved), '--baseline', str(self.baseline), '--candidate', str(self.candidate)]
        for option in ('--report', '--baseline', '--candidate'):
            i = args.index(option)
            result = subprocess.run(args[:i] + args[i+2:], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertTrue(result.stderr)
