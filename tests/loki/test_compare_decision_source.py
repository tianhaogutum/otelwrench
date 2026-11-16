"""Source selection precedes local counting and views, never raw trace validation."""

import copy
import itertools
import unittest

from .test_compare_decision_field_counts import field_count_cases
from .test_compare_decision_source_counts import SOURCES, COUNTERS, VIEWS, KEY_SOURCES, expected_buckets
from otelwrench.loki.mapping import compare, explain
from otelwrench.loki.strict import PreviewError

# Independent literal-change expectations for each changed key; all others are zero.
FIELD_CHANGES = {
    'literal-to-RE2': {'service_name': (1, 0, 1)},
    'selector-only': {'service_name': (0, 0, 1)},
    'pointer-only': {'service_name': (1, 0, 0)},
    'action-only': {'service_name': (0, 1, 0)},
    'mixed-same-regex': {'service_name': (0, 0, 1), 'net_peer_port': (0, 1, 0), 'net_peer_host': (0, 1, 0)},
    'mixed-selector-replacement': {'service_name': (0, 0, 1), 'net_peer_port': (0, 1, 1), 'net_peer_host': (0, 1, 1)},
    'defaults-fallback-nested': {'service_name': (1, 1, 0), 'k8s_pod_name': (1, 1, 0),
                               'net_peer_port': (1, 1, 1), 'net_peer_host': (1, 1, 1)},
    'default-to-explicit': {'service_name': (1, 0, 1)},
    'topology': {'route_old': (1, 1, 1), 'route_new': (1, 1, 1)},
}
LOCAL = ('decisions', 'decisionCounts', 'decisionFieldCounts', 'decisionCountsBySource')


class DecisionSourceTests(unittest.TestCase):
    def test_all_sources_views_keys_and_audits(self):
        subcases = 0
        for name, data, before, after, record_id, *_ in field_count_cases():
            keys = ([] if name == 'zero-leaf' else ['route_old', 'route_new'] if name == 'topology'
                    else list(KEY_SOURCES)[:8])
            for key, detailed in itertools.product([None, *keys], (False, True)):
                args = dict(record_id=record_id, key=key, include_records=detailed)
                all_sources = compare(data, before, after, **args)
                self.assertNotIn('decisionSource', all_sources['selectedRecordTrace'])
                self.assertEqual(compare(data, before, after, **args, decision_source=None), all_sources)
                for side, policy in (('before', before), ('after', after)):
                    self.assertEqual(all_sources['selectedRecordTrace'][side],
                                     explain(data, policy, record_id=record_id, key=key))
                for mode, options in VIEWS:
                    full = compare(data, before, after, **args, **options)
                    for source in SOURCES:
                        with self.subTest(case=name, key=key, audit=detailed, view=mode, source=source):
                            subcases += 1
                            report = compare(data, before, after, **args, **options, decision_source=source)
                            trace = report['selectedRecordTrace']
                            self.assertEqual(trace['decisionSource'], source)
                            self.assertEqual(trace['decisionFilter'], mode)
                            self.assertEqual(trace['decisions'], [d for d in full['selectedRecordTrace']['decisions']
                                                                 if d['sourceRef']['source'] == source])
                            buckets = expected_buckets(name, key)
                            expected = {s: buckets[s] if s == source else dict.fromkeys(COUNTERS, 0) for s in SOURCES}
                            self.assertEqual(trace['decisionCountsBySource'], expected)
                            self.assertEqual(trace['decisionCounts'], expected[source])
                            fields = [v for k, v in FIELD_CHANGES.get(name, {}).items()
                                      if KEY_SOURCES[k] == source and (key is None or k == key)]
                            self.assertEqual(trace['decisionFieldCounts'], dict(zip(('ruleRef', 'placement', 'selector'),
                                             (sum(v[i] for v in fields) for i in range(3)))))
                            # Complete retained/dropped values, raw explanations, globals, audits and indexes are identical.
                            trace.pop('decisionSource')
                            for field in LOCAL:
                                trace[field] = full['selectedRecordTrace'][field]
                            self.assertEqual(report, full)
        self.assertEqual(subcases, 5264)

    def test_selection_errors_and_full_capture_validation_precede_source_filtering(self):
        _, data, before, after, record_id, *_ = field_count_cases()[0]
        for source, (_, options) in itertools.product(SOURCES, VIEWS):
            for selection, message in [({}, 'requires --record-id'),
                                       ({'record_id': 'missing', 'key': 'missing'}, 'record ID not found'),
                                       ({'record_id': record_id, 'key': 'missing'}, 'normalized output key not found'),
                                       ({'record_id': record_id, 'key': 'service.name'}, 'normalized output key not found')]:
                with self.subTest(source=source, options=options, selection=selection):
                    with self.assertRaisesRegex(PreviewError, message):
                        compare(data, before, after, **selection, **options, decision_source=source)
            invalid = copy.deepcopy(data)
            invalid['resourceLogs'][0]['scopeLogs'][0]['logRecords'].append({'timeUnixNano': '0'})
            with self.assertRaisesRegex(PreviewError, 'comparison validation failed.*missing timestamp'):
                compare(invalid, before, after, record_id='missing', key='missing', **options, decision_source=source)
        for invalid in ('', 'RESOURCE', 'all', 'bogus', 0, False, [], {}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(PreviewError, 'decision-source must be one of'):
                compare(data, before, after, record_id=record_id, decision_source=invalid)
        for (_, first), (_, second) in itertools.combinations(VIEWS[1:], 2):
            with self.assertRaisesRegex(PreviewError, 'cannot combine'):
                compare(data, before, after, record_id=record_id, decision_source='field', **first, **second)
