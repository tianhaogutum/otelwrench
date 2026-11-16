"""Selected totals cover the key selection before any summary filtering."""

import unittest

from .helpers import ROOT, logs, profile
from otelwrench.loki.config import load_profile
from otelwrench.loki.mapping import compare, explain
from otelwrench.loki.strict import read_json

FIXTURE = ROOT / 'examples/loki/explain'
RECORD = 'resourceLogs[0].scopeLogs[0].logRecords[0]'


class DecisionCountTests(unittest.TestCase):
    def setUp(self):
        self.data = read_json(FIXTURE / 'logs.json')
        self.before = load_profile(FIXTURE / 'profile.yaml')
        self.after = load_profile(FIXTURE / 'after.yaml')

    def check_counts(self, data, before, after, expected, record_id=RECORD, key=None):
        full = compare(data, before, after, record_id=record_id, key=key, include_records=True)
        filtered = compare(data, before, after, record_id=record_id, key=key,
                           include_records=True, changed_decisions_only=True)
        counts = dict(zip(('selectedLeafCount', 'winnerChangedCount', 'placementChangedCount', 'reasonChangedCount'), expected))
        for report in (full, filtered):
            trace = report['selectedRecordTrace']
            self.assertEqual(trace['decisionCounts'], counts)
            self.assertTrue(all(type(value) is int and value >= 0 for value in trace['decisionCounts'].values()))
            for side, policy in [('before', before), ('after', after)]:
                self.assertEqual(trace[side], explain(data, policy, record_id=record_id, key=key))
        # Only decisions and their view label change, including when no winner changed.
        filtered['selectedRecordTrace']['decisions'] = full['selectedRecordTrace']['decisions']
        filtered['selectedRecordTrace']['decisionFilter'] = full['selectedRecordTrace']['decisionFilter']
        self.assertEqual(filtered, full)
        trace = full.pop('selectedRecordTrace')
        self.assertEqual(full, compare(data, before, after, include_records=True))
        return full, trace

    def test_reason_only_totals_and_key_selection_scope(self):
        for after in (self.after, load_profile(FIXTURE / 'selector-only.yaml')):
            with self.subTest(profile=after.describe()):
                report, trace = self.check_counts(self.data, self.before, after, (8, 1, 0, 1))
                self.assertEqual(report['changedPlacementCount'], 0)
                self.assertEqual(len(trace['before']['record']['placements']), 8)
                self.check_counts(self.data, self.before, after, (1, 1, 0, 1), key='service_name')
                self.check_counts(self.data, self.before, after, (1, 0, 0, 0), key='debug_private_value')

    def test_identity_shadowed_only_and_empty_placement_counts(self):
        rules = self.before.describe()
        rules['resource'][1] = {'action': 'index_label', 'regex': '^service[.]name$'}
        shadowed = profile(resource_attributes={'attributes_config': rules['resource']},
                           scope_attributes=rules['scope'], log_attributes=rules['log'])
        for after in (self.before, shadowed):
            self.check_counts(self.data, self.before, after, (8, 0, 0, 0))
        policy = profile(resource_attributes={'ignore_defaults': True})
        report, trace = self.check_counts(logs(resource=[]), policy, policy, (0, 0, 0, 0))
        self.assertEqual(trace['decisions'], [])
        self.assertTrue(report['warnings'])

    def test_local_placement_counts_do_not_count_other_records(self):
        fixture = ROOT / 'examples/loki/stream-topology'
        data = read_json(fixture / 'logs.json')
        before, after = [load_profile(fixture / name) for name in ('before.yaml', 'after.yaml')]
        record = 'resourceLogs[2].scopeLogs[0].logRecords[0]'
        for key, expected in [(None, (2, 2, 2, 0)), ('route_old', (1, 1, 1, 0))]:
            with self.subTest(key=key):
                report, trace = self.check_counts(data, before, after, expected, record, key)
                self.assertEqual(report['changedPlacementCount'], 8)
                self.assertEqual(report['streamTopology'], read_json(fixture / 'expected-topology.json'))
                self.assertEqual((trace['beforeStreamIndex'], trace['afterStreamIndex']), (1, 0))

    def test_defaults_fallback_and_nested_leaves_count_individually(self):
        before = profile()
        after = profile(resource_attributes={'ignore_defaults': True}, log_attributes=[
            {'action': 'drop', 'attributes': ['net.peer']}])
        report, trace = self.check_counts(self.data, before, after, (8, 4, 4, 0))
        self.assertEqual(report['changedPlacementCount'], 4)
        self.assertEqual([d['sourceRef']['sourcePath'] for d in trace['decisions']
                          if d['placementChanged']],
                         [['service.name'], ['k8s.pod.name'], ['net.peer', 'port'], ['net.peer', 'host']])

    def test_no_record_selection_has_no_local_counts_or_trace(self):
        for data in (self.data, {'resourceLogs': []}):
            for detailed in (False, True):
                report = compare(data, self.before, self.after, include_records=detailed)
                self.assertNotIn('selectedRecordTrace', report)
                self.assertNotIn('decisionCounts', report)
