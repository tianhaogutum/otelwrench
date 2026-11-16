"""Concrete decision summaries separate winner edits from action/topology changes."""

import unittest

from .helpers import ROOT, attr, logs, profile
from otelwrench.loki.config import load_profile
from otelwrench.loki.mapping import compare, explain
from otelwrench.loki.strict import read_json

FIXTURE = ROOT / 'examples/loki/explain'
RECORD = 'resourceLogs[0].scopeLogs[0].logRecords[0]'
BASE = '/limits_config/otlp_config/resource_attributes/attributes_config/'


class CompareDecisionTests(unittest.TestCase):
    def setUp(self):
        self.data = read_json(FIXTURE / 'logs.json')
        self.before = load_profile(FIXTURE / 'profile.yaml')
        self.after = load_profile(FIXTURE / 'after.yaml')

    def selected(self, data, before, after, record_id=RECORD, key=None):
        report = compare(data, before, after, record_id=record_id, key=key)
        trace = report['selectedRecordTrace']
        decisions = trace['decisions']
        self.assertEqual(len(decisions), len(trace['before']['explanations']))
        for side, policy in [('before', before), ('after', after)]:
            self.assertEqual(trace[side], explain(data, policy, record_id=record_id, key=key))
            group = report['streamTopology'][side][trace[side + 'StreamIndex']]
            self.assertIn(record_id, group['recordIds'])
        compact = dict(report)
        del compact['selectedRecordTrace']
        self.assertEqual(compact, compare(data, before, after))
        for decision, entry in zip(decisions, trace['before']['explanations'], strict=True):
            self.assertEqual(set(decision), {'sourceRef', 'key', 'before', 'after',
                                            'placementChanged', 'winnerChanged', 'changedFields'})
            self.assertEqual(decision['winnerChanged'], bool(decision['changedFields']))
            self.assertEqual(decision['placementChanged'], 'placement' in decision['changedFields'])
            self.assertEqual(decision['sourceRef'], entry['sourceRef'])
            self.assertEqual(decision['key'], entry['final']['key'])
        return report, decisions

    def test_literal_to_re2_exact_golden_and_unchanged_global_counts(self):
        report, decisions = self.selected(self.data, self.before, self.after, key='service_name')
        self.assertEqual(decisions, read_json(FIXTURE / 'expected-decisions.json'))
        self.assertEqual(report['changedPlacementCount'], 0)
        self.assertFalse(report['streamTopology']['membershipChanged'])
        self.assertEqual(report['changedStreams'], [])

    def test_same_pointer_selector_change_is_a_winner_change(self):
        after = load_profile(FIXTURE / 'selector-only.yaml')
        _, decisions = self.selected(self.data, self.before, after, key='service_name')
        expected = read_json(FIXTURE / 'expected-decisions.json')
        expected[0]['after']['ruleRef'] = BASE + '0'
        expected[0]['changedFields'] = ['selector']
        self.assertEqual(decisions, expected)

    def test_only_winning_rule_matters_not_shadowed_unmatched_or_disabled_defaults(self):
        changed = self.before.describe()
        # Keep selected rule 0; change a matching shadowed rule and add an unmatched rule.
        changed['resource'][1] = {'action': 'index_label', 'regex': '^service[.]name$'}
        after = profile(resource_attributes={'ignore_defaults': True,
                        'attributes_config': changed['resource'] + [{'action': 'drop', 'attributes': ['absent']}]},
                        scope_attributes=changed['scope'], log_attributes=changed['log'])
        report, decisions = self.selected(self.data, self.before, after, key='service_name')
        self.assertFalse(decisions[0]['winnerChanged'])
        self.assertFalse(decisions[0]['placementChanged'])
        self.assertEqual(decisions[0]['changedFields'], [])
        self.assertNotEqual(report['selectedRecordTrace']['before']['explanations'],
                            report['selectedRecordTrace']['after']['explanations'])

    def test_changed_placement_retains_sorted_stream_indexes_and_topology(self):
        fixture = ROOT / 'examples/loki/stream-topology'
        data = read_json(fixture / 'logs.json')
        before, after = [load_profile(fixture / name) for name in ('before.yaml', 'after.yaml')]
        report, decisions = self.selected(data, before, after,
                                          'resourceLogs[2].scopeLogs[0].logRecords[0]', 'route_old')
        self.assertEqual(decisions[0]['before'], {'ruleRef': BASE + '0', 'placement': 'index_label',
                                                'selector': {'attributes': ['route.old']}})
        self.assertEqual(decisions[0]['after'], {'ruleRef': 'builtin:structured_metadata',
                                               'placement': 'structured_metadata', 'selector': None})
        self.assertTrue(decisions[0]['winnerChanged'])
        self.assertTrue(decisions[0]['placementChanged'])
        self.assertEqual(decisions[0]['changedFields'], ['ruleRef', 'placement', 'selector'])
        trace = report['selectedRecordTrace']
        self.assertEqual((trace['beforeStreamIndex'], trace['afterStreamIndex']), (1, 0))
        self.assertEqual(report['changedPlacementCount'], 8)
        self.assertEqual(report['streamTopology'], read_json(fixture / 'expected-topology.json'))

    def test_action_change_at_same_pointer_and_selector(self):
        before = profile(resource_attributes={'attributes_config': [
            {'action': 'index_label', 'attributes': ['service.name']}]})
        after = profile(resource_attributes={'attributes_config': [
            {'action': 'drop', 'attributes': ['service.name']}]})
        _, decisions = self.selected(self.data, before, after, key='service_name')
        a, b = decisions[0]['before'], decisions[0]['after']
        self.assertEqual(a['ruleRef'], b['ruleRef'])
        self.assertEqual(a['selector'], b['selector'])
        self.assertTrue(decisions[0]['placementChanged'])
        self.assertTrue(decisions[0]['winnerChanged'])
        self.assertEqual(decisions[0]['changedFields'], ['placement'])

    def test_identity_all_sources_defaults_fallback_fixed_nested_and_dropped(self):
        _, decisions = self.selected(self.data, self.before, self.before)
        self.assertEqual(len(decisions), 8)
        by_key = {entry['key']: entry for entry in decisions}
        for entry in decisions:
            self.assertEqual(entry['changedFields'], [])
            self.assertEqual(entry['before'], entry['after'])
            self.assertFalse(entry['placementChanged'])
            self.assertFalse(entry['winnerChanged'])
        for key, ref in [('k8s_pod_name', 'defaults.json#/attributes/12'),
                         ('empty', 'builtin:structured_metadata'), ('scope_name', 'builtin:fixed_otlp_field')]:
            self.assertEqual(by_key[key]['before']['ruleRef'], ref)
            self.assertIsNone(by_key[key]['before']['selector'])
        self.assertEqual(by_key['net_peer_port']['sourceRef']['sourcePath'], ['net.peer', 'port'])
        self.assertEqual(by_key['net_peer_port']['before']['selector'], {'regex': '^net[.]'})
        self.assertEqual(by_key['debug_private_value']['before']['placement'], 'drop')
        self.assertEqual(by_key['internal_component']['sourceRef']['source'], 'scope')
        _, selected = self.selected(self.data, self.before, self.before, key='debug_private_value')
        self.assertEqual(selected, [by_key['debug_private_value']])
        # A literal joined key and a nested path can look alike; keep exact provenance.
        nested = logs(log=[attr('net.peer', {'kvlistValue': {'values': [attr('port')]}})])
        literal = logs(log=[attr('net.peer_port')])
        for data, path in [(nested, ['net.peer', 'port']), (literal, ['net.peer_port'])]:
            _, selected = self.selected(data, self.before, self.before, key='net_peer_port')
            self.assertEqual(selected[0]['sourceRef']['sourcePath'], path)

    def test_default_to_explicit_same_action_changes_winner_but_not_placement(self):
        before = profile()
        after = profile(resource_attributes={'attributes_config': [
            {'action': 'index_label', 'attributes': ['service.name']}]})
        _, decisions = self.selected(self.data, before, after, key='service_name')
        self.assertEqual(decisions[0]['before'], {'ruleRef': 'defaults.json#/attributes/16',
                                                'placement': 'index_label', 'selector': None})
        self.assertFalse(decisions[0]['placementChanged'])
        self.assertTrue(decisions[0]['winnerChanged'])
        self.assertEqual(decisions[0]['changedFields'], ['ruleRef', 'selector'])

    def test_no_placements_produce_empty_decisions(self):
        policy = profile(resource_attributes={'ignore_defaults': True})
        _, decisions = self.selected(logs(resource=[]), policy, policy)
        self.assertEqual(decisions, [])

    def test_selector_comparison_is_literal_including_attribute_order(self):
        before = profile(resource_attributes={'attributes_config': [
            {'action': 'index_label', 'attributes': ['service.name', 'k8s.pod.name']}]})
        after = profile(resource_attributes={'attributes_config': [
            {'action': 'index_label', 'attributes': ['k8s.pod.name', 'service.name']}]})
        _, decisions = self.selected(self.data, before, after, key='service_name')
        self.assertTrue(decisions[0]['winnerChanged'])
        self.assertFalse(decisions[0]['placementChanged'])
        self.assertEqual(decisions[0]['changedFields'], ['selector'])

    def test_regex_text_and_rule_pointer_compare_literally(self):
        before = profile(resource_attributes={'attributes_config': [
            {'action': 'index_label', 'regex': '^service[.]name$'}]})
        for rules, expected in [
                ([{'action': 'index_label', 'regex': '^service\\.name$'}], ['selector']),
                ([{'action': 'drop', 'attributes': ['absent']},
                  {'action': 'index_label', 'regex': '^service[.]name$'}], ['ruleRef'])]:
            after = profile(resource_attributes={'attributes_config': rules})
            _, decisions = self.selected(self.data, before, after, key='service_name')
            self.assertEqual(decisions[0]['changedFields'], expected)
