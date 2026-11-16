"""Saved selected reports identify their view even when arrays look identical."""

import copy
import unittest

from .helpers import ROOT, logs, profile
from otelwrench.loki.config import load_profile
from otelwrench.loki.mapping import compare, explain
from otelwrench.loki.strict import read_json

FIXTURE = ROOT / 'examples/loki/explain'
RECORD = 'resourceLogs[0].scopeLogs[0].logRecords[0]'
MODES = [('all', {}), ('winner_changed', {'changed_decisions_only': True}),
         ('placement_changed', {'placement_decisions_only': True}),
         ('reason_changed', {'reason_decisions_only': True})]


class DecisionFilterTests(unittest.TestCase):
    def setUp(self):
        self.data = read_json(FIXTURE / 'logs.json')
        self.before = load_profile(FIXTURE / 'profile.yaml')
        self.after = load_profile(FIXTURE / 'after.yaml')

    def views(self, data, before, after, counts, lengths, record_id=RECORD, key=None, changed_fields=None):
        reports = []
        for (mode, options), length in zip(MODES, lengths, strict=True):
            with self.subTest(mode=mode, key=key):
                report = compare(data, before, after, record_id=record_id, key=key,
                                 include_records=True, **options)
                trace = report['selectedRecordTrace']
                self.assertEqual(trace['decisionFilter'], mode)
                self.assertEqual(trace['decisionCounts'], dict(zip(
                    ('selectedLeafCount', 'winnerChangedCount', 'placementChangedCount', 'reasonChangedCount'), counts)))
                self.assertTrue(all(type(value) is int and value >= 0
                                    for value in trace['decisionCounts'].values()))
                self.assertEqual(len(trace['decisions']), length)
                for decision in trace['decisions']:
                    self.assertEqual(decision['changedFields'], (changed_fields or {}).get(decision['key'], []))
                    self.assertEqual(decision['winnerChanged'], bool(decision['changedFields']))
                    self.assertEqual(decision['placementChanged'], 'placement' in decision['changedFields'])
                for side, policy in [('before', before), ('after', after)]:
                    self.assertEqual(trace[side], explain(data, policy, record_id=record_id, key=key))
                reports.append(report)
        self.assertEqual(len(reports), 4)
        self.assertEqual(len(reports[3]['selectedRecordTrace']['decisions']), counts[3])
        for report in reports[1:]:
            restored = copy.deepcopy(report)
            for field in ('decisions', 'decisionFilter'):
                restored['selectedRecordTrace'][field] = reports[0]['selectedRecordTrace'][field]
            self.assertEqual(restored, reports[0])
        compact = copy.deepcopy(reports[0])
        del compact['selectedRecordTrace']
        self.assertEqual(compact, compare(data, before, after, include_records=True))
        return [r['selectedRecordTrace'] for r in reports]

    def test_reason_only_and_same_pointer_views(self):
        for same_pointer, after in enumerate((self.after, load_profile(FIXTURE / 'selector-only.yaml'))):
            fields = {'service_name': ['selector'] if same_pointer else ['ruleRef', 'selector']}
            views = self.views(self.data, self.before, after, (8, 1, 0, 1), (8, 1, 0, 1), changed_fields=fields)
            self.assertEqual(views[1]['decisions'][0]['key'], 'service_name')
            self.assertEqual(views[2]['decisions'], [])
            expected = read_json(FIXTURE / 'expected-decisions.json')
            if same_pointer:
                expected[0]['after']['ruleRef'] = expected[0]['before']['ruleRef']
                expected[0]['changedFields'] = ['selector']
            self.assertEqual(views[3]['decisions'], expected)
            self.views(self.data, self.before, after, (1, 1, 0, 1), (1, 1, 0, 1),
                       key='service_name', changed_fields=fields)

    def test_mixed_and_topology_views_preserve_exact_membership_and_order(self):
        rules = load_profile(FIXTURE / 'selector-only.yaml').describe()
        rules['log'][0] = {'action': 'drop', 'attributes': ['net.peer']}
        after = profile(resource_attributes={'attributes_config': rules['resource']},
                        scope_attributes=rules['scope'], log_attributes=rules['log'])
        fields = {'service_name': ['selector'], 'net_peer_port': ['placement', 'selector'],
                  'net_peer_host': ['placement', 'selector']}
        views = self.views(self.data, self.before, after, (8, 3, 2, 1), (8, 3, 2, 1), changed_fields=fields)
        self.assertEqual([(d['key'], d['changedFields']) for d in views[0]['decisions']], [
            ('service_name', ['selector']), ('k8s_pod_name', []), ('internal_component', []),
            ('net_peer_port', ['placement', 'selector']), ('net_peer_host', ['placement', 'selector']),
            ('debug_private_value', []), ('empty', []), ('scope_name', [])])
        self.assertEqual([d['key'] for d in views[1]['decisions']],
                         ['service_name', 'net_peer_port', 'net_peer_host'])
        self.assertEqual([d['key'] for d in views[2]['decisions']], ['net_peer_port', 'net_peer_host'])
        expected = read_json(FIXTURE / 'expected-decisions.json')
        expected[0]['after']['ruleRef'] = expected[0]['before']['ruleRef']
        expected[0]['changedFields'] = ['selector']
        self.assertEqual(views[3]['decisions'], expected)
        fixture = ROOT / 'examples/loki/stream-topology'
        policies = [load_profile(fixture / name) for name in ('before.yaml', 'after.yaml')]
        record = 'resourceLogs[2].scopeLogs[0].logRecords[0]'
        fields = {'route_old': ['ruleRef', 'placement', 'selector'],
                  'route_new': ['ruleRef', 'placement', 'selector']}
        views = self.views(read_json(fixture / 'logs.json'), *policies, (2, 2, 2, 0), (2, 2, 2, 0),
                           record, changed_fields=fields)
        self.views(read_json(fixture / 'logs.json'), *policies, (1, 1, 1, 0), (1, 1, 1, 0),
                   record, key='route_old', changed_fields=fields)
        for trace in views[:3]:
            self.assertEqual([d['key'] for d in trace['decisions']], ['route_old', 'route_new'])
        self.assertEqual(views[3]['decisions'], [])
        for trace in views:
            self.assertEqual((trace['beforeStreamIndex'], trace['afterStreamIndex']), (1, 0))

    def test_default_fallback_changes_and_fixed_fields_in_all_views(self):
        before = profile()
        after = profile(resource_attributes={'ignore_defaults': True}, log_attributes=[
            {'action': 'drop', 'attributes': ['net.peer']}])
        self.views(self.data, before, after, (8, 4, 4, 0), (8, 4, 4, 0), changed_fields={
            'service_name': ['ruleRef', 'placement'], 'k8s_pod_name': ['ruleRef', 'placement'],
            'net_peer_port': ['ruleRef', 'placement', 'selector'],
            'net_peer_host': ['ruleRef', 'placement', 'selector']})

    def test_action_only_same_pointer_and_selector_in_all_views(self):
        policies = [profile(resource_attributes={'attributes_config': [
            {'action': action, 'attributes': ['service.name']}]}) for action in ('index_label', 'drop')]
        self.views(self.data, *policies, (1, 1, 1, 0), (1, 1, 1, 0), key='service_name',
                   changed_fields={'service_name': ['placement']})

    def test_identity_shadowed_unchanged_key_and_zero_leaf_views_stay_explicit(self):
        rules = self.before.describe()
        rules['resource'][1] = {'action': 'index_label', 'regex': '^service[.]name$'}
        shadowed = profile(resource_attributes={'attributes_config': rules['resource']},
                           scope_attributes=rules['scope'], log_attributes=rules['log'])
        for after in (self.before, shadowed):
            self.views(self.data, self.before, after, (8, 0, 0, 0), (8, 0, 0, 0))
        views = self.views(self.data, self.before, self.after, (1, 0, 0, 0), (1, 0, 0, 0), key='debug_private_value')
        for trace in views:
            for side in ('before', 'after'):
                self.assertEqual(trace[side]['explanations'][0]['final']['value'], 'SYNTHETIC-ONLY')
                self.assertEqual(len(trace[side]['record']['placements']), 8)
        policy = profile(resource_attributes={'ignore_defaults': True})
        self.views(logs(resource=[]), policy, policy, (0, 0, 0, 0), (0, 0, 0, 0))

    def test_no_selection_emits_no_view_metadata(self):
        for data in (self.data, {'resourceLogs': []}):
            for detailed in (False, True):
                report = compare(data, self.before, self.after, include_records=detailed)
                self.assertNotIn('selectedRecordTrace', report)
                self.assertNotIn('decisionFilter', report)
                self.assertNotIn('reasonChangedCount', report)
                self.assertNotIn('changedFields', report)
