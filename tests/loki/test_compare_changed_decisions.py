"""Changed-only summaries preserve selection, raw traces and full validation."""

import copy
import json
import subprocess
import sys
import unittest

from .helpers import ROOT, attr, logs, profile
from otelwrench.loki.config import load_profile
from otelwrench.loki.mapping import compare
from otelwrench.loki.strict import PreviewError, read_json

FIXTURE = ROOT / 'examples/loki/explain'
RECORD = 'resourceLogs[0].scopeLogs[0].logRecords[0]'


class ChangedDecisionTests(unittest.TestCase):
    def setUp(self):
        self.data = read_json(FIXTURE / 'logs.json')
        self.before = load_profile(FIXTURE / 'profile.yaml')
        self.after = load_profile(FIXTURE / 'after.yaml')

    def selected(self, data, before, after, record_id=RECORD, **options):
        full = compare(data, before, after, record_id=record_id, **options)
        changed = compare(data, before, after, record_id=record_id,
                          changed_decisions_only=True, **options)
        # Compare every other field, including raw explain sides and all-record audits.
        restored = copy.deepcopy(changed)
        restored['selectedRecordTrace']['decisions'] = full['selectedRecordTrace']['decisions']
        restored['selectedRecordTrace']['decisionFilter'] = full['selectedRecordTrace']['decisionFilter']
        self.assertEqual(restored, full)
        return changed, changed['selectedRecordTrace']['decisions']

    def test_reason_only_and_same_pointer_selector_changes_exact_entries(self):
        golden = read_json(FIXTURE / 'expected-decisions.json')
        for after, expected in [(self.after, golden),
                                (load_profile(FIXTURE / 'selector-only.yaml'), copy.deepcopy(golden))]:
            if after is not self.after:
                expected[0]['after']['ruleRef'] = expected[0]['before']['ruleRef']
                expected[0]['changedFields'] = ['selector']
            with self.subTest(profile=after.describe()):
                report, decisions = self.selected(self.data, self.before, after)
                self.assertEqual(decisions, expected)
                self.assertEqual(report['changedPlacementCount'], 0)
                self.assertEqual(len(report['selectedRecordTrace']['before']['explanations']), 8)

    def test_identity_shadowed_only_and_empty_placements_have_empty_summary(self):
        rules = self.before.describe()
        rules['resource'][1] = {'action': 'index_label', 'regex': '^service[.]name$'}
        shadowed = profile(resource_attributes={'attributes_config': rules['resource']},
                           scope_attributes=rules['scope'], log_attributes=rules['log'])
        for after in (self.before, shadowed):
            _, decisions = self.selected(self.data, self.before, after)
            self.assertEqual(decisions, [])
        policy = profile(resource_attributes={'ignore_defaults': True})
        _, decisions = self.selected(logs(resource=[]), policy, policy)
        self.assertEqual(decisions, [])

    def test_key_filter_applies_before_change_filter_without_hiding_values(self):
        _, changed = self.selected(self.data, self.before, self.after, key='service_name')
        self.assertEqual(changed, read_json(FIXTURE / 'expected-decisions.json'))
        report, unchanged = self.selected(self.data, self.before, self.after, key='debug_private_value')
        self.assertEqual(unchanged, [])
        for side in ('before', 'after'):
            trace = report['selectedRecordTrace'][side]
            self.assertEqual(len(trace['explanations']), 1)
            self.assertEqual(trace['explanations'][0]['final']['placement'], 'drop')
            self.assertEqual(len(trace['record']['placements']), 8)
            self.assertTrue(trace['record']['dropped'])
        # Empty summary is local: another key still has a winning-selector change.
        self.assertNotEqual(report['selectedRecordTrace']['before']['profile'],
                            report['selectedRecordTrace']['after']['profile'])

    def test_changed_placements_preserve_order_topology_and_optional_audit(self):
        fixture = ROOT / 'examples/loki/stream-topology'
        data = read_json(fixture / 'logs.json')
        before, after = [load_profile(fixture / name) for name in ('before.yaml', 'after.yaml')]
        report, decisions = self.selected(data, before, after,
                                          'resourceLogs[2].scopeLogs[0].logRecords[0]', include_records=True)
        self.assertEqual([d['key'] for d in decisions], ['route_old', 'route_new'])
        self.assertEqual([d['sourceRef']['sourcePath'] for d in decisions], [['route.old'], ['route.new']])
        self.assertTrue(all(d['winnerChanged'] and d['placementChanged'] for d in decisions))
        self.assertEqual(report['changedPlacementCount'], 8)
        self.assertEqual(report['streamTopology'], read_json(fixture / 'expected-topology.json'))
        trace = report['selectedRecordTrace']
        self.assertEqual((trace['beforeStreamIndex'], trace['afterStreamIndex']), (1, 0))

    def test_default_fallback_dropped_and_nested_changes_keep_provenance(self):
        before = profile()
        after = profile(resource_attributes={'ignore_defaults': True}, log_attributes=[
            {'action': 'drop', 'attributes': ['net.peer']}])
        report, decisions = self.selected(self.data, before, after)
        self.assertEqual([d['key'] for d in decisions], ['service_name', 'k8s_pod_name',
                                                       'net_peer_port', 'net_peer_host'])
        self.assertEqual(decisions[0]['before']['ruleRef'], 'defaults.json#/attributes/16')
        self.assertEqual(decisions[0]['after']['ruleRef'], 'builtin:structured_metadata')
        self.assertEqual(decisions[2]['sourceRef']['sourcePath'], ['net.peer', 'port'])
        self.assertEqual(decisions[2]['after']['placement'], 'drop')
        self.assertTrue(all(d['winnerChanged'] and d['placementChanged'] for d in decisions))
        self.assertEqual(report['changedPlacementCount'], 4)

    def test_selection_errors_and_full_capture_errors_remain_errors(self):
        for selection, message in [({}, '--changed-decisions-only requires --record-id'),
                                   ({'include_records': True}, '--changed-decisions-only requires --record-id'),
                                   ({'record_id': 'missing'}, 'record ID not found'),
                                   ({'record_id': ''}, 'record ID not found'),
                                   ({'record_id': RECORD, 'key': 'service.name'}, 'normalized output key not found')]:
            with self.subTest(selection=selection), self.assertRaisesRegex(PreviewError, message):
                compare(self.data, self.before, self.after, changed_decisions_only=True, **selection)
        invalid_later = copy.deepcopy(self.data)
        invalid_later['resourceLogs'][0]['scopeLogs'][0]['logRecords'].append({'timeUnixNano': '0'})
        collision = logs(log=[attr('debug.private.value'), attr('debug_private_value')])
        for data in (invalid_later, collision):
            with self.subTest(data=data):
                with self.assertRaises(PreviewError) as full:
                    compare(data, self.before, self.after)
                with self.assertRaises(PreviewError) as filtered:
                    compare(data, self.before, self.after, record_id='missing', key='missing',
                            changed_decisions_only=True)
                self.assertEqual(str(filtered.exception), str(full.exception))

    def test_cli_success_dependency_and_unknown_selection(self):
        command = [sys.executable, '-m', 'otelwrench', 'loki', 'compare', str(FIXTURE / 'logs.json'),
                   '--before', str(FIXTURE / 'profile.yaml'), '--after', str(FIXTURE / 'after.yaml'),
                   '--changed-decisions-only']
        result = subprocess.run(command + ['--record-id', RECORD], text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['selectedRecordTrace']['decisions'],
                         read_json(FIXTURE / 'expected-decisions.json'))
        for extra, message in [([], '--changed-decisions-only requires --record-id'),
                               (['--include-records'], '--changed-decisions-only requires --record-id'),
                               (['--record-id', 'missing'], 'record ID not found'),
                               (['--record-id', RECORD, '--key', 'missing'], 'normalized output key not found')]:
            with self.subTest(extra=extra):
                result = subprocess.run(command + extra, text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn(message, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
