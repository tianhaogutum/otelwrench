from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import unittest

from otelwrench.sentry.mapping import convert_bytes, timestamp
from otelwrench.sentry.parser import ConversionError
from .test_parser import envelope

ROOT = Path(__file__).resolve().parents[2]


def convert(event):
    return convert_bytes(json.dumps(event, ensure_ascii=False).encode())


def record(result):
    return result["logs.json"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]


def spans(result):
    return result["traces.json"]["resourceSpans"][0]["scopeSpans"][0]["spans"]


def attrs(obj):
    return {a["key"]: a["value"] for a in obj["attributes"]}


class TimeTests(unittest.TestCase):
    def test_exact_numeric_times(self):
        cases = [(0, "0"), (1700000000, "1700000000000000000"),
                 (Decimal("1700000000.123456789"), "1700000000123456789"),
                 (Decimal("0.000000001"), "1"),
                 (Decimal("18446744073.709551615"), "18446744073709551615"),
                 (Decimal("1700000000.12345678900000000000000000000000"), "1700000000123456789"),
                 (Decimal("0e-999999999"), "0")]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(timestamp(value, "/test"), expected)

    def test_exact_rfc3339_times(self):
        cases = {
            "2023-11-14T22:13:20.123456789Z": "1700000000123456789",
            "2023-11-14T23:13:20.123456789+01:00": "1700000000123456789",
            "2023-11-14T21:13:20.123456789-01:00": "1700000000123456789",
            "2023-11-14t22:13:20.123456789z": "1700000000123456789",
            "2023-11-14T22:13:20.123456789000000000Z": "1700000000123456789",
            "2554-07-21T23:34:33.709551615Z": "18446744073709551615",
            "1970-01-01T01:00:00+01:00": "0",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(timestamp(value, "/test"), expected)

    def test_invalid_or_unrepresentable_times(self):
        values = [-1, True, None, [], {}, 1.25, "1700000000.25",
                  Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"),
                  Decimal("18446744073.709551616"), Decimal("1e999999999"),
                  Decimal("1e-999999999"), Decimal("0.0000000001"),
                  "2023-11-14T22:13:20", "2023-11-14T22:13:20-00:00",
                  "2023-11-14T22:13:20+24:00", "2023-11-14T22:13:20+01:60",
                  "2023-11-14T22:13:60Z", "2023-02-30T22:13:20Z",
                  "1969-12-31T23:59:59Z", "0000-01-01T00:00:00Z",
                  "2023-11-14T22:13:20.0000000001Z", "2023-11-14T22:13:20.１２３Z"]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ConversionError):
                timestamp(value, "/test")


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.transaction = json.loads((ROOT / "examples/sentry/transaction.json").read_text())

    def test_error_golden(self):
        result = convert_bytes((ROOT / "examples/sentry/error.envelope").read_bytes())
        self.assertEqual(result["logs.json"], json.loads((ROOT / "tests/sentry/golden/error.logs.json").read_text()))
        self.assertEqual(result["traces.json"], {"resourceSpans": []})

    def test_transaction_golden(self):
        result = convert_bytes((ROOT / "examples/sentry/transaction.json").read_bytes())
        self.assertEqual(result["traces.json"], json.loads((ROOT / "tests/sentry/golden/transaction.traces.json").read_text()))
        self.assertEqual(result["logs.json"], {"resourceLogs": []})
        self.assertEqual(result["manifest.json"]["unsupportedFieldCategories"], {"data": 1, "measurements": 1})

    def test_missing_optional_context_is_uncorrelated(self):
        result = convert({"message": "uncorrelated"})
        log = record(result)
        self.assertNotIn("traceId", log)
        self.assertNotIn("spanId", log)
        self.assertNotIn("timeUnixNano", log)
        self.assertNotIn("severityNumber", log)
        self.assertTrue(result["manifest.json"]["items"][0]["notes"])

    def test_levels(self):
        for level, number in [("debug", 5), ("info", 9), ("warning", 13), ("error", 17), ("fatal", 21)]:
            with self.subTest(level=level):
                log = record(convert({"message": "m", "level": level}))
                self.assertEqual(log["severityNumber"], number)
                self.assertEqual(log["severityText"], level)
        for value in ("warn", "critical", "ERROR", "", None, 17):
            with self.subTest(value=value), self.assertRaises(ConversionError):
                convert({"message": "m", "level": value})

    def test_message_precedence_and_retention(self):
        event = {"message": "primary", "logentry": {"message": "format %s", "formatted": "format value", "params": ["value"]},
                 "exception": {"values": [{"type": "SyntheticError", "value": "cause"}]}}
        result = convert(event)
        log = record(result)
        self.assertEqual(log["body"], {"stringValue": "primary"})
        self.assertEqual(attrs(log)["sentry.logentry.message"], {"stringValue": "format %s"})
        self.assertEqual(attrs(log)["sentry.logentry.formatted"], {"stringValue": "format value"})
        self.assertEqual(attrs(log)["exception.message"], {"stringValue": "cause"})
        self.assertEqual(result["manifest.json"]["items"][0]["losses"][0]["path"], "/logentry/params")
        del event["message"]
        self.assertEqual(record(convert(event))["body"], {"stringValue": "format value"})
        del event["logentry"]
        self.assertEqual(record(convert(event))["body"], {"stringValue": "SyntheticError: cause"})

    def test_exception_chain_frames_and_loss_paths(self):
        result = convert_bytes((ROOT / "examples/sentry/mixed.envelope").read_bytes())
        log = record(result)
        self.assertEqual(log["body"]["stringValue"], "合成错误 — café\nsecond line")
        attributes = attrs(log)
        self.assertEqual(attributes["exception.type"], {"stringValue": "RuntimeError"})
        chain = attributes["sentry.exception.chain"]["arrayValue"]["values"]
        first = {a["key"]: a["value"] for a in chain[0]["kvlistValue"]["values"]}
        self.assertEqual(first["stacktrace"]["stringValue"], "  File 'synthetic.py', line 12, in parse\nValueError: synthetic cause")
        frame = first["frames"]["arrayValue"]["values"][0]["kvlistValue"]["values"]
        self.assertIn({"key": "lineno", "value": {"intValue": "12"}}, frame)
        paths = {loss["path"] for loss in result["manifest.json"]["items"][0]["losses"]}
        self.assertEqual(paths, {"/user", "/tags", "/contexts/runtime", "/exception/values/1/mechanism", "/exception/values/0/stacktrace/frames/0/vars"})
        serialized = json.dumps(result)
        self.assertNotIn("synthetic-user-do-not-copy", serialized)
        self.assertNotIn("synthetic variable", serialized)
        self.assertEqual(result["manifest.json"]["summary"]["inputItems"], 2)

    def test_all_selected_stack_frame_fields_retained(self):
        fields = {"filename": "a.py", "abs_path": "/synthetic/a.py", "function": "f", "module": "m", "lineno": 1, "colno": 2, "in_app": False}
        result = convert({"exception": {"values": [{"type": "X", "stacktrace": {"frames": [fields]}}]}})
        chain = attrs(record(result))["sentry.exception.chain"]["arrayValue"]["values"]
        exc = {a["key"]: a["value"] for a in chain[0]["kvlistValue"]["values"]}
        actual = {a["key"]: a["value"] for a in exc["frames"]["arrayValue"]["values"][0]["kvlistValue"]["values"]}
        self.assertEqual(set(actual), set(fields))
        self.assertEqual(actual["abs_path"], {"stringValue": "/synthetic/a.py"})
        self.assertEqual(actual["in_app"], {"boolValue": False})
        self.assertEqual(result["manifest.json"]["summary"]["lossEntries"], 0)

    def test_invalid_error_shapes(self):
        bad = [{}, {"message": None}, {"message": {}}, {"logentry": []}, {"logentry": {"formatted": None}},
               {"exception": None}, {"exception": {}}, {"exception": {"values": []}},
               {"exception": {"values": [None]}}, {"exception": {"values": [{}]}},
               {"exception": {"values": [{"type": 3}]}}, {"message": "m", "contexts": None},
               {"message": "m", "contexts": {"trace": []}}, {"message": "m", "release": None},
               {"message": "m", "environment": 3}, {"message": "m", "type": []}]
        for event in bad:
            with self.subTest(event=event), self.assertRaises(ConversionError):
                convert(event)

    def test_invalid_frame_shapes(self):
        bad = [None, [], {"lineno": True}, {"lineno": -1}, {"lineno": 2**63},
               {"colno": 1.2}, {"function": None}, {"in_app": "yes"}]
        for frame in bad:
            event = {"exception": {"values": [{"type": "X", "stacktrace": {"frames": [frame]}}]}}
            with self.subTest(frame=frame), self.assertRaises(ConversionError):
                convert(event)
        for stack in (None, [], {}, {"frames": None}):
            event = {"exception": {"values": [{"type": "X", "stacktrace": stack}]}}
            with self.subTest(stack=stack), self.assertRaises(ConversionError):
                convert(event)

    def test_ids_and_partial_context(self):
        valid = {"trace_id": "A" * 32, "span_id": "B" * 16}
        log = record(convert({"message": "m", "contexts": {"trace": valid}}))
        self.assertEqual(log["traceId"], "a" * 32)
        self.assertEqual(log["spanId"], "b" * 16)
        for field in ("trace_id", "span_id"):
            for value in (None, 1, "0" * (32 if field == "trace_id" else 16), "G" * 32, "abc", ""):
                trace = {**valid, field: value}
                with self.subTest(field=field, value=value), self.assertRaises(ConversionError):
                    convert({"message": "m", "contexts": {"trace": trace}})
            trace = dict(valid)
            del trace[field]
            with self.assertRaisesRegex(ConversionError, "both be supplied"):
                convert({"message": "m", "contexts": {"trace": trace}})

    def test_original_event_id_and_header_consistency(self):
        original = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
        source = b'{"event_id":"' + b'a' * 32 + b'"}\n' + b'{"type":"event"}\n' + json.dumps({"message": "m", "event_id": original}).encode()
        self.assertEqual(attrs(record(convert_bytes(source)))["sentry.event_id"], {"stringValue": original})
        for value in (None, "", "0" * 32, "not-an-id", "a" * 31):
            with self.subTest(value=value), self.assertRaises(ConversionError):
                convert({"message": "m", "event_id": value})
        with self.assertRaisesRegex(ConversionError, "differs"):
            convert_bytes(source.replace(b'"event_id":"aaaaaaaa', b'"event_id":"bbbbbbbb', 1))

    def test_unknown_subtrees_are_each_accounted_without_values(self):
        result = convert({"message": "m", "user": {"email": "do-not-copy"}, "request": {"cookies": "do-not-copy"}, "a/b~c": 2})
        manifest = result["manifest.json"]
        self.assertEqual(manifest["summary"]["lossEntries"], 3)
        self.assertEqual(manifest["unsupportedFieldCategories"], {"a/b~c": 1, "request": 1, "user": 1})
        self.assertIn("/a~1b~0c", {loss["path"] for loss in manifest["items"][0]["losses"]})
        self.assertNotIn("do-not-copy", json.dumps(result))

    def test_transaction_required_fields(self):
        for field in ("type", "transaction", "start_timestamp", "timestamp", "contexts"):
            event = deepcopy(self.transaction)
            del event[field]
            with self.subTest(field=field), self.assertRaises(ConversionError):
                convert(event)
        for field in ("trace_id", "span_id"):
            event = deepcopy(self.transaction)
            del event["contexts"]["trace"][field]
            with self.subTest(field=field), self.assertRaises(ConversionError):
                convert(event)

    def test_child_requires_own_ids_timestamps_and_name(self):
        for field in ("trace_id", "span_id", "start_timestamp", "timestamp"):
            event = deepcopy(self.transaction)
            del event["spans"][0][field]
            with self.subTest(field=field), self.assertRaises(ConversionError):
                convert(event)
        event = deepcopy(self.transaction)
        del event["spans"][0]["description"]
        del event["spans"][0]["op"]
        with self.assertRaises(ConversionError):
            convert(event)

    def test_child_uses_explicit_op_as_name_without_inventing_parent(self):
        event = deepcopy(self.transaction)
        del event["spans"][0]["description"]
        del event["spans"][0]["parent_span_id"]
        child = spans(convert(event))[1]
        self.assertEqual(child["name"], "db.query")
        self.assertNotIn("parentSpanId", child)

    def test_span_end_before_start(self):
        for child in (False, True):
            event = deepcopy(self.transaction)
            target = event["spans"][0] if child else event
            target["timestamp"] = target["start_timestamp"] - 1
            with self.subTest(child=child), self.assertRaisesRegex(ConversionError, "precedes"):
                convert(event)

    def test_span_trace_mismatch_duplicate_self_parent_and_cycle(self):
        events = []
        event = deepcopy(self.transaction)
        event["spans"][0]["trace_id"] = "f" * 32
        events.append(event)
        event = deepcopy(self.transaction)
        event["spans"][0]["span_id"] = "2" * 16
        events.append(event)
        event = deepcopy(self.transaction)
        event["spans"][0]["parent_span_id"] = "3" * 16
        events.append(event)
        event = deepcopy(self.transaction)
        event["contexts"]["trace"]["parent_span_id"] = "3" * 16
        events.append(event)
        for event in events:
            with self.subTest(event=event), self.assertRaises(ConversionError):
                convert(event)

    def test_external_parent_and_zero_duration_allowed(self):
        event = deepcopy(self.transaction)
        event["contexts"]["trace"]["parent_span_id"] = "f" * 16
        event["timestamp"] = event["start_timestamp"]
        event["spans"] = []
        root = spans(convert(event))[0]
        self.assertEqual(root["parentSpanId"], "f" * 16)
        self.assertEqual(root["startTimeUnixNano"], root["endTimeUnixNano"])

    def test_status_semantics_original_retained(self):
        for status, code in [("ok", 1), ("internal_error", 2), ("new-unknown-status", 0)]:
            event = deepcopy(self.transaction)
            event["contexts"]["trace"]["status"] = status
            result = convert(event)
            self.assertEqual(spans(result)[0]["status"], {"code": code})
            self.assertEqual(attrs(spans(result)[0])["sentry.status"], {"stringValue": status})
            if code == 0:
                self.assertEqual(result["manifest.json"]["lossCategories"]["semantic_loss"], 1)

    def test_unsupported_items_default_reject_and_explicit_skip(self):
        source = (ROOT / "examples/sentry/with-attachment.envelope").read_bytes()
        with self.assertRaisesRegex(ConversionError, "unsupported item type"):
            convert_bytes(source)
        result = convert_bytes(source, skip_unsupported=True)
        manifest = result["manifest.json"]
        self.assertEqual(manifest["summary"]["inputItems"], 3)
        self.assertEqual(manifest["summary"]["convertedItems"], 2)
        self.assertEqual(manifest["summary"]["skippedItems"], 1)
        self.assertEqual(manifest["items"][2]["status"], "skipped")
        self.assertEqual(manifest["items"][2]["losses"][-1]["category"], "unsupported_item")
        self.assertNotIn("not-json", json.dumps(result))

    def test_skips_do_not_hide_supported_item_errors_or_bad_framing(self):
        bad_sources = [envelope(b'{"timestamp":NaN,"message":"m"}'),
                       envelope(b'{"type":"transaction"}', kind="event"),
                       envelope(b'{}', kind="attachment")[:-2]]
        for source in bad_sources:
            with self.subTest(source=source), self.assertRaises(ConversionError):
                convert_bytes(source, skip_unsupported=True)

    def test_empty_envelope_and_all_skipped_accounting(self):
        result = convert_bytes(b'{}\n', input_format="envelope")
        self.assertEqual(result["manifest.json"]["summary"]["inputItems"], 0)
        self.assertEqual(result["logs.json"], {"resourceLogs": []})
        self.assertEqual(result["traces.json"], {"resourceSpans": []})
        result = convert_bytes(b'{"event_id":"' + b'a' * 32 + b'"}\n{"type":"attachment","length":0}\n', skip_unsupported=True)
        self.assertEqual(result["manifest.json"]["summary"]["skippedItems"], 1)
        self.assertEqual(result["manifest.json"]["envelopeLosses"][0]["path"], "/envelope/event_id")

    def test_deterministic_outputs(self):
        source = (ROOT / "examples/sentry/mixed.envelope").read_bytes()
        self.assertEqual(convert_bytes(source), convert_bytes(source))


if __name__ == "__main__":
    unittest.main()
