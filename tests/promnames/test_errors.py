# SPDX-License-Identifier: Apache-2.0
import copy
import unittest
from otelwrench.promnames import ConversionError, convert, load_json
from otelwrench.promnames.core import WITHOUT
from .helpers import attr, document, gauge, sum_metric


class CollisionTests(unittest.TestCase):
    def assert_collision(self, d, kind, **options):
        with self.assertRaises(ConversionError) as caught:
            convert(d, **options)
        report = caught.exception.report
        self.assertEqual(report["status"], "collision")
        self.assertIn(kind, {c["kind"] for c in report["collisions"]})
        for collision in report["collisions"]:
            self.assertTrue(collision["first"].startswith("$.resourceMetrics"))
            self.assertTrue(collision["second"].startswith("$.resourceMetrics"))
        return report

    def test_metric_name_collisions_both_strategies(self):
        for strategy in ["UnderscoreEscapingWithSuffixes", WITHOUT]:
            with self.subTest(strategy=strategy):
                self.assert_collision(document(gauge("a.b"), gauge("a_b")), "metric_name", strategy=strategy)

    def test_unit_suffix_collision(self):
        self.assert_collision(document(gauge("latency", "s"), gauge("latency_seconds")), "metric_name")

    def test_counter_suffix_collision(self):
        self.assert_collision(document(sum_metric("requests"), sum_metric("requests_total")), "metric_name")

    def test_parser_family_and_conflicting_type(self):
        self.assert_collision(document(sum_metric("requests"), gauge("requests")), "conflicting_descriptor")
        self.assert_collision(document(sum_metric("requests"), gauge("requests_total", "")), "metric_name")
        self.assert_collision(document(sum_metric("requests_total"), gauge("requests")), "parser_family")

    def test_no_suffix_canonical_sample_collision(self):
        self.assert_collision(document(sum_metric("requests"), gauge("requests_total")), "final_series", strategy=WITHOUT)

    def test_labels_collide_even_across_distinct_points(self):
        self.assert_collision(document(gauge(attrs=[attr("host.name", "x")]), gauge(attrs=[attr("host_name", "y")])), "label_name")

    def test_same_point_label_collision(self):
        self.assert_collision(document(gauge(attrs=[attr("host.name", "x"), attr("host_name", "y")])), "label_name")

    def test_resource_and_point_collision(self):
        for point_key in ["service.name", "service_name"]:
            with self.subTest(point_key=point_key):
                self.assert_collision(document(gauge(attrs=[attr(point_key, "point")]),
                                      resource={"attributes": [attr("service.name", "resource")]}),
                                      "label_name", promote_resource=["service.name"])

    def test_duplicate_series_across_resources(self):
        d = document(gauge())
        d["resourceMetrics"].append(copy.deepcopy(d["resourceMetrics"][0]))
        self.assert_collision(d, "final_series")
        d["resourceMetrics"][0]["resource"] = {"attributes": [attr("service.name", "a")]}
        d["resourceMetrics"][1]["resource"] = {"attributes": [attr("service.name", "b")]}
        self.assert_collision(d, "final_series")
        self.assertEqual(convert(d, promote_resource=["service.name"])[1]["sample_count"], 2)

    def test_empty_labels_equivalent_to_absence(self):
        self.assert_collision(document(gauge(), gauge(attrs=[attr("x", "")])), "final_series")

    def test_stringified_values_collide(self):
        self.assert_collision(document(gauge(attrs=[attr("x", "7")]), gauge(attrs=[attr("x", "7", "intValue")])), "final_series")

    def test_conflicting_descriptors(self):
        cases = [gauge("x", "s"), sum_metric("x"), gauge("x", description="different")]
        for metric in cases:
            with self.subTest(metric=metric):
                self.assert_collision(document(gauge("x"), metric), "conflicting_descriptor")


