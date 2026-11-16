"""Placement-only summaries exclude reason changes without altering local totals."""

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
RULES = '/limits_config/otlp_config/resource_attributes/attributes_config/'


class PlacementDecisionTests(unittest.TestCase):
    def setUp(self):
        self.data = read_json(FIXTURE / 'logs.json')
        self.before = load_profile(FIXTURE / 'profile.yaml')
        self.after = load_profile(FIXTURE / 'after.yaml')

    def selected(self, data, before, after, counts, record_id=RECORD, **options):
        full = compare(data, before, after, record_id=record_id, **options)
        filtered = compare(data, before, after, record_id=record_id,
                           placement_decisions_only=True, **options)
        trace = filtered['selectedRecordTrace']
        self.assertEqual(trace['decisionCounts'], dict(zip(
            ('selectedLeafCount', 'winnerChangedCount', 'placementChangedCount', 'reasonChangedCount'), counts)))
        restored = copy.deepcopy(filtered)
        restored['selectedRecordTrace']['decisions'] = full['selectedRecordTrace']['decisions']
        restored['selectedRecordTrace']['decisionFilter'] = full['selectedRecordTrace']['decisionFilter']
        self.assertEqual(restored, full)  # raw sides, pre-filter totals, indexes, global fields and audit
        return filtered, trace['decisions']

    def test_reason_only_and_same_pointer_changes_are_excluded(self):
        for after in (self.after, load_profile(FIXTURE / 'selector-only.yaml')):
            with self.subTest(profile=after.describe()):
                report, decisions = self.selected(self.data, self.before, after, (8, 1, 0, 1))
                self.assertEqual(decisions, [])
                self.assertEqual(report['changedPlacementCount'], 0)
                self.assertEqual(len(report['selectedRecordTrace']['before']['explanations']), 8)
                self.selected(self.data, self.before, after, (1, 1, 0, 1), key='service_name')

    def test_placement_changes_have_exact_provenance_and_before_order(self):
        fixture = ROOT / 'examples/loki/stream-topology'
        data = read_json(fixture / 'logs.json')
        before, after = [load_profile(fixture / name) for name in ('before.yaml', 'after.yaml')]
        record = 'resourceLogs[2].scopeLogs[0].logRecords[0]'
        report, decisions = self.selected(data, before, after, (2, 2, 2, 0), record, include_records=True)
        fallback = {'ruleRef': 'builtin:structured_metadata', 'placement': 'structured_metadata', 'selector': None}
        old = {'ruleRef': RULES + '0', 'placement': 'index_label', 'selector': {'attributes': ['route.old']}}
        new = {'ruleRef': RULES + '0', 'placement': 'index_label', 'selector': {'attributes': ['route.new']}}
        expected = [
            {'sourceRef': {'recordId': record, 'source': 'resource', 'sourcePath': ['route.old']},
             'key': 'route_old', 'before': old, 'after': fallback, 'placementChanged': True, 'winnerChanged': True,
             'changedFields': ['ruleRef', 'placement', 'selector']},
            {'sourceRef': {'recordId': record, 'source': 'resource', 'sourcePath': ['route.new']},
             'key': 'route_new', 'before': fallback, 'after': new, 'placementChanged': True, 'winnerChanged': True,
             'changedFields': ['ruleRef', 'placement', 'selector']}]
        self.assertEqual(decisions, expected)
        self.assertEqual(report['changedPlacementCount'], 8)
        self.assertEqual(report['streamTopology'], read_json(fixture / 'expected-topology.json'))
        trace = report['selectedRecordTrace']
        self.assertEqual((trace['beforeStreamIndex'], trace['afterStreamIndex']), (1, 0))
        _, selected = self.selected(data, before, after, (1, 1, 1, 0), record, key='route_old')
        self.assertEqual(selected, expected[:1])

    def test_mixed_winner_and_action_changes_filter_only_actions(self):
        rules = load_profile(FIXTURE / 'selector-only.yaml').describe()
        rules['log'][0] = {'action': 'drop', 'attributes': ['net.peer']}
        after = profile(resource_attributes={'attributes_config': rules['resource']},
                        scope_attributes=rules['scope'], log_attributes=rules['log'])
        report, decisions = self.selected(self.data, self.before, after, (8, 3, 2, 1))
        self.assertEqual([d['key'] for d in decisions], ['net_peer_port', 'net_peer_host'])
        self.assertEqual([d['sourceRef']['sourcePath'] for d in decisions],
                         [['net.peer', 'port'], ['net.peer', 'host']])
        self.assertTrue(all(d['placementChanged'] and d['winnerChanged'] for d in decisions))
        self.assertEqual(report['changedPlacementCount'], 2)
        self.assertEqual(len(report['selectedRecordTrace']['after']['record']['dropped']), 4)

    def test_identity_shadowed_empty_and_unchanged_key_preserve_counts_and_values(self):
        rules = self.before.describe()
        rules['resource'][1] = {'action': 'index_label', 'regex': '^service[.]name$'}
        shadowed = profile(resource_attributes={'attributes_config': rules['resource']},
                           scope_attributes=rules['scope'], log_attributes=rules['log'])
        for after in (self.before, shadowed):
            _, decisions = self.selected(self.data, self.before, after, (8, 0, 0, 0))
            self.assertEqual(decisions, [])
        policy = profile(resource_attributes={'ignore_defaults': True})
        report, decisions = self.selected(logs(resource=[]), policy, policy, (0, 0, 0, 0))
        self.assertEqual(decisions, [])
        self.assertTrue(report['warnings'])
        report, decisions = self.selected(self.data, self.before, self.after, (1, 0, 0, 0), key='debug_private_value')
        self.assertEqual(decisions, [])
        for side in ('before', 'after'):
            trace = report['selectedRecordTrace'][side]
            self.assertEqual(len(trace['record']['placements']), 8)
            self.assertEqual(len(trace['explanations']), 1)
            self.assertEqual(trace['explanations'][0]['final']['value'], 'SYNTHETIC-ONLY')
            self.assertEqual(trace['explanations'][0]['final']['placement'], 'drop')

    def test_dependencies_conflict_and_unknown_selection_fail(self):
        for options, message in [
                ({}, '--placement-decisions-only requires --record-id'),
                ({'include_records': True}, '--placement-decisions-only requires --record-id'),
                ({'changed_decisions_only': True}, 'cannot combine'),
                ({'record_id': RECORD, 'changed_decisions_only': True}, 'cannot combine'),
                ({'record_id': 'missing'}, 'record ID not found'),
                ({'record_id': RECORD, 'key': 'service.name'}, 'normalized output key not found')]:
            with self.subTest(options=options), self.assertRaisesRegex(PreviewError, message):
                compare(self.data, self.before, self.after, placement_decisions_only=True, **options)

    def test_full_capture_validation_precedes_selection_and_filter(self):
        later = copy.deepcopy(self.data)
        later['resourceLogs'][0]['scopeLogs'][0]['logRecords'].append({'timeUnixNano': '0'})
        collision = logs(log=[attr('debug.private.value'), attr('debug_private_value')])
        empty = copy.deepcopy(self.data)
        empty['resourceLogs'][0]['resource']['attributes'][1]['value'] = {}
        for data in (later, collision, empty):
            with self.subTest(data=data):
                with self.assertRaises(PreviewError) as full:
                    compare(data, self.before, self.after)
                with self.assertRaises(PreviewError) as filtered:
                    compare(data, self.before, self.after, record_id='missing', key='missing',
                            placement_decisions_only=True)
                self.assertEqual(str(filtered.exception), str(full.exception))

    def test_cli_success_and_option_errors_are_atomic(self):
        command = [sys.executable, '-m', 'otelwrench', 'loki', 'compare', str(FIXTURE / 'logs.json'),
                   '--before', str(FIXTURE / 'profile.yaml'), '--after', str(FIXTURE / 'after.yaml'),
                   '--placement-decisions-only']
        result = subprocess.run(command + ['--record-id', RECORD], text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        trace = json.loads(result.stdout)['selectedRecordTrace']
        self.assertEqual(trace['decisions'], [])
        self.assertEqual(trace['decisionCounts'], {'selectedLeafCount': 8, 'winnerChangedCount': 1, 'placementChangedCount': 0, 'reasonChangedCount': 1})
        for extra, message in [([], '--placement-decisions-only requires --record-id'),
                               (['--include-records'], '--placement-decisions-only requires --record-id'),
                               (['--changed-decisions-only'], 'not allowed with argument'),
                               (['--record-id', RECORD, '--changed-decisions-only'], 'not allowed with argument'),
                               (['--record-id', 'missing'], 'record ID not found'),
                               (['--record-id', RECORD, '--key', 'missing'], 'normalized output key not found')]:
            with self.subTest(extra=extra):
                result = subprocess.run(command + extra, text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn(message, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
