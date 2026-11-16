"""Version-1 saved-report identity contract, with independent hash preimages."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from otelwrench.tailsim.corpus import load_corpus, parse_corpus
from otelwrench.tailsim.policy import parse_config
from otelwrench.tailsim.replay import compare, replay
from .helpers import attr, document, policies, policy, record

ROOT = Path(__file__).resolve().parents[2]


def digest(value):
    # Independent version-1 encoding; do not use tailsim's normalizer/serializer.
    data = json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def config(entries):
    return {'processors': {'tail_sampling': {'policies': entries}}}


class Fingerprints(unittest.TestCase):
    def setUp(self):
        self.text = json.dumps(document([record(attrs=[attr('service', 'λ')])]),
                               indent=2, ensure_ascii=False).replace('\n', '\r\n') + '\r\n'
        self.corpus = parse_corpus(self.text)

    def test_replay_exact_source_hash_and_normalized_preimage(self):
        # All seven supported policy types, including defaults nested under NOT.
        entries = [
            policy('always_sample', name='all-λ'),
            policy('status_code', {'status_codes': ['UNSET', 'ERROR', 'ERROR']}),
            policy('latency', {'threshold_ms': 500}),
            policy('string_attribute', {'key': 'service', 'values': ['λ', 'api', 'λ']}),
            policy('and', {'and_sub_policy': [
                policy('not', {'not_sub_policy': policy('always_sample')})]}),
            policy('not', {'not_sub_policy': policy('latency', {'threshold_ms': 10})}),
            policy('drop', {'drop_sub_policy': [
                policy('status_code', {'status_codes': ['OK', 'ERROR']})]})]
        expected = config([
            {'name': 'all-λ', 'type': 'always_sample', 'always_sample': {}},
            {'name': 'status_code', 'type': 'status_code', 'status_code': {'status_codes': ['ERROR', 'UNSET']}},
            {'name': 'latency', 'type': 'latency', 'latency': {'threshold_ms': 500, 'upper_threshold_ms': 0}},
            {'name': 'string_attribute', 'type': 'string_attribute', 'string_attribute': {'key': 'service', 'values': ['api', 'λ']}},
            {'name': 'and', 'type': 'and', 'and': {'and_sub_policy': [
                {'name': 'not', 'type': 'not', 'not': {'not_sub_policy':
                    {'name': 'always_sample', 'type': 'always_sample', 'always_sample': {}}}}]}},
            {'name': 'not', 'type': 'not', 'not': {'not_sub_policy':
                {'name': 'latency', 'type': 'latency', 'latency': {'threshold_ms': 10, 'upper_threshold_ms': 0}}}},
            {'name': 'drop', 'type': 'drop', 'drop': {'drop_sub_policy': [
                {'name': 'status_code', 'type': 'status_code', 'status_code': {'status_codes': ['ERROR', 'OK']}}]}}])
        parsed = policies(*entries)
        original = copy.deepcopy(parsed)
        result = replay(self.corpus, parsed)
        self.assertEqual(result['fingerprints'], {
            'version': 1, 'algorithm': 'sha256',
            'corpus_source_sha256': hashlib.sha256(self.text.encode('utf-8')).hexdigest(),
            'effective_config_sha256': digest(expected)})
        # Hashing does not insert defaults into or reorder the evaluated policies.
        self.assertEqual(parsed, original)
        self.assertEqual([p['name'] for p in result['traces'][0]['policies']],
                         [p['name'] for p in entries])

    def test_equivalent_yaml_defaults_and_sets_have_same_hash(self):
        compact = '''processors: {tail_sampling: {policies: [
          {name: all, type: always_sample},
          {name: slow, type: latency, latency: {threshold_ms: 500}},
          {name: errors, type: status_code, status_code: {status_codes: [OK, ERROR, ERROR]}},
          {name: service, type: string_attribute, string_attribute: {key: service, values: [λ, api, api]}}
        ]}}'''
        normalized = config([
            policy('always_sample', {}, 'all'),
            policy('latency', {'upper_threshold_ms': 0, 'threshold_ms': 500}, 'slow'),
            policy('status_code', {'status_codes': ['ERROR', 'OK']}, 'errors'),
            policy('string_attribute', {'values': ['api', 'λ'], 'key': 'service'}, 'service')])
        formatted = '# different formatting and mapping order\n' + yaml.safe_dump(normalized, sort_keys=True)
        a = replay(self.corpus, parse_config(compact))
        b = replay(self.corpus, parse_config(formatted))
        self.assertEqual(a['fingerprints'], b['fingerprints'])
        self.assertEqual(a['fingerprints']['effective_config_sha256'], digest(normalized))

    def test_source_identity_includes_whitespace_metadata_and_empty_groups(self):
        variants = [self.text, json.dumps(json.loads(self.text), sort_keys=True)]
        changed = json.loads(self.text)
        changed['resourceSpans'][0]['scopeSpans'][0]['spans'][0]['name'] = 'metadata-only'
        variants.append(json.dumps(changed))
        changed['resourceSpans'].append({'scopeSpans': []})
        variants.append(json.dumps(changed))
        hashes = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.json'
            for text in variants:
                with self.subTest(text=text):
                    path.write_bytes(text.encode('utf-8'))
                    result = replay(load_corpus(path), policies(policy('always_sample')))
                    self.assertEqual(result['traces'][0]['decision'], 'sample')
                    actual = result['fingerprints']['corpus_source_sha256']
                    self.assertEqual(actual, hashlib.sha256(path.read_bytes()).hexdigest())
                    hashes.append(actual)
        self.assertEqual(len(set(hashes)), len(variants))

    def test_config_identity_preserves_names_order_and_parameters(self):
        # Identical decisions on an empty corpus must not hide config changes.
        source = parse_corpus('{"resourceSpans":[]}')
        entries = [policy('latency', {'threshold_ms': 500}, 'slow'),
                   policy('always_sample', name='all')]
        original = replay(source, policies(*entries))['fingerprints']['effective_config_sha256']
        variants = [list(reversed(entries))]
        for field, value in [('name', 'renamed'), ('latency', {'threshold_ms': 501}),
                             ('latency', {'threshold_ms': 500, 'upper_threshold_ms': 700})]:
            changed = copy.deepcopy(entries)
            changed[0][field] = value
            variants.append(changed)
        for variant in variants:
            with self.subTest(variant=variant):
                report = replay(source, policies(*variant))
                self.assertEqual(report['traces'], [])
                self.assertNotEqual(report['fingerprints']['effective_config_sha256'], original)
        children = [policy('always_sample', name='a'), policy('always_sample', name='b')]
        a = policies(policy('and', {'and_sub_policy': children}))
        b = policies(policy('and', {'and_sub_policy': list(reversed(children))}))
        self.assertNotEqual(replay(source, a)['fingerprints']['effective_config_sha256'],
                            replay(source, b)['fingerprints']['effective_config_sha256'])

    def test_comparison_labels_pair_and_embedded_reports(self):
        baseline = policies(policy('always_sample'))
        candidate = policies(policy('latency', {'threshold_ms': 500}))
        forward = compare(self.corpus, baseline, candidate)
        reverse = compare(self.corpus, candidate, baseline)
        same = compare(self.corpus, candidate, candidate)
        for report in [forward, reverse, same]:
            before, after = report['baseline']['fingerprints'], report['candidate']['fingerprints']
            self.assertEqual(report['fingerprints'], {
                'version': 1, 'algorithm': 'sha256',
                'corpus_source_sha256': before['corpus_source_sha256'],
                'baseline_effective_config_sha256': before['effective_config_sha256'],
                'candidate_effective_config_sha256': after['effective_config_sha256']})
            self.assertEqual(before['corpus_source_sha256'], after['corpus_source_sha256'])
        self.assertEqual(forward['fingerprints']['baseline_effective_config_sha256'],
                         reverse['fingerprints']['candidate_effective_config_sha256'])
        self.assertEqual(forward['fingerprints']['candidate_effective_config_sha256'],
                         reverse['fingerprints']['baseline_effective_config_sha256'])
        self.assertEqual(same['comparison']['changed_trace_count'], 0)
        self.assertEqual(same['fingerprints']['baseline_effective_config_sha256'],
                         same['fingerprints']['candidate_effective_config_sha256'])

    def command(self, *args):
        return subprocess.run([sys.executable, '-m', 'otelwrench', 'tailsim', *map(str, args)],
                              capture_output=True, text=True)

    def test_saved_cli_report_is_path_independent_and_matches_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, base, cand = root / 'λ.json', root / 'base.yaml', root / 'cand.yaml'
            source.write_bytes(self.text.encode('utf-8'))
            base.write_text('processors: {tail_sampling: {policies: [{name: all, type: always_sample}]}}')
            cand.write_text('processors: {tail_sampling: {policies: [{name: slow, type: latency, latency: {threshold_ms: 500}}]}}')
            result = self.command('compare', source, '--baseline', base, '--candidate', cand)
            self.assertEqual(result.returncode, 0, result.stderr)
            saved = root / 'comparison.json'
            saved.write_text(result.stdout, encoding='utf-8')
            report = json.loads(saved.read_text(encoding='utf-8'))
            self.assertEqual(report['fingerprints'], {
                'version': 1, 'algorithm': 'sha256',
                'corpus_source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'baseline_effective_config_sha256': digest(config([policy('always_sample', {}, 'all')])),
                'candidate_effective_config_sha256': digest(config([policy('latency', {'threshold_ms': 500, 'upper_threshold_ms': 0}, 'slow')]))})
            moved = root / 'moved.json'
            source.rename(moved)
            replayed = self.command('replay', moved, '--config', cand)
            self.assertEqual(replayed.returncode, 0, replayed.stderr)
            self.assertEqual(json.loads(replayed.stdout)['fingerprints'], report['candidate']['fingerprints'])
            self.assertNotIn(str(root), saved.read_text())

    def test_normalization_does_not_mask_invalid_inputs_or_emit_partial_hashes(self):
        valid = 'processors: {tail_sampling: {policies: [{name: all, type: always_sample}]}}'
        bad_configs = [
            valid.replace('type: always_sample', 'type: always_sample, always_sample: {ignored: false}'),
            valid.replace('type: always_sample', 'type: latency, latency: {threshold_ms: -1}'),
            valid.replace('type: always_sample', 'type: string_attribute, string_attribute: {key: x, values: [a], enabled_regex_matching: false}'),
            valid.replace('name: all', 'name: all, name: duplicate'),
            valid + '\nprocessors: {}',
            'processors: &x {tail_sampling: *x}',
            valid.replace('name: all', 'name: "\\uD800"')]
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / 'bad.yaml'
            for text in bad_configs:
                bad.write_text(text, encoding='utf-8')
                for command, options in [('replay', ['--config', bad]),
                                         ('compare', ['--baseline', ROOT / 'examples/tailsim/baseline.yaml', '--candidate', bad])]:
                    with self.subTest(command=command, config=text):
                        result = self.command(command, ROOT / 'examples/tailsim/traces.json', *options)
                        self.assertEqual(result.returncode, 2)
                        self.assertEqual(result.stdout, '')
                        self.assertIn('tailsim:', result.stderr)
                        self.assertNotIn('Traceback', result.stderr)
            source = Path(tmp) / 'bad.json'
            for contents in [b'\xff', b'{"resourceSpans":[],"extra":false}',
                             b'{"resourceSpans":[],"resourceSpans":[]}']:
                source.write_bytes(contents)
                result = self.command('compare', source, '--baseline', ROOT / 'examples/tailsim/baseline.yaml',
                                      '--candidate', ROOT / 'examples/tailsim/policy.yaml')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('tailsim:', result.stderr)
