# SPDX-License-Identifier: Apache-2.0
import copy
import math
import unittest
from decimal import Decimal
from prometheus_client.parser import text_string_to_metric_families

from otelwrench.promnames import ConversionError, convert, load_json
from otelwrench.promnames.core import WITHOUT
from .helpers import attr, document, gauge, sum_metric


class NamingTests(unittest.TestCase):
    def test_supported_units_types_and_suffixes(self):
        cases = [
            (gauge("time", "s"), "time_seconds", "gauge"),
            (gauge("time", "ms"), "time_milliseconds", "gauge"),
            (gauge("size", "By"), "size_bytes", "gauge"),
            (gauge("load", "1"), "load_ratio", "gauge"),
            (gauge("load"), "load", "gauge"),
            (sum_metric(), "requests_total", "counter"),
            (sum_metric("requests_total"), "requests_total", "counter"),
            (sum_metric("elapsed_total", "s"), "elapsed_seconds_total", "counter"),
            (sum_metric("elapsed_seconds_total", "s"), "elapsed_seconds_total", "counter"),
            (sum_metric("size", "By"), "size_bytes_total", "counter"),
            (sum_metric("active", "1", False), "active_ratio", "gauge"),
            (sum_metric("active", "", False, "-3"), "active", "gauge"),
            (gauge("time_seconds", "s"), "time_seconds", "gauge"),
            (gauge("seconds", "s"), "seconds", "gauge"),
            (gauge("(lambda).function.executions(#)"), "lambda_function_executions", "gauge"),
            (gauge("9.cpu:load__used"), "_9_cpu_load_used", "gauge"),
            (gauge("métric"), "m_tric", "gauge"),
        ]
        for metric, name, typ in cases:
            with self.subTest(name=name, metric=metric):
                text, mapping = convert(document(metric))
                self.assertIn(f"# TYPE {name} {typ}\n{name} ", text)
                entry = mapping["metrics"][0]
                self.assertEqual(entry["target_family"], name)
                parsed = list(text_string_to_metric_families(text))
                self.assertEqual(parsed[0].type, typ)
                self.assertEqual(parsed[0].samples[0].value, float(entry["data_points"][0]["value"]))

    def test_without_suffixes_retains_existing_suffix(self):
        for name, unit, target in [("requests", "1", "requests"),
                                    ("time.total", "ms", "time_total"),
                                    ("requests_total", "s", "requests_total")]:
            with self.subTest(name=name):
                text, mapping = convert(document(sum_metric(name, unit)), strategy=WITHOUT)
                self.assertIn(f"# TYPE {target} counter\n{target} 7\n", text)
                self.assertEqual(mapping["metrics"][0]["target_family"], target)
                self.assertEqual(list(text_string_to_metric_families(text))[0].samples[0].name,
                                 target if target.endswith("_total") else target + "_total")
                self.assertTrue(any("unit retained" in x["reason"] for x in mapping["losses"]))

    def test_label_names_values_and_escaping(self):
        attrs = [attr("host.name", "a\\b\n\"c"), attr("2 cents", "x"),
                 attr("a__b", True, "boolValue"), attr("_id", "123", "intValue"),
                 attr("métric", Decimal("1.25"), "doubleValue"), attr("name (of the host)", "h")]
        text, mapping = convert(document(gauge(attrs=attrs)))
        sample = list(text_string_to_metric_families(text))[0].samples[0]
        self.assertEqual(sample.labels, {"host_name": "a\\b\n\"c", "key_2_cents": "x", "a__b": "true",
                                         "_id": "123", "m_tric": "1.25", "name__of_the_host_": "h"})
        labels = mapping["metrics"][0]["data_points"][0]["labels"]
        self.assertTrue(any(x["operation"] == "scalar_to_label_string" for a in labels for x in a["normalizations"]))
        self.assertIn('host_name="a\\\\b\\n\\"c"', text)

    def test_resource_promotion_is_explicit(self):
        d = document(gauge(), resource={"attributes": [attr("service.name", "shop"), attr("secret", "synthetic-only")]})
        plain, excluded = convert(d)
        text, promoted = convert(d, promote_resource=["service.name"])
        self.assertNotIn("shop", plain)
        self.assertIn('{service_name="shop"}', text)
        self.assertEqual(sum("promotion allowlist" in x["reason"] for x in excluded["losses"]), 2)
        self.assertEqual(sum("promotion allowlist" in x["reason"] for x in promoted["losses"]), 1)

    def test_int64_precision_in_text_and_report(self):
        for number in [-(2**63), 2**53 + 1, 2**63 - 1]:
            with self.subTest(number=number):
                text, report = convert(document(gauge(value=str(number))))
                self.assertIn(f"temperature {number}\n", text)
                self.assertEqual(report["metrics"][0]["data_points"][0]["value"], str(number))
                self.assertIn("binary64", report["numeric_policy"])
                self.assertEqual(list(text_string_to_metric_families(text))[0].samples[0].value, number)

    def test_json_decimal_text_not_rounded_by_loader(self):
        d = load_json('{"resourceMetrics":[{"scopeMetrics":[{"metrics":[{"name":"x","gauge":{"dataPoints":[{"asDouble":0.123456789012345678901} ]}}]}]}]}')
        text, _ = convert(d)
        self.assertIn("x 0.123456789012345678901\n", text)

    def test_nonfinite_explicit_opt_in(self):
        for raw, target in [("NaN", "NaN"), ("Infinity", "+Inf"), ("-Infinity", "-Inf")]:
            with self.subTest(raw=raw):
                metric = {"name": "x", "gauge": {"dataPoints": [{"asDouble": raw}]}}
                with self.assertRaisesRegex(ConversionError, "allow-nonfinite"):
                    convert(document(metric))
                text, _ = convert(document(metric), allow_nonfinite=True)
                self.assertIn(f"x {target}\n", text)
                value = list(text_string_to_metric_families(text))[0].samples[0].value
                self.assertTrue(math.isnan(value) if raw == "NaN" else math.isinf(value))

    def test_deterministic_prom_independent_of_input_order(self):
        first = gauge("a", attrs=[attr("b", "2"), attr("a", "1")])
        second = gauge("a", attrs=[attr("a", "3")])
        d = document(gauge("z"), first, second)
        original = copy.deepcopy(d)
        expected, report = convert(d)
        self.assertEqual(convert(d), (expected, report))
        self.assertEqual(d, original)
        d["resourceMetrics"][0]["scopeMetrics"][0]["metrics"].reverse()
        first["gauge"]["dataPoints"][0]["attributes"].reverse()
        self.assertEqual(convert(d)[0], expected)

    def test_metadata_and_snapshot_losses(self):
        metric = gauge(description="Text\\with\nnewline")
        metric["gauge"]["dataPoints"][0].update(timeUnixNano="1794420000000000000", startTimeUnixNano="1", flags=0, exemplars=[])
        d = document(metric)
        rm = d["resourceMetrics"][0]
        rm["schemaUrl"] = "https://synthetic.invalid/schema"
        rm["resource"]["droppedAttributesCount"] = 2
        rm["scopeMetrics"][0]["scope"] = {"name": "test", "version": "1", "attributes": [attr("scope.key", "v")]}
        text, report = convert(d)
        self.assertEqual(len(report["losses"]), 7)
        self.assertEqual(list(text_string_to_metric_families(text))[0].documentation, "Text\\with\nnewline")
        self.assertEqual(report["metrics"][0]["original_description"], "Text\\with\nnewline")

    def test_empty_document_and_empty_descriptor(self):
        self.assertEqual(convert({})[0], "")
        text, report = convert(document({"name": "x", "gauge": {}}))
        self.assertEqual(text, "# TYPE x gauge\n")
        self.assertEqual(report["sample_count"], 0)
        self.assertTrue(report["losses"])
