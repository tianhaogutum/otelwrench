"""Complete per-record audits and symmetric failures on the same captured input."""

import copy
import json
import subprocess
import sys
import unittest

from .helpers import ROOT, attr, logs, profile
from otelwrench.loki.config import load_profile
from otelwrench.loki.mapping import compare, preview
from otelwrench.loki.strict import PreviewError, read_json

FIXTURE = ROOT / 'examples/loki/policy-audit'


class ComparisonAuditTests(unittest.TestCase):
    def setUp(self):
        self.data = read_json(FIXTURE / 'logs.json')
        self.before = load_profile(FIXTURE / 'before.yaml')
        self.after = load_profile(FIXTURE / 'after.yaml')

    def test_complete_audit_includes_unchanged_and_dropped_placements(self):
        report = compare(self.data, self.before, self.after, include_records=True)
        self.assertEqual(report['countScope'], 'observed_input_records_only')
        self.assertEqual(report['before'], {'recordCount': 3, 'streamCount': 2, 'droppedAttributeCount': 0})
        self.assertEqual(report['after'], {'recordCount': 3, 'streamCount': 1, 'droppedAttributeCount': 6})
        self.assertEqual(report['streamCountDelta'], -1)
        self.assertEqual(report['changedPlacementCount'], 9)
        for index, record in enumerate(report['records']):
            self.assertEqual(record['recordId'], f'resourceLogs[{index}].scopeLogs[0].logRecords[0]')
            pod = ['p1', 'p2', 'p1'][index]
            order = str(42 + index)
            self.assertEqual(record['before']['stream'], {'service_name': 'checkout', 'k8s_pod_name': pod})
            self.assertEqual(record['after']['stream'], {'service_name': 'checkout'})
            fixed = {'scope_name': 'example.checkout', 'scope_version': '1.0'}
            self.assertEqual(record['before']['structuredMetadata'], {
                **fixed, 'component': 'payment', 'order_id': order, 'debug_secret': 'SYNTHETIC-ONLY'})
            self.assertEqual(record['after']['structuredMetadata'], {**fixed, 'order_id': order, 'k8s_pod_name': pod})
            for side, actions in [('before', ['index_label', 'index_label', 'structured_metadata',
                                             'structured_metadata', 'structured_metadata',
                                             'structured_metadata', 'structured_metadata']),
                                  ('after', ['index_label', 'structured_metadata', 'drop',
                                            'structured_metadata', 'drop', 'structured_metadata', 'structured_metadata'])]:
                placements = record[side]['placements']
                self.assertEqual([(p['source'], p['sourcePath'], p['key'], p['value']) for p in placements], [
                    ('resource', ['service.name'], 'service_name', 'checkout'),
                    ('resource', ['k8s.pod.name'], 'k8s_pod_name', pod),
                    ('scope', ['component'], 'component', 'payment'),
                    ('log', ['order.id'], 'order_id', order),
                    ('log', ['debug.secret'], 'debug_secret', 'SYNTHETIC-ONLY'),
                    ('field', ['scope_name'], 'scope_name', 'example.checkout'),
                    ('field', ['scope_version'], 'scope_version', '1.0')])
                self.assertEqual([p['placement'] for p in placements], actions)
                self.assertTrue(all(p['reason'] for p in placements))
                self.assertEqual(record[side]['dropped'], [p for p in placements if p['placement'] == 'drop'])
        self.assertEqual(len(report['records']), 3)

    def test_identity_comparison_still_explains_every_record(self):
        report = compare(self.data, self.before, self.before, include_records=True)
        self.assertEqual(report['changedPlacements'], [])
        self.assertEqual(report['changedStreams'], [])
        self.assertEqual(len(report['records']), 3)
        for record in report['records']:
            self.assertEqual(record['before'], record['after'])
            self.assertEqual(len(record['before']['placements']), 7)

    def test_compact_default_remains_available(self):
        report = compare(self.data, self.before, self.after)
        self.assertNotIn('records', report)

    def test_all_cross_level_collisions_fail_identically_even_when_dropped(self):
        data = read_json(FIXTURE / 'collisions.json')
        # Test the full resource/scope/log collision and each pair separately.
        for removed in (None, 'resource', 'scope', 'log'):
            candidate = copy.deepcopy(data)
            resource = candidate['resourceLogs'][0]
            scope = resource['scopeLogs'][0]
            containers = {'resource': resource['resource'], 'scope': scope['scope'],
                          'log': scope['logRecords'][0]}
            if removed:
                containers[removed]['attributes'] = [a for a in containers[removed]['attributes']
                                                    if a['key'] not in ('shared.key', 'shared_key', 'shared-key')]
            with self.subTest(removed=removed):
                errors = []
                for policy in (self.before, self.after):
                    with self.assertRaises(PreviewError) as raised:
                        preview(candidate, policy)
                    errors.append(str(raised.exception))
                self.assertEqual(errors[0], errors[1])
                self.assertIn("normalized key collision 'shared_key'", errors[0])
                with self.assertRaises(PreviewError) as raised:
                    compare(candidate, self.before, self.after, include_records=True)
                self.assertIn('before: invalid: ' + errors[0], str(raised.exception))
                self.assertIn('after: invalid: ' + errors[1], str(raised.exception))

    def test_policy_dependent_failure_identifies_valid_side_in_either_order(self):
        data = logs(resource=[attr('service.name', {'stringValue': ''})])
        demoted = profile(resource_attributes={'ignore_defaults': True})
        for before, after, invalid_side, valid_side in (
            (self.before, demoted, 'before', 'after'),
            (demoted, self.before, 'after', 'before')):
            with self.subTest(invalid_side=invalid_side), self.assertRaises(PreviewError) as raised:
                compare(data, before, after)
            self.assertIn(invalid_side + ': invalid:', str(raised.exception))
            self.assertIn('empty index-label value', str(raised.exception))
            self.assertIn(valid_side + ': valid', str(raised.exception))

    def test_cli_audit_and_collision_failure(self):
        command = [sys.executable, '-m', 'otelwrench', 'loki', 'compare',
                   str(FIXTURE / 'logs.json'), '--before', str(FIXTURE / 'before.yaml'),
                   '--after', str(FIXTURE / 'after.yaml'), '--include-records']
        result = subprocess.run(command, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['records']), 3)
        command[4] = str(FIXTURE / 'collisions.json')
        result = subprocess.run(command, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn("before: invalid:", result.stderr)
        self.assertIn("after: invalid:", result.stderr)
        self.assertIn("normalized key collision 'shared_key'", result.stderr)
        self.assertNotIn('Traceback', result.stderr)