class InvalidInputTests(unittest.TestCase):
    def test_reject_unsupported_metric_types(self):
        for kind in ["histogram", "exponentialHistogram", "summary"]:
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ConversionError, "unsupported metric type"):
                    convert(document({"name": "x", kind: {}}))

    def test_temporality_numeric_cumulative_only(self):
        for value in [0, 1, "2", "AGGREGATION_TEMPORALITY_CUMULATIVE", True, None]:
            with self.subTest(value=value):
                metric = sum_metric()
                metric["sum"]["aggregationTemporality"] = value
                with self.assertRaisesRegex(ConversionError, "cumulative"):
                    convert(document(metric))

    def test_reject_units_even_without_suffixes(self):
        for unit in ["%", "{requests}", "seconds", "m/s", "μs"]:
            for strategy in ["UnderscoreEscapingWithSuffixes", WITHOUT]:
                with self.subTest(unit=unit, strategy=strategy):
                    with self.assertRaisesRegex(ConversionError, "unsupported unit"):
                        convert(document(gauge(unit=unit)), strategy=strategy)

    def test_int64_validation(self):
        for value in [str(2**63), str(-(2**63) - 1), "1.0", "01", True, 1.5, None]:
            with self.subTest(value=value):
                with self.assertRaises(ConversionError):
                    convert(document(gauge(value=value)))

    def test_double_validation(self):
        for value in ["3.5", "+Inf", "nan", True, None, {}, [], 10**310]:
            with self.subTest(value=value):
                with self.assertRaises(ConversionError):
                    convert(document({"name": "x", "gauge": {"dataPoints": [{"asDouble": value}]}}))
        for raw in ["1e-9999", "1e9999"]:
            with self.subTest(raw=raw):
                d = load_json('{"resourceMetrics":[{"scopeMetrics":[{"metrics":[{"name":"x","gauge":{"dataPoints":[{"asDouble":'+raw+'}]}}]}]}]}')
                with self.assertRaisesRegex(ConversionError, "IEEE-754"):
                    convert(d)

    def test_invalid_anyvalue(self):
        cases = [{}, {"arrayValue": {"values": []}}, {"kvlistValue": {"values": []}},
                 {"bytesValue": "YQ=="}, {"stringValue": "x", "intValue": "1"},
                 {"boolValue": "false"}, {"stringValue": 1}, {"intValue": str(2**63)},
                 {"doubleValue": "1.5"}, None]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ConversionError):
                    convert(document(gauge(attrs=[{"key": "x", "value": value}])))

    def test_unpromoted_attributes_still_validated(self):
        with self.assertRaises(ConversionError):
            convert(document(gauge(), resource={"attributes": [{"key": "x", "value": {"arrayValue": {}}}]}))

    def test_duplicate_and_reserved_label_keys(self):
        for attrs in [[attr("x", "a"), attr("x", "b")], [attr("__name__", "x")], [attr("..x", "y")], [attr("", "x")]]:
            with self.subTest(attrs=attrs):
                with self.assertRaises(ConversionError):
                    convert(document(gauge(attrs=attrs)))

    def test_unknown_fields_and_shapes(self):
        for d in [[], None, {"resource_metrics": []}, {"resourceMetrics": {}},
                  {"resourceMetrics": [None]}, {"resourceMetrics": [{"scopeMetrics": [42]}]},
                  document({"name": "x", "gauge": {}, "surprise": 1}),
                  document({"name": "x", "gauge": {}, "sum": {}}),
                  document({"name": "x", "gauge": {}, "metadata": []}),
                  document({"name": "x"}), document({"name": "x", "gauge": None}),
                  document(gauge("!!!")), document(gauge("")), document(gauge("x\ud800"))]:
            with self.subTest(d=repr(d)):
                with self.assertRaises(ConversionError):
                    convert(d)

    def test_number_point_fields_flags_and_exemplars(self):
        for point in [{}, {"asInt": "1", "asDouble": 1}, {"asInt": "1", "flags": 1},
                      {"asInt": "1", "flags": "0"}, {"asInt": "1", "flags": False},
                      {"asInt": "1", "exemplars": [{}]}, {"asInt": "1", "exemplars": None},
                      {"asInt": "1", "timeUnixNano": "-1"}, {"asInt": "1", "timeUnixNano": str(2**64)},
                      {"asInt": "1", "typo": 2}]:
            with self.subTest(point=point):
                with self.assertRaises(ConversionError):
                    convert(document({"name": "x", "gauge": {"dataPoints": [point]}}))

    def test_monotonic_validation(self):
        with self.assertRaisesRegex(ConversionError, "negative"):
            convert(document(sum_metric(value="-1")))
        with self.assertRaisesRegex(ConversionError, "boolean"):
            convert(document(sum_metric(monotonic="true")))

    def test_json_loader_rejects_duplicates_constants_and_malformed(self):
        for text in ['{"resourceMetrics":[],"resourceMetrics":[]}',
                     '{"resourceMetrics":NaN}', '{"resourceMetrics":Infinity}', 'null', '{bad']:
            with self.subTest(text=text):
                with self.assertRaises(ConversionError):
                    load_json(text)

    def test_strategy_error(self):
        with self.assertRaisesRegex(ConversionError, "strategy"):
            convert({}, strategy="NoTranslation")
