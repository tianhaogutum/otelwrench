"""Literal winning rule-reference filtering, including placement changes, after validated selection."""

import copy
import itertools
import json
import subprocess
import sys
import unittest

from .helpers import attr, logs
from .test_compare_decision_field_counts import FIXTURE, RECORD, field_count_cases
from otelwrench.loki.config import load_profile
from otelwrench.loki.mapping import compare
from otelwrench.loki.strict import PreviewError, read_json

EXPECTED_KEYS = {
    'literal-to-RE2': ['service_name'],
    'selector-only': [],
    'pointer-only': ['service_name'],
    'action-only': [],
    'mixed-same-regex': [],
    'mixed-selector-replacement': [],
    'identity': [],
    'shadowed-only': [],
    'defaults-fallback-nested': ['service_name', 'k8s_pod_name', 'net_peer_port', 'net_peer_host'],
    'default-to-explicit': ['service_name'],
    'zero-leaf': [],
    'topology': ['route_old', 'route_new'],
}

ALL_COUNTS = {
    'literal-to-RE2': (8, 1, 0, 1), 'selector-only': (8, 1, 0, 1),
    'pointer-only': (8, 1, 0, 1), 'action-only': (8, 1, 1, 0),
    'mixed-same-regex': (8, 3, 2, 1), 'mixed-selector-replacement': (8, 3, 2, 1),
    'identity': (8, 0, 0, 0), 'shadowed-only': (8, 0, 0, 0),
    'defaults-fallback-nested': (8, 4, 4, 0), 'default-to-explicit': (8, 1, 0, 1),
    'zero-leaf': (0, 0, 0, 0), 'topology': (2, 2, 2, 0),
}


class RuleRefDecisionTests(unittest.TestCase):
    def test_exact_membership_order_and_entire_report_invariance(self):
        for name, data, before, after, record_id, totals, keys in field_count_cases():
            for key, fields in [(None, totals), *keys.items()]:
                for detailed in (False, True):
                    with self.subTest(case=name, key=key, audit=detailed):
                        args = dict(record_id=record_id, key=key, include_records=detailed)
                        full = compare(data, before, after, **args)
                        report = compare(data, before, after, rule_ref_decisions_only=True, **args)
                        trace = report['selectedRecordTrace']
                        wanted = [k for k in EXPECTED_KEYS[name] if key is None or key == k]
                        self.assertEqual(trace['decisionFilter'], 'rule_ref_changed')
                        self.assertEqual([d['key'] for d in trace['decisions']], wanted)
                        self.assertEqual(trace['decisionFieldCounts'],
                                         dict(zip(('ruleRef', 'placement', 'selector'), fields)))
                        self.assertEqual(len(trace['decisions']), fields[0])
                        if key is None:
                            expected_counts = ALL_COUNTS[name]
                        else:
                            expected_counts = (1, int(any(fields)), fields[1], int(any(fields) and not fields[1]))
                        self.assertEqual(trace['decisionCounts'], dict(zip(
                            ('selectedLeafCount', 'winnerChangedCount', 'placementChangedCount', 'reasonChangedCount'),
                            expected_counts)))
                        # Exact entry parity includes raw selectors, pointers, source refs and flags.
                        self.assertEqual(trace['decisions'], [d for d in full['selectedRecordTrace']['decisions']
                                                             if d['key'] in wanted])
                        for field in ('decisions', 'decisionFilter'):
                            trace[field] = full['selectedRecordTrace'][field]
                        # Both full raw sides/values, pre-filter counters, indexes, audits and globals.
                        self.assertEqual(report, full)


class RuleRefDecisionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.data = read_json(FIXTURE / 'logs.json')
        self.before = load_profile(FIXTURE / 'profile.yaml')
        self.after = load_profile(FIXTURE / 'after.yaml')

    def test_api_dependencies_all_conflicts_and_unknown_selections(self):
        for detailed in (False, True):
            with self.subTest(audit=detailed), self.assertRaisesRegex(
                    PreviewError, '--rule-ref-decisions-only requires --record-id'):
                compare(self.data, self.before, self.after, include_records=detailed, rule_ref_decisions_only=True)
        others = ('changed_decisions_only', 'placement_decisions_only', 'reason_decisions_only', 'selector_decisions_only')
        for size in range(1, len(others) + 1):
            for flags in itertools.combinations(others, size):
                for record in (None, RECORD):
                    with self.subTest(flags=flags, record=record), self.assertRaisesRegex(PreviewError, 'cannot combine'):
                        compare(self.data, self.before, self.after, record_id=record,
                                rule_ref_decisions_only=True, **dict.fromkeys(flags, True))
        for data, selection, message in [
                (self.data, {'record_id': 'missing'}, 'record ID not found'),
                (self.data, {'record_id': RECORD, 'key': 'missing'}, 'normalized output key not found'),
                (self.data, {'record_id': RECORD, 'key': 'service.name'}, 'normalized output key not found'),
                ({'resourceLogs': []}, {'record_id': RECORD}, 'record ID not found')]:
            with self.subTest(selection=selection), self.assertRaisesRegex(PreviewError, message):
                compare(data, self.before, self.after, rule_ref_decisions_only=True, **selection)

    def test_full_capture_validation_precedes_filter_and_selection(self):
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
                            rule_ref_decisions_only=True)
                self.assertEqual(str(filtered.exception), str(full.exception))

    def test_cli_success_and_atomic_option_selection_errors(self):
        command = [sys.executable, '-m', 'otelwrench', 'loki', 'compare', str(FIXTURE / 'logs.json'),
                   '--before', str(FIXTURE / 'profile.yaml'), '--after', str(FIXTURE / 'after.yaml'),
                   '--rule-ref-decisions-only']
        result = subprocess.run(command + ['--record-id', RECORD], text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        trace = json.loads(result.stdout)['selectedRecordTrace']
        self.assertEqual(trace['decisionFilter'], 'rule_ref_changed')
        self.assertEqual(trace['decisions'], read_json(FIXTURE / 'expected-decisions.json'))
        self.assertEqual(trace['decisionCounts'], dict(selectedLeafCount=8, winnerChangedCount=1,
                                                     placementChangedCount=0, reasonChangedCount=1))
        cases = [([], '--rule-ref-decisions-only requires --record-id'),
                 (['--include-records'], '--rule-ref-decisions-only requires --record-id'),
                 (['--record-id', 'missing'], 'record ID not found'),
                 (['--record-id', RECORD, '--key', 'missing'], 'normalized output key not found'),
                 (['--record-id', RECORD, '--key', 'service.name'], 'normalized output key not found')]
        others = ('--changed-decisions-only', '--placement-decisions-only', '--reason-decisions-only', '--selector-decisions-only')
        for size in range(1, len(others) + 1):
            for flags in itertools.combinations(others, size):
                for selection in ([], ['--record-id', RECORD]):
                    cases.append(([*flags, *selection], 'not allowed with argument'))
        for extra, message in cases:
            with self.subTest(extra=extra):
                result = subprocess.run(command + extra, text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn(message, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
