"""Reason-only filtering retains selection and atomic validation boundaries."""

import copy
import itertools
import json
import subprocess
import sys
import unittest

from .helpers import ROOT, attr, logs
from otelwrench.loki.config import load_profile
from otelwrench.loki.mapping import compare
from otelwrench.loki.strict import PreviewError, read_json

FIXTURE = ROOT / 'examples/loki/explain'
RECORD = 'resourceLogs[0].scopeLogs[0].logRecords[0]'
FILTERS = ('changed_decisions_only', 'placement_decisions_only', 'reason_decisions_only')


class ReasonDecisionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.data = read_json(FIXTURE / 'logs.json')
        self.before = load_profile(FIXTURE / 'profile.yaml')
        self.after = load_profile(FIXTURE / 'after.yaml')

    def test_api_dependencies_conflicts_and_unknown_selection(self):
        for detailed in (False, True):
            with self.subTest(detailed=detailed), self.assertRaisesRegex(
                    PreviewError, '--reason-decisions-only requires --record-id'):
                compare(self.data, self.before, self.after, include_records=detailed, reason_decisions_only=True)
        for size in (2, 3):
            for flags in itertools.combinations(FILTERS, size):
                for record in (None, RECORD):
                    with self.subTest(flags=flags, record=record), self.assertRaisesRegex(PreviewError, 'cannot combine'):
                        compare(self.data, self.before, self.after, record_id=record, **dict.fromkeys(flags, True))
        for selection, message in [({'record_id': 'missing'}, 'record ID not found'),
                                   ({'record_id': RECORD, 'key': 'missing'}, 'normalized output key not found'),
                                   ({'record_id': RECORD, 'key': 'service.name'}, 'normalized output key not found')]:
            with self.subTest(selection=selection), self.assertRaisesRegex(PreviewError, message):
                compare(self.data, self.before, self.after, reason_decisions_only=True, **selection)
        with self.assertRaisesRegex(PreviewError, 'record ID not found'):
            compare({'resourceLogs': []}, self.before, self.after, record_id=RECORD, reason_decisions_only=True)

    def test_full_capture_validation_precedes_reason_filter_and_selection(self):
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
                    compare(data, self.before, self.after, record_id='missing', key='missing', reason_decisions_only=True)
                self.assertEqual(str(filtered.exception), str(full.exception))

    def test_cli_success_dependencies_conflicts_and_selection_errors_are_atomic(self):
        command = [sys.executable, '-m', 'otelwrench', 'loki', 'compare', str(FIXTURE / 'logs.json'),
                   '--before', str(FIXTURE / 'profile.yaml'), '--after', str(FIXTURE / 'after.yaml'),
                   '--reason-decisions-only']
        result = subprocess.run(command + ['--record-id', RECORD], text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        trace = json.loads(result.stdout)['selectedRecordTrace']
        self.assertEqual(trace['decisionFilter'], 'reason_changed')
        self.assertEqual(trace['decisions'], read_json(FIXTURE / 'expected-decisions.json'))
        self.assertEqual(trace['decisionCounts'], {'selectedLeafCount': 8, 'winnerChangedCount': 1, 'placementChangedCount': 0, 'reasonChangedCount': 1})
        cases = [([], '--reason-decisions-only requires --record-id'),
                 (['--include-records'], '--reason-decisions-only requires --record-id'),
                 (['--record-id', 'missing'], 'record ID not found'),
                 (['--record-id', RECORD, '--key', 'missing'], 'normalized output key not found'),
                 (['--record-id', RECORD, '--key', 'service.name'], 'normalized output key not found')]
        for flags in (['--changed-decisions-only'], ['--placement-decisions-only'],
                      ['--changed-decisions-only', '--placement-decisions-only']):
            for selection in ([], ['--record-id', RECORD]):
                cases.append((flags + selection, 'not allowed with argument'))
        for extra, message in cases:
            with self.subTest(extra=extra):
                result = subprocess.run(command + extra, text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn(message, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
