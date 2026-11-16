"""Selected comparison traces preserve whole-capture validation and count semantics."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest

from .helpers import ROOT, attr, logs, profile
from otelwrench.loki.config import load_profile
from otelwrench.loki.mapping import compare, explain
from otelwrench.loki.strict import PreviewError, read_json

FIXTURE = ROOT / 'examples/loki/explain'
RECORD = 'resourceLogs[0].scopeLogs[0].logRecords[0]'
RESOURCE_RULES = '/limits_config/otlp_config/resource_attributes/attributes_config/'


class CompareTraceTests(unittest.TestCase):
    def setUp(self):
        self.data = read_json(FIXTURE / 'logs.json')
        self.before = load_profile(FIXTURE / 'profile.yaml')
        self.after = load_profile(FIXTURE / 'after.yaml')

    def check_trace(self, data, before, after, record_id=RECORD, key=None, include_records=False):
        report = compare(data, before, after, record_id=record_id, key=key, include_records=include_records)
        trace = report['selectedRecordTrace']
        self.assertEqual(trace['recordId'], record_id)
        for side, policy in [('before', before), ('after', after)]:
            self.assertEqual(trace[side], explain(data, policy, record_id=record_id, key=key))
            group = report['streamTopology'][side][trace[side + 'StreamIndex']]
            self.assertIn(record_id, group['recordIds'])
            self.assertEqual(group['stream'], trace[side]['record']['stream'])
        compact = dict(report)
        compact.pop('selectedRecordTrace')
        self.assertEqual(compact, compare(data, before, after, include_records=include_records))
        return report, trace

    def test_reason_only_literal_to_regex_winner_changes_without_placement_changes(self):
        report, trace = self.check_trace(self.data, self.before, self.after, key='service_name')
        self.assertEqual(report['changedPlacementCount'], 0)
        self.assertEqual(report['changedPlacements'], [])
        self.assertEqual(report['changedStreams'], [])
        self.assertFalse(report['streamTopology']['membershipChanged'])
        self.assertEqual((trace['beforeStreamIndex'], trace['afterStreamIndex']), (0, 0))
        for side, rule, statuses in [('before', 0, ['selected', 'shadowed', 'not_matched']),
                                     ('after', 1, ['not_matched', 'selected', 'shadowed', 'not_matched'])]:
            explanation = trace[side]['explanations'][0]
            self.assertEqual(explanation['selection']['ruleRef'], RESOURCE_RULES + str(rule))
            self.assertEqual([r['status'] for r in explanation['selection']['rules']], statuses)
            self.assertEqual(explanation['final']['placement'], 'structured_metadata')
            self.assertEqual(explanation['final']['value'], 'checkout')
        self.assertEqual(trace['before']['explanations'][0]['selection']['rules'][0]['selector'],
                         {'attributes': ['service.name']})
        self.assertEqual(trace['after']['explanations'][0]['selection']['rules'][1]['selector'],
                         {'regex': '^service[.]name$'})

    def test_same_rule_pointer_and_reason_still_expose_changed_selector(self):
        before = profile(resource_attributes={'attributes_config': [
            {'action': 'index_label', 'attributes': ['service.name']}]})
        after = profile(resource_attributes={'attributes_config': [
            {'action': 'index_label', 'regex': '^service[.]name$'}]})
        report, trace = self.check_trace(self.data, before, after, key='service_name')
        a, b = [trace[side]['explanations'][0] for side in ('before', 'after')]
        self.assertEqual(a['final'], b['final'])
        self.assertEqual(a['selection']['ruleRef'], b['selection']['ruleRef'])
        self.assertNotEqual(a['selection']['rules'], b['selection']['rules'])
        self.assertEqual(report['changedPlacementCount'], 0)

    def test_changed_placement_nonfirst_record_resolves_actual_sorted_stream_indexes(self):
        fixture = ROOT / 'examples/loki/stream-topology'
        data = read_json(fixture / 'logs.json')
        before, after = [load_profile(fixture / name) for name in ('before.yaml', 'after.yaml')]
        record_id = 'resourceLogs[2].scopeLogs[0].logRecords[0]'
        report, trace = self.check_trace(data, before, after, record_id, 'route_old', include_records=True)
        self.assertEqual((trace['beforeStreamIndex'], trace['afterStreamIndex']), (1, 0))
        self.assertEqual(trace['before']['explanations'][0]['final']['placement'], 'index_label')
        self.assertEqual(trace['after']['explanations'][0]['selection']['ruleRef'], 'builtin:structured_metadata')
        self.assertEqual(report['changedPlacementCount'], 8)
        self.assertEqual(len(report['streamTopology']['merges']), 2)
        self.assertEqual(len(report['streamTopology']['splits']), 2)
        for side in ('before', 'after'):
            self.assertEqual(trace[side]['record'], report['records'][2][side])

    def test_all_keys_identity_default_fallback_fixed_nested_and_dropped_parity(self):
        report, trace = self.check_trace(self.data, self.before, self.before)
        self.assertEqual(trace['before'], trace['after'])
        self.assertEqual(len(trace['before']['explanations']), 8)
        self.assertNotIn('records', report)
        for key in ('net_peer_port', 'debug_private_value', 'internal_component', 'k8s_pod_name',
                    'empty', 'scope_name'):
            with self.subTest(key=key):
                _, selected = self.check_trace(self.data, self.before, self.after, key=key)
                self.assertEqual(len(selected['before']['explanations']), 1)
                self.assertEqual(len(selected['before']['record']['placements']), 8)

    def test_no_labels_and_record_without_placements(self):
        policy = profile(resource_attributes={'ignore_defaults': True})
        for data in (self.data, logs(resource=[], scope=[], log=[])):
            _, trace = self.check_trace(data, policy, policy)
            self.assertEqual(trace['before']['record']['stream'], {})
            self.assertTrue(any('no nonempty index label' in w for w in trace['before']['warnings']))
        self.assertEqual(trace['before']['explanations'], [])

    def test_invalid_selection_never_silently_returns_compact_report(self):
        for selection, message in [({'key': 'service_name'}, '--key requires --record-id'),
                                   ({'record_id': 'missing'}, 'record ID not found'),
                                   ({'record_id': ''}, 'record ID not found'),
                                   ({'record_id': RECORD, 'key': ''}, 'normalized output key not found'),
                                   ({'record_id': RECORD, 'key': 'service.name'}, 'normalized output key not found')]:
            with self.subTest(selection=selection), self.assertRaisesRegex(PreviewError, message):
                compare(self.data, self.before, self.after, **selection)
        with self.assertRaisesRegex(PreviewError, 'record ID not found'):
            compare({'resourceLogs': []}, self.before, self.after, record_id=RECORD)

    def test_invalid_capture_retains_paired_validation_before_selection(self):
        invalid_later = copy.deepcopy(self.data)
        invalid_later['resourceLogs'][0]['scopeLogs'][0]['logRecords'].append({'timeUnixNano': '0'})
        collision = logs(log=[attr('debug.private.value'), attr('debug_private_value')])
        empty = logs(resource=[attr('k8s.pod.name', {})])
        no_labels = profile(resource_attributes={'ignore_defaults': True})
        for data, before, after, message in [(invalid_later, self.before, self.after, 'missing timestamp'),
                                             (collision, self.before, self.after, 'normalized key collision'),
                                             (empty, self.before, no_labels, 'empty index-label value'),
                                             (empty, no_labels, self.after, 'empty index-label value')]:
            with self.subTest(message=message):
                with self.assertRaises(PreviewError) as compact:
                    compare(data, before, after)
                with self.assertRaises(PreviewError) as selected:
                    compare(data, before, after, record_id='missing', key='missing')
                self.assertEqual(str(compact.exception), str(selected.exception))
                self.assertIn(message, str(selected.exception))
                self.assertIn('before:', str(selected.exception))
                self.assertIn('after:', str(selected.exception))

    def test_cli_selection_and_option_dependency(self):
        command = [sys.executable, '-m', 'otelwrench', 'loki', 'compare', str(FIXTURE / 'logs.json'),
                   '--before', str(FIXTURE / 'profile.yaml'), '--after', str(FIXTURE / 'after.yaml')]
        result = subprocess.run(command + ['--record-id', RECORD, '--key', 'service_name'],
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), compare(self.data, self.before, self.after,
                                                           record_id=RECORD, key='service_name'))
        with tempfile.TemporaryDirectory() as directory:
            for extra, message in [(['--key', 'service_name'], '--key requires --record-id'),
                                   (['--record-id', RECORD, '--key', 'missing'], 'normalized output key not found'),
                                   (['--record-id', 'missing'], 'record ID not found'),
                                   (['--push-json', directory + '/push.json'], 'unrecognized arguments'),
                                   (['--endpoint', 'https://example.invalid'], 'unrecognized arguments')]:
                with self.subTest(extra=extra):
                    result = subprocess.run(command + extra, text=True, capture_output=True, timeout=60)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, '')
                    self.assertIn(message, result.stderr)
                    self.assertNotIn('Traceback', result.stderr)
