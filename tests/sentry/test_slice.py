import json
from pathlib import Path
import tempfile
import unittest

from otelwrench.sentry.mapping import convert_bytes
from otelwrench.sentry.output import write_output
from otelwrench.sentry.parser import ConversionError

ROOT = Path(__file__).resolve().parents[2]


class SliceTests(unittest.TestCase):
    def test_error_to_complete_directory(self):
        result = convert_bytes((ROOT / "examples/sentry/error.envelope").read_bytes())
        record = result["logs.json"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        self.assertEqual(record["timeUnixNano"], "1700000000250000000")
        self.assertEqual(record["severityNumber"], 17)
        self.assertEqual(record["body"], {"stringValue": "boom"})
        self.assertEqual(record["traceId"], "1" * 32)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "result"
            write_output(result, output)
            self.assertEqual({p.name for p in output.iterdir()}, set(result))
            for name, expected in result.items():
                self.assertEqual(json.loads((output / name).read_text()), expected)
            with self.assertRaises(ConversionError):
                write_output(result, output)

    def test_transaction_maps_root_and_child_exactly(self):
        result = convert_bytes((ROOT / "examples/sentry/transaction.json").read_bytes())
        spans = result["traces.json"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0]["startTimeUnixNano"], "1700000000125000001")
        self.assertEqual(spans[1]["parentSpanId"], spans[0]["spanId"])
        self.assertEqual(result["manifest.json"]["summary"]["lossEntries"], 2)

    def test_invalid_framing_rejected(self):
        with self.assertRaisesRegex(ConversionError, "truncated"):
            convert_bytes(b'{}\n{"type":"event","length":200}\n{}')


if __name__ == "__main__":
    unittest.main()
