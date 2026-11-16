"""Exact additive selected field totals, after key selection and before every view."""

import unittest

from .helpers import ROOT, logs, profile
from otelwrench.loki.config import load_profile
from otelwrench.loki.mapping import compare, explain
from otelwrench.loki.strict import read_json

FIXTURE = ROOT / 'examples/loki/explain'
RECORD = 'resourceLogs[0].scopeLogs[0].logRecords[0]'
VIEWS = ({}, {'changed_decisions_only': True}, {'placement_decisions_only': True},
         {'reason_decisions_only': True}, {'selector_decisions_only': True}, {'rule_ref_decisions_only': True})


def field_count_cases():
    """Hand-authored expectations; never compute totals from implementation output."""
    data = read_json(FIXTURE / 'logs.json')
    before = load_profile(FIXTURE / 'profile.yaml')
    after = load_profile(FIXTURE / 'after.yaml')
    selector = load_profile(FIXTURE / 'selector-only.yaml')
    rules = before.describe()
    shadowed = profile(resource_attributes={'attributes_config': [rules['resource'][0],
                       {'action': 'index_label', 'regex': '^service[.]name$'}]},
                       scope_attributes=rules['scope'], log_attributes=rules['log'])
    regex = {'action': 'index_label', 'regex': '^service[.]name$'}
    pointer_before = profile(resource_attributes={'attributes_config': [regex]})
    pointer_after = profile(resource_attributes={'attributes_config': [
        {'action': 'drop', 'attributes': ['absent']}, regex]})
    action = profile(resource_attributes={'attributes_config': [
        {'action': 'drop', 'regex': '^service[.]name$'}]})
    # Same-pointer selector edit plus two inherited nested-leaf action edits.
    mixed = profile(resource_attributes={'attributes_config': selector.describe()['resource']},
                    scope_attributes=rules['scope'], log_attributes=[
                        {'action': 'drop', 'regex': '^net[.]'}, *rules['log'][1:]])
    replacement = profile(resource_attributes={'attributes_config': selector.describe()['resource']},
                          scope_attributes=rules['scope'], log_attributes=[
                              {'action': 'drop', 'attributes': ['net.peer']}, *rules['log'][1:]])
    defaults = profile()
    demote = profile(resource_attributes={'ignore_defaults': True}, log_attributes=[
        {'action': 'drop', 'attributes': ['net.peer']}])
    topology = ROOT / 'examples/loki/stream-topology'
    # name, capture, before, after, record ID, all-key counts, selected-key counts
    return [
        ('literal-to-RE2', data, before, after, RECORD, (1, 0, 1),
         {'service_name': (1, 0, 1), 'debug_private_value': (0, 0, 0)}),
        ('selector-only', data, before, selector, RECORD, (0, 0, 1), {'service_name': (0, 0, 1)}),
        ('pointer-only', data, pointer_before, pointer_after, RECORD, (1, 0, 0), {'service_name': (1, 0, 0)}),
        ('action-only', data, pointer_before, action, RECORD, (0, 1, 0), {'service_name': (0, 1, 0)}),
        ('mixed-same-regex', data, before, mixed, RECORD, (0, 2, 1),
         {'service_name': (0, 0, 1), 'net_peer_port': (0, 1, 0), 'net_peer_host': (0, 1, 0)}),
        ('mixed-selector-replacement', data, before, replacement, RECORD, (0, 2, 3),
         {'service_name': (0, 0, 1), 'net_peer_port': (0, 1, 1), 'net_peer_host': (0, 1, 1)}),
        ('identity', data, before, before, RECORD, (0, 0, 0),
         {key: (0, 0, 0) for key in ('k8s_pod_name', 'empty', 'scope_name', 'debug_private_value')}),
        ('shadowed-only', data, before, shadowed, RECORD, (0, 0, 0), {'service_name': (0, 0, 0)}),
        ('defaults-fallback-nested', data, defaults, demote, RECORD, (4, 4, 2),
         {'service_name': (1, 1, 0), 'k8s_pod_name': (1, 1, 0), 'net_peer_port': (1, 1, 1)}),
        ('default-to-explicit', data, defaults, pointer_before, RECORD, (1, 0, 1),
         {'service_name': (1, 0, 1)}),
        ('zero-leaf', logs(resource=[]), demote, demote, RECORD, (0, 0, 0), {}),
        ('topology', read_json(topology / 'logs.json'), load_profile(topology / 'before.yaml'),
         load_profile(topology / 'after.yaml'), 'resourceLogs[2].scopeLogs[0].logRecords[0]',
         (2, 2, 2), {'route_old': (1, 1, 1), 'route_new': (1, 1, 1)}),
    ]


class DecisionFieldCountTests(unittest.TestCase):
    def test_exact_field_counts_in_every_view_after_normalized_key_selection(self):
        for name, data, before, after, record_id, all_counts, keys in field_count_cases():
            for key, values in [(None, all_counts), *keys.items()]:
                full = compare(data, before, after, record_id=record_id, key=key, include_records=True)
                for options, mode in zip(VIEWS, ('all', 'winner_changed', 'placement_changed', 'reason_changed', 'selector_changed', 'rule_ref_changed'), strict=True):
                    with self.subTest(case=name, key=key, view=mode):
                        candidate = compare(data, before, after, record_id=record_id, key=key,
                                            include_records=True, **options)
                        trace = candidate['selectedRecordTrace']
                        self.assertEqual(set(trace), {'recordId', 'before', 'after', 'beforeStreamIndex',
                                                     'afterStreamIndex', 'decisions', 'decisionCounts',
                                                     'decisionFieldCounts', 'decisionCountsBySource', 'decisionFilter'})
                        self.assertEqual(trace['decisionFieldCounts'], dict(zip(('ruleRef', 'placement', 'selector'), values)))
                        self.assertTrue(all(type(n) is int and n >= 0 for n in trace['decisionFieldCounts'].values()))
                        self.assertEqual(trace['decisionFilter'], mode)
                        self.assertEqual(trace['decisionFieldCounts']['placement'], trace['decisionCounts']['placementChangedCount'])
                        # Filtering can only change the decisions array and its label.
                        trace['decisions'] = full['selectedRecordTrace']['decisions']
                        trace['decisionFilter'] = 'all'
                        self.assertEqual(candidate, full)
                trace = full.pop('selectedRecordTrace')
                for side, policy in [('before', before), ('after', after)]:
                    self.assertEqual(trace[side], explain(data, policy, record_id=record_id, key=key))
                self.assertEqual(full, compare(data, before, after, include_records=True))

    def test_unselected_compact_and_audit_reports_have_no_field_counters(self):
        for _, data, before, after, *_ in field_count_cases():
            for detailed in (False, True):
                report = compare(data, before, after, include_records=detailed)
                self.assertNotIn('selectedRecordTrace', report)
                self.assertNotIn('decisionFieldCounts', report)
                self.assertNotIn('decisionCounts', report)
