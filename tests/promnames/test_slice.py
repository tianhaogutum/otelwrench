# SPDX-License-Identifier: Apache-2.0
import unittest
from pathlib import Path

from prometheus_client.parser import text_string_to_metric_families
from otelwrench.promnames import ConversionError, convert, load_json


class SliceTests(unittest.TestCase):
    def test_real_fixture_parser(self):
        text, report = convert(load_json(Path("examples/prom-names/metrics.json").read_text()))
        families = {f.name: f for f in text_string_to_metric_families(text)}
        self.assertEqual(report["sample_count"], 5)
        self.assertEqual(families["requests"].type, "counter")
        self.assertEqual(families["requests"].samples[0].value, 7)
        self.assertEqual(families["requests"].samples[0].labels, {"route": "/cart"})
        self.assertEqual(families["request_duration_milliseconds"].samples[0].value, 12.5)
        self.assertIn('requests_total{route="/cart"} 7\n', text)

    def test_reject_delta(self):
        document = load_json(Path("examples/prom-names/metrics.json").read_text())
        document["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["sum"]["aggregationTemporality"] = 1
        with self.assertRaisesRegex(ConversionError, "only cumulative"):
            convert(document)
