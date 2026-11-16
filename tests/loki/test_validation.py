import json
from pathlib import Path
import tempfile
import unittest

from .helpers import attr, logs, profile
from otelwrench.loki.config import parse_profile
from otelwrench.loki.mapping import preview
from otelwrench.loki.strict import PreviewError, read_json, read_yaml, read_text


class ValidationTests(unittest.TestCase):
    def test_invalid_or_unsupported_profiles(self):
        invalid = [
            ({}, "missing limits_config"),
            ({"limits_config": None}, "expected an object"),
            ({"limits_config": {"otlp_config": None}}, "expected an object"),
            ({"limits_config": {}, "distributor": {}}, "unsupported fields"),
            ({"limits_config": {"discover_service_name": []}}, "unsupported fields"),
            ({"limits_config": {"allow_structured_metadata": False}}, "must be true"),
            ({"limits_config": {"allow_structured_metadata": "true"}}, "must be true"),
            ({"limits_config": {"otlp_config": {"severity_text_as_label": True}}}, "unsupported fields"),
            ({"limits_config": {"otlp_config": {"resource_attributes": {"ignore_defaults": 1}}}}, "boolean"),
            ({"limits_config": {"otlp_config": {"resource_attributes": {"unknown": []}}}}, "unsupported fields"),
        ]
        for data, message in invalid:
            with self.subTest(data=data), self.assertRaisesRegex(PreviewError, message):
                parse_profile(data)

    def test_scope_and_log_index_labels_fail_even_without_records(self):
        for level in ("scope_attributes", "log_attributes"):
            with self.subTest(level=level), self.assertRaisesRegex(PreviewError, "only for resource"):
                profile(**{level: [{"action": "index_label", "attributes": ["x"]}]})

    def test_invalid_rule_shapes_and_regex(self):
        cases = [({"action": "drop", "regex": "["}, "invalid RE2 regex"),
                 ({"action": "drop", "regex": "(?=x)"}, "invalid RE2 regex"),
                 ({"action": "drop", "regex": r"(x)\1"}, "invalid RE2 regex"),
                 ({"action": "unknown", "regex": ".*"}, "action must be"),
                 ({"regex": ".*"}, "action must be"),
                 ({"action": "drop"}, "exactly one"),
                 ({"action": "drop", "regex": ".*", "attributes": ["x"]}, "exactly one"),
                 ({"action": "drop", "attributes": []}, "nonempty and unique"),
                 ({"action": "drop", "attributes": ["x", "x"]}, "nonempty and unique"),
                 ({"action": "drop", "attributes": [""]}, "nonempty string"),
                 ({"action": "drop", "attributes": "x"}, "expected an array"),
                 ({"action": "drop", "regex": 1}, "nonempty string"),
                 ({"action": "drop", "regex": ".*", "replace": "x"}, "unsupported fields")]
        for rule, message in cases:
            with self.subTest(rule=rule), self.assertRaisesRegex(PreviewError, message):
                profile(log_attributes=[rule])

    def test_malformed_anyvalue_variants(self):
        cases = [None, [], {"unknownValue": "x"}, {"stringValue": "x", "intValue": "1"},
                 {"stringValue": 1}, {"boolValue": 1}, {"intValue": 1}, {"intValue": "01"}, {"intValue": "-0"},
                 {"intValue": str(2**63)}, {"intValue": str(-2**63 - 1)},
                 {"doubleValue": True}, {"doubleValue": "NaN"}, {"doubleValue": float("inf")},
                 {"bytesValue": "%%%"}, {"bytesValue": "AB=="}, {"bytesValue": "YQ"},
                 {"arrayValue": []}, {"arrayValue": {"values": [1]}},
                 {"kvlistValue": {"values": [{"key": "x"}]}},
                 {"kvlistValue": {"values": [attr("x"), attr("x")]}},
                 {"stringValue": "\ud800"}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(PreviewError):
                preview(logs(body=value), profile())

    def test_empty_maps_are_rejected_as_attributes(self):
        for value in ({"kvlistValue": {}}, {"kvlistValue": {"values": [attr("child", {"kvlistValue": {}})]}}):
            with self.subTest(value=value), self.assertRaisesRegex(PreviewError, "empty map"):
                preview(logs(log=[attr("parent", value)]), profile())
        # Empty maps in a body or in an array have a lossless JSON representation.
        self.assertEqual(preview(logs(body={"kvlistValue": {}}), profile())["records"][0]["line"], "{}")

    def test_missing_and_bad_timestamps(self):
        for value in (None, 1700000000000000001, "", "-1", "1.0", "01", str(2**64), True):
            with self.subTest(value=value), self.assertRaisesRegex(PreviewError, "timestamp"):
                preview(logs(timeUnixNano=value), profile())
        with self.assertRaisesRegex(PreviewError, "missing timestamp"):
            preview(logs(timeUnixNano="0"), profile())
        with self.assertRaisesRegex(PreviewError, "timestamp"):
            preview(logs(observedTimeUnixNano="bad"), profile())

    def test_unknown_otlp_fields_are_never_ignored(self):
        for mutate in (
            lambda d: d.update(resourceSpans=[]),
            lambda d: d["resourceLogs"][0].update(unknown=1),
            lambda d: d["resourceLogs"][0]["resource"].update(entityRefs=[]),
            lambda d: d["resourceLogs"][0]["scopeLogs"][0].update(unknown=1),
            lambda d: d["resourceLogs"][0]["scopeLogs"][0]["scope"].update(unknown=1),
            lambda d: d["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0].update(unknown=1),
        ):
            data = logs()
            mutate(data)
            with self.assertRaisesRegex(PreviewError, "unsupported fields"):
                preview(data, profile())

    def test_missing_resource_logs_or_wrong_arrays(self):
        for data in ({}, {"resource_logs": []}, [], {"resourceLogs": {}},
                     {"resourceLogs": [{"scopeLogs": "bad"}]}, {"resourceLogs": [None]}):
            with self.subTest(data=data), self.assertRaises(PreviewError):
                preview(data, profile())

    def test_duplicate_attributes_fail_at_every_level(self):
        for level in ("resource", "scope", "log"):
            with self.subTest(level=level), self.assertRaisesRegex(PreviewError, "duplicate attribute"):
                preview(logs(**{level: [attr("same"), attr("same")]}), profile())

    def test_malformed_attribute_shapes(self):
        for value in ([{"key": ""}], [{"key": "a"}], [{"key": "a", "value": {}, "unknown": "v"}],
                      [{"key": 1, "value": {}}], ["x"], {}):
            with self.subTest(value=value), self.assertRaises(PreviewError):
                preview(logs(log=value), profile())

    def test_normalized_collisions_with_equal_or_different_values(self):
        for second in ("v", "other"):
            with self.subTest(second=second), self.assertRaisesRegex(PreviewError, "normalized key collision"):
                preview(logs(log=[attr("a.b"), attr("a_b", {"stringValue": second})]), profile())

    def test_nested_key_collision(self):
        nested = {"kvlistValue": {"values": [attr("child")]}}
        with self.assertRaisesRegex(PreviewError, "normalized key collision"):
            preview(logs(log=[attr("parent", nested), attr("parent_child")]), profile())

    def test_cross_level_and_fixed_field_collisions(self):
        for data in (logs(resource=[attr("a.b")], log=[attr("a_b")]),
                     logs(log=[attr("severity.text")], severityText="INFO"),
                     logs(scope=[attr("order.id")], log=[attr("order_id")])):
            with self.subTest(data=data), self.assertRaisesRegex(PreviewError, "normalized key collision"):
                preview(data, profile())

    def test_dropped_collision_still_fails(self):
        with self.assertRaisesRegex(PreviewError, "normalized key collision"):
            preview(logs(log=[attr("a.b"), attr("a_b")]), profile(log_attributes=[{"action": "drop", "regex": ".*"}]))

    def test_invalid_fixed_fields(self):
        for fields in ({"severityNumber": 25}, {"severityNumber": "9"}, {"severityText": 9},
                       {"flags": -1}, {"flags": 2**32}, {"droppedAttributesCount": True},
                       {"traceId": "0" * 32}, {"traceId": "zz" * 16}, {"traceId": "AA=="},
                       {"spanId": "1" * 15}, {"spanId": "0" * 16}, {"eventName": None}):
            with self.subTest(fields=fields), self.assertRaises(PreviewError):
                preview(logs(**fields), profile())

    def test_anyvalue_depth_limit(self):
        value = {"stringValue": "leaf"}
        for _ in range(34):
            value = {"arrayValue": {"values": [value]}}
        with self.assertRaisesRegex(PreviewError, "nesting exceeds"):
            preview(logs(body=value), profile())

    def test_strict_json_duplicate_and_nonfinite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for content in ('{"resourceLogs":[],"resourceLogs":[]}', '{"value":NaN}', '{"value":Infinity}'):
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises(PreviewError):
                    read_json(path)

    def test_yaml_duplicate_keys_aliases_and_unsafe_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.yaml"
            for content in ("limits_config: {}\nlimits_config: {}\n", "a: &a [1]\nb: *a\n", "1: x\n"):
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises(PreviewError):
                    read_yaml(path)

    def test_input_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input"
            path.write_text("12345")
            with self.assertRaisesRegex(PreviewError, "exceeds"):
                read_text(path, 4)
