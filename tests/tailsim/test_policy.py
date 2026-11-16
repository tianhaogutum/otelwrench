import unittest

from otelwrench.tailsim.replay import replay
from .helpers import attr, corpus, policies, policy, record


class DecisionMatrix(unittest.TestCase):
    def decision(self, spans, *items, attrs=None):
        return replay(corpus(spans, attrs), policies(*items))['traces'][0]

    def test_hand_truthed_latency_matrix(self):
        # Lower bound is STRICT after whole-millisecond truncation; upper inclusive.
        cases = [(499_999_999, False), (500_000_000, False), (500_999_999, False),
                 (501_000_000, True), (600_000_000, True), (600_999_999, True),
                 (601_000_000, False)]
        for duration, expected in cases:
            with self.subTest(duration=duration):
                row = self.decision([record(duration=duration)],
                                    policy('latency', {'threshold_ms': 500, 'upper_threshold_ms': 600}))
                self.assertEqual(row['decision'], 'sample' if expected else 'drop')

    def test_unbounded_latency_zero_upper(self):
        row = self.decision([record(duration=2_000_000_000)],
                            policy('latency', {'threshold_ms': 500, 'upper_threshold_ms': 0}))
        self.assertEqual(row['decision'], 'sample')

    def test_status_any_span_and_unset_default(self):
        spans = [record(sid=1), record(sid=2, status=2)]
        row = self.decision(spans, policy('status_code', {'status_codes': ['ERROR']}))
        self.assertEqual(row['decision'], 'sample')
        self.assertEqual(row['kept_span_count'], 2)
        self.assertTrue(row['error'])
        missing = record()
        del missing['status']
        self.assertEqual(self.decision([missing], policy('status_code', {'status_codes': ['UNSET']}))['decision'], 'sample')

    def test_multispan_complete_duration_across_resources(self):
        spans = [record(sid=1, start=10_000_000_000, duration=10_000_000),
                 record(sid=2, start=10_590_000_000, duration=10_000_000, status=2)]
        row = self.decision(spans, policy('latency', {'threshold_ms': 500}))
        self.assertEqual(row['duration_ns'], 600_000_000)
        self.assertEqual(row['decision'], 'sample')
        self.assertEqual(row['statuses'], ['ERROR', 'OK'])

    def test_drop_veto_independent_of_policy_order(self):
        drop = policy('drop', {'drop_sub_policy': [policy('status_code', {'status_codes': ['ERROR']})]}, 'veto')
        for config in [(policy('always_sample'), drop), (drop, policy('always_sample'))]:
            row = self.decision([record(status=2)], *config)
            self.assertEqual(row['decision'], 'drop')
            self.assertEqual(row['reason'], 'drop_policy_precedence')
            self.assertEqual(row['kept_span_count'], 0)
            self.assertCountEqual(row['matched_policy_names'], ['always_sample', 'veto'])

    def test_drop_requires_all_children(self):
        drop = policy('drop', {'drop_sub_policy': [policy('status_code', {'status_codes': ['ERROR']}),
                                                   policy('latency', {'threshold_ms': 500})]})
        for status, duration, expected in [(2, 600_000_000, 'drop'), (2, 10_000_000, 'sample'),
                                            (1, 600_000_000, 'sample')]:
            with self.subTest(status=status, duration=duration):
                self.assertEqual(self.decision([record(status=status, duration=duration)],
                                              policy('always_sample'), drop)['decision'], expected)

    def test_and_not_matrix(self):
        combined = policy('and', {'and_sub_policy': [policy('status_code', {'status_codes': ['ERROR']}),
                            policy('not', {'not_sub_policy': policy('string_attribute', {'key': 'url.path', 'values': ['/health']})})]})
        for status, path, expected in [(2, '/checkout', 'sample'), (2, '/health', 'drop'),
                                       (1, '/checkout', 'drop'), (1, '/health', 'drop')]:
            with self.subTest(status=status, path=path):
                row = self.decision([record(status=status, attrs=[attr('url.path', path)])], combined)
                self.assertEqual(row['decision'], expected)
                self.assertEqual(len(row['policies'][0]['children']), 2)

    def test_not_is_not_a_global_veto(self):
        negation = policy('not', {'not_sub_policy': policy('always_sample')})
        self.assertEqual(self.decision([record()], negation)['decision'], 'drop')
        self.assertEqual(self.decision([record()], negation, policy('always_sample'))['decision'], 'sample')

    def test_string_exact_resource_and_span_values(self):
        exact = policy('string_attribute', {'key': 'service.name', 'values': ['api']})
        self.assertEqual(self.decision([record()], exact, attrs=[attr('service.name', 'api')])['decision'], 'sample')
        self.assertEqual(self.decision([record(attrs=[attr('service.name', 'api')])], exact)['decision'], 'sample')
        self.assertEqual(self.decision([record(attrs=[attr('service.name', 'API')])], exact)['decision'], 'drop')
        # '*' is a literal character; numeric/bool types never coerce to strings.
        star = policy('string_attribute', {'key': 'x', 'values': ['a*', '12', 'True']})
        for value, kind in [('abc', 'stringValue'), ('12', 'intValue'), (True, 'boolValue')]:
            self.assertEqual(self.decision([record(attrs=[attr('x', value, kind)])], star)['decision'], 'drop')

    def test_no_match_has_explanation(self):
        row = self.decision([record()], policy('status_code', {'status_codes': ['ERROR']}))
        self.assertEqual(row['matched_policy_names'], [])
        self.assertEqual(row['reason'], 'no_sample_policy_match')

    def test_attribute_conflicts_across_spans_both_participate(self):
        row = self.decision([record(sid=1, attrs=[attr('x', 'a')]), record(sid=2, attrs=[attr('x', 'b')])],
                            policy('string_attribute', {'key': 'x', 'values': ['a']}))
        self.assertEqual(row['decision'], 'sample')

    def test_permutation_decisions_identical_including_upper_bound(self):
        spans = [record(trace=2, sid=3, duration=600_000_000),
                 record(trace=1, sid=2, start=2_000_000_000, duration=10_000_000, status=2),
                 record(trace=1, sid=1, duration=10_000_000)]
        config = policies(policy('latency', {'threshold_ms': 500, 'upper_threshold_ms': 800}),
                          policy('status_code', {'status_codes': ['ERROR']}))
        expected = replay(corpus(spans), config)
        actual = replay(corpus(list(reversed(spans))), config)
        self.assertEqual(actual['traces'], expected['traces'])
        self.assertEqual(actual['summary'], expected['summary'])

    def test_complete_duration_ignores_intermediate_upper_bound_match(self):
        spans = [record(sid=1, duration=600_000_000), record(sid=2, duration=900_000_000)]
        config = policy('latency', {'threshold_ms': 500, 'upper_threshold_ms': 700})
        a = self.decision(spans, config)
        b = self.decision(list(reversed(spans)), config)
        self.assertEqual(a, b)
        self.assertEqual(a['decision'], 'drop')
        self.assertEqual(a['duration_ms_floor'], 900)
