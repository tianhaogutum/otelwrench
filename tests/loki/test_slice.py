import json
from pathlib import Path
import unittest

from otelwrench.loki.config import load_profile
from otelwrench.loki.mapping import compare, preview, push_json
from otelwrench.loki.strict import read_json

ROOT = Path(__file__).resolve().parents[2]


class VerticalSliceTests(unittest.TestCase):
    def test_two_pods_to_one_stream(self):
        data = read_json(ROOT / "examples/loki/logs.json")
        report = preview(data, load_profile(ROOT / "examples/loki/loki.yaml"))
        push = push_json(report)
        self.assertEqual(report["summary"], {"recordCount": 2, "streamCount": 1, "droppedAttributeCount": 2})
        self.assertEqual(push["streams"][0]["stream"], {"service_name": "checkout"})
        self.assertEqual(push["streams"][0]["values"][0], [
            "1700000000000000001", "order 1 accepted",
            {"component": "payment", "k8s_pod_name": "p1", "order_id": "42",
             "scope_name": "example.checkout", "scope_version": "1.0"}])
        diff = compare(data, load_profile(ROOT / "examples/loki/defaults.yaml"), load_profile(ROOT / "examples/loki/loki.yaml"))
        self.assertEqual((diff["before"]["streamCount"], diff["after"]["streamCount"]), (2, 1))
        self.assertEqual(diff["changedPlacementCount"], 4)


if __name__ == "__main__":
    unittest.main()
