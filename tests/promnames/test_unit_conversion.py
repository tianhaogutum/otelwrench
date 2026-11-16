# SPDX-License-Identifier: Apache-2.0
import copy
from decimal import Decimal, localcontext
import unittest

from prometheus_client.parser import text_string_to_metric_families
from otelwrench.promnames import ConversionError, convert, load_json
from otelwrench.promnames.core import WITH, WITHOUT
from .helpers import ROOT, attr, document, gauge, sum_metric


class UnitConversionTests(unittest.TestCase):
    def test_integer_decimal_fixture_matches_independent_golden(self):
        source = load_json((ROOT / "examples/prom-names/milliseconds.json").read_text())
        before = copy.deepcopy(source)
        text, report = convert(source, convert_unit="ms:s")
        self.assertEqual(text, (ROOT / "examples/prom-names/seconds.prom").read_text())
        self.assertEqual(source, before)
        self.assertEqual(convert(source, convert_unit="ms:s"), (text, report))
        self.assertEqual(report["unit_conversion"], "ms:s")
        self.assertEqual(report["collisions"], [])
        self.assertEqual(report["sample_count"], 6)
        entry = report["metrics"][0]
        self.assertEqual((entry["original_unit"], entry["target_unit"]), ("ms", "s"))
        self.assertEqual([(p["original_value"], p["value"]) for p in entry["data_points"]],
                         [("1250", "1.250"), ("0.100000000000000000001", "0.000100000000000000000001")])
        self.assertTrue(any(x["reason"].startswith("unit conversion") for x in report["losses"]))
        self.assertTrue(any("binary64 rounding" in x["reason"] for x in report["losses"]))
        parsed = {f.name: f for f in text_string_to_metric_families(text)}
        self.assertEqual(parsed["elapsed_seconds"].type, "counter")
        self.assertEqual(parsed["latency_seconds"].samples[1].value, 1.25)
        self.assertEqual(parsed["payload_bytes"].samples[0].value, 4096)

    def test_exact_scaling_ignores_decimal_context_and_keeps_attributes(self):
        metric = gauge("latency", "ms", attrs=[attr("wait", Decimal("12.345"), "doubleValue")])
        metric["gauge"]["dataPoints"][0] = {
            "asDouble": Decimal("12345678901234567890123456789.123456789"),
            "attributes": metric["gauge"]["dataPoints"][0]["attributes"]}
        with localcontext() as context:
            context.prec = 3
            text, report = convert(document(metric), convert_unit="ms:s")
        self.assertIn('latency_seconds{wait="12.345"} 12345678901234567890123456.789123456789\n', text)
        self.assertEqual(report["metrics"][0]["data_points"][0]["labels"][0]["value"], "12.345")

    def test_suffix_replacement_for_gauges_and_sums(self):
        for strategy in (WITH, WITHOUT):
            for metric, expected in [
                (gauge("wait.milliseconds", "ms"), "wait_seconds"),
                (gauge("milliseconds", "ms"), "seconds"),
                (sum_metric("wait_milliseconds_total", "ms"), "wait_seconds_total"),
                (sum_metric("wait.milliseconds", "ms", monotonic=False), "wait_seconds"),
                (sum_metric("wait_total", "ms"), "wait_seconds_total" if strategy == WITH else "wait_total"),
                (gauge("wait", "ms"), "wait_seconds" if strategy == WITH else "wait"),
            ]:
                with self.subTest(strategy=strategy, metric=metric):
                    text, report = convert(document(metric), strategy=strategy, convert_unit="ms:s")
                    self.assertEqual(report["metrics"][0]["target_family"], expected)
                    self.assertIn(" 0.042\n" if "gauge" in metric else " 0.007\n", text)
                    self.assertEqual(report["metrics"][0]["target_unit"], "s")

    def test_default_and_unrelated_units_remain_unscaled(self):
        for strategy in (WITH, WITHOUT):
            text, report = convert(document(gauge("latency", "ms", "1250")), strategy=strategy)
            self.assertIn(" 1250\n", text)
            self.assertEqual(report["metrics"][0]["target_unit"], "ms")
            self.assertIsNone(report["unit_conversion"])
            for unit in ("", "s", "By", "1"):
                with self.subTest(strategy=strategy, unit=unit):
                    source = document(gauge("other", unit, "1250"))
                    baseline, _ = convert(source, strategy=strategy)
                    actual, mapping = convert(source, strategy=strategy, convert_unit="ms:s")
                    self.assertEqual(actual, baseline)
                    self.assertEqual(mapping["metrics"][0]["target_unit"], unit)
                    self.assertFalse(any(x["reason"].startswith("unit conversion") for x in mapping["losses"]))

    def test_invalid_pairs_and_unsupported_units_fail(self):
        for option in ("s:ms", "By:s", "us:s", "ms:By", "", True):
            with self.subTest(option=option), self.assertRaisesRegex(ConversionError, "unsupported unit conversion"):
                convert({}, convert_unit=option)
        for unit in ("us", "%", "seconds", "m/s"):
            with self.subTest(unit=unit), self.assertRaisesRegex(ConversionError, "unsupported unit"):
                convert(document(gauge(unit=unit)), convert_unit="ms:s")

    def test_invalid_numeric_ranges_fail_instead_of_saturating_or_zeroing(self):
        for number, diagnostic in [(Decimal("1e309"), "overflows"),
                                   (Decimal("5e-324"), "underflows"),
                                   (Decimal("-5e-324"), "underflows")]:
            with self.subTest(number=number), self.assertRaisesRegex(ConversionError, diagnostic):
                convert(document({"name": "latency", "unit": "ms", "gauge": {
                    "dataPoints": [{"asDouble": number}]}}), convert_unit="ms:s")
        with self.assertRaisesRegex(ConversionError, "integer outside"):
            convert(document(gauge(unit="ms", value=str(2**63))), convert_unit="ms:s")

    def test_sign_zero_nonfinite_and_small_representable_values(self):
        for value, expected in [(Decimal("-12.5"), "-0.0125"), (Decimal("-0.0"), "-0.0000"),
                                (Decimal("5e-321"), "5E-324"), ("NaN", "NaN"),
                                ("Infinity", "+Inf"), ("-Infinity", "-Inf")]:
            with self.subTest(value=value):
                source = document({"name": "latency", "unit": "ms", "gauge": {"dataPoints": [{"asDouble": value}]}})
                if isinstance(value, str):
                    with self.assertRaisesRegex(ConversionError, "allow-nonfinite"):
                        convert(source, convert_unit="ms:s")
                text, _ = convert(source, convert_unit="ms:s", allow_nonfinite=True)
                self.assertIn(f"latency_seconds {expected}\n", text)

    def test_converted_targets_use_all_existing_collision_checks(self):
        cases = [
            (document(gauge("latency", "ms"), gauge("latency_seconds", "s")), WITH, "metric_name"),
            (document(sum_metric("work_milliseconds_total", "ms"), gauge("work_seconds", "s")), WITH, "parser_family"),
            (document(sum_metric("work_milliseconds", "ms"), gauge("work_seconds_total", "s")), WITHOUT, "final_series"),
            (document(gauge("latency_milliseconds", "ms"), gauge("latency_seconds", "s")), WITHOUT, "metric_name"),
            (document(gauge("latency", "ms"), gauge("latency", "s")), WITH, "conflicting_descriptor"),
            (document(gauge("latency", "ms"), gauge("latency", "ms")), WITH, "final_series"),
        ]
        for source, strategy, kind in cases:
            with self.subTest(strategy=strategy, kind=kind), self.assertRaises(ConversionError) as caught:
                convert(source, strategy=strategy, convert_unit="ms:s")
            self.assertEqual(caught.exception.report["status"], "collision")
            self.assertIn(kind, {c["kind"] for c in caught.exception.report["collisions"]})
