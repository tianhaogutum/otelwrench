"""Selected pre-filter decision totals attributed to stable source levels."""

import copy
import unittest

from .test_compare_decision_field_counts import field_count_cases
from otelwrench.loki.mapping import compare, explain

SOURCES = ('resource', 'scope', 'log', 'field')
COUNTERS = ('selectedLeafCount', 'winnerChangedCount', 'placementChangedCount', 'reasonChangedCount')
VIEWS = (('all', {}), ('winner_changed', {'changed_decisions_only': True}),
         ('placement_changed', {'placement_decisions_only': True}),
         ('reason_changed', {'reason_decisions_only': True}),
         ('selector_changed', {'selector_decisions_only': True}),
         ('rule_ref_changed', {'rule_ref_decisions_only': True}),
         ('unchanged', {'unchanged_decisions_only': True}))
KEY_SOURCES = {'service_name': 'resource', 'k8s_pod_name': 'resource',
               'internal_component': 'scope', 'net_peer_port': 'log', 'net_peer_host': 'log',
               'debug_private_value': 'log', 'empty': 'log', 'scope_name': 'field',
               'route_old': 'resource', 'route_new': 'resource'}
# Hand-authored all-key expectations in resource/scope/log/field order.
BASE = ((2, 0, 0, 0), (1, 0, 0, 0), (4, 0, 0, 0), (1, 0, 0, 0))
REASON = ((2, 1, 0, 1), *BASE[1:])
MIXED = (REASON[0], BASE[1], (4, 2, 2, 0), BASE[3])
ALL = {'literal-to-RE2': REASON, 'selector-only': REASON, 'pointer-only': REASON,
       'action-only': ((2, 1, 1, 0), *BASE[1:]), 'mixed-same-regex': MIXED,
       'mixed-selector-replacement': MIXED, 'identity': BASE, 'shadowed-only': BASE,
       'defaults-fallback-nested': ((2, 2, 2, 0), BASE[1], (4, 2, 2, 0), BASE[3]),
       'default-to-explicit': REASON, 'zero-leaf': ((0, 0, 0, 0),) * 4,
       'topology': ((2, 2, 2, 0), *((0, 0, 0, 0),) * 3)}
CHANGED_KEYS = {'literal-to-RE2': {'service_name': 'reason'},
                'selector-only': {'service_name': 'reason'}, 'pointer-only': {'service_name': 'reason'},
                'action-only': {'service_name': 'placement'},
                'mixed-same-regex': {'service_name': 'reason', 'net_peer_port': 'placement', 'net_peer_host': 'placement'},
                'mixed-selector-replacement': {'service_name': 'reason', 'net_peer_port': 'placement', 'net_peer_host': 'placement'},
                'defaults-fallback-nested': dict.fromkeys(('service_name', 'k8s_pod_name', 'net_peer_port', 'net_peer_host'), 'placement'),
                'default-to-explicit': {'service_name': 'reason'},
                'topology': dict.fromkeys(('route_old', 'route_new'), 'placement')}


def expected_buckets(name, key):
    if key is None:
        return {source: dict(zip(COUNTERS, counts)) for source, counts in zip(SOURCES, ALL[name], strict=True)}
    result = {source: dict.fromkeys(COUNTERS, 0) for source in SOURCES}
    kind = CHANGED_KEYS.get(name, {}).get(key)
    result[KEY_SOURCES[key]] = dict(zip(COUNTERS, (1, int(kind is not None),
                                                 int(kind == 'placement'), int(kind == 'reason'))))
    return result


class DecisionSourceCountTests(unittest.TestCase):
    def test_exact_buckets_in_all_views_record_key_and_audit_selections(self):
        subcases = 0
        for name, data, before, after, record_id, *_ in field_count_cases():
            keys = ([] if name == 'zero-leaf' else ['route_old', 'route_new'] if name == 'topology'
                    else list(KEY_SOURCES)[:8])
            for key in [None, *keys]:
                for detailed in (False, True):
                    args = dict(record_id=record_id, key=key, include_records=detailed)
                    full = compare(data, before, after, **args)
                    for mode, options in VIEWS:
                        with self.subTest(case=name, key=key, audit=detailed, view=mode):
                            subcases += 1
                            report = compare(data, before, after, **args, **options)
                            trace = report['selectedRecordTrace']
                            buckets = trace['decisionCountsBySource']
                            self.assertEqual(list(buckets), list(SOURCES))
                            self.assertEqual(buckets, expected_buckets(name, key))
                            for counts in buckets.values():
                                self.assertEqual(list(counts), list(COUNTERS))
                                self.assertTrue(all(type(n) is int and n >= 0 for n in counts.values()))
                            self.assertEqual({counter: sum(b[counter] for b in buckets.values())
                                              for counter in COUNTERS}, trace['decisionCounts'])
                            self.assertEqual(trace['decisionFilter'], mode)
                            # Every view changes only the decisions and view label, including empty views.
                            restored = copy.deepcopy(report)
                            for field in ('decisions', 'decisionFilter'):
                                restored['selectedRecordTrace'][field] = full['selectedRecordTrace'][field]
                            self.assertEqual(restored, full)
                    for side, policy in (('before', before), ('after', after)):
                        self.assertEqual(full['selectedRecordTrace'][side],
                                         explain(data, policy, record_id=record_id, key=key))
                    full.pop('selectedRecordTrace')
                    self.assertNotIn('decisionCountsBySource', full)
                    self.assertEqual(full, compare(data, before, after, include_records=detailed))
        self.assertEqual(subcases, 1316)

    def test_no_record_selection_has_no_trace_or_source_counts(self):
        for name, data, before, after, *_ in field_count_cases():
            for detailed in (False, True):
                with self.subTest(case=name, audit=detailed):
                    report = compare(data, before, after, include_records=detailed)
                    self.assertNotIn('selectedRecordTrace', report)
                    self.assertNotIn('decisionCountsBySource', report)
