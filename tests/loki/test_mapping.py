import copy
import json
import unittest

from .helpers import ROOT, attr, logs, profile
from otelwrench.loki.config import DEFAULTS, load_profile
from otelwrench.loki.mapping import compare, preview, push_json
from otelwrench.loki.otlp import compact
from otelwrench.loki.strict import PreviewError, read_json


class MappingTests(unittest.TestCase):
    def test_bundled_defaults_are_dated_and_complete(self):
        self.assertEqual(len(DEFAULTS["attributes"]), 18)
        self.assertEqual(len(set(DEFAULTS["attributes"])), 18)
        self.assertIn("deployment.environment", DEFAULTS["attributes"])
        self.assertIn("deployment.environment.name", DEFAULTS["attributes"])
        self.assertIn("2026-11-11", DEFAULTS["retrievedAt"])
        self.assertRegex(DEFAULTS["sourceSha256"], r"^[a-f0-9]{64}$")
        report = preview(logs(resource=[attr(k) for k in DEFAULTS["attributes"]]), profile())
        self.assertEqual(len(report["records"][0]["stream"]), 18)

    def test_defaults_vs_ignore_defaults(self):
        data = logs(resource=[attr("service.name"), attr("k8s.pod.name"), attr("custom")])
        self.assertEqual(set(preview(data, profile())["records"][0]["stream"]), {"service_name", "k8s_pod_name"})
        record = preview(data, profile(resource_attributes={"ignore_defaults": True}))["records"][0]
        self.assertEqual(record["stream"], {})
        self.assertEqual(record["structuredMetadata"], {"service_name": "v", "k8s_pod_name": "v", "custom": "v"})

    def test_explicit_metadata_and_drop_override_defaults(self):
        config = profile(resource_attributes={"attributes_config": [
            {"action": "structured_metadata", "attributes": ["service.name"]},
            {"action": "drop", "attributes": ["k8s.pod.name"]}]})
        record = preview(logs(resource=[attr("service.name"), attr("k8s.pod.name")]), config)["records"][0]
        self.assertEqual(record["stream"], {})
        self.assertEqual(record["structuredMetadata"], {"service_name": "v"})
        self.assertEqual(record["dropped"][0]["sourcePath"], ["k8s.pod.name"])
        self.assertIn("first match", record["dropped"][0]["reason"])

    def test_first_regex_wins_on_original_keys(self):
        config = profile(resource_attributes={"attributes_config": [
            {"action": "structured_metadata", "regex": r"^service\.name$"},
            {"action": "drop", "regex": ".*"}]},
            scope_attributes=[{"action": "structured_metadata", "attributes": ["scope.keep"]},
                              {"action": "drop", "regex": ".*"}],
            log_attributes=[{"action": "structured_metadata", "regex": r"^order\."},
                            {"action": "drop", "regex": ".*"}])
        record = preview(logs(scope=[attr("scope.keep"), attr("scope.drop")],
                              log=[attr("order.id"), attr("private")]), config)["records"][0]
        self.assertEqual(record["structuredMetadata"], {"service_name": "checkout", "scope_keep": "v", "order_id": "v"})
        self.assertEqual({x["sourcePath"][0] for x in record["dropped"]}, {"scope.drop", "private"})

    def test_regex_is_re2_search_and_unicode(self):
        config = profile(log_attributes=[{"action": "drop", "regex": r"\p{Greek}+"}])
        record = preview(logs(log=[attr("prefix.α.suffix")]), config)["records"][0]
        self.assertEqual(record["dropped"][0]["key"], "prefix___suffix")

    def test_nested_map_and_arrays_preserve_values(self):
        nested = {"kvlistValue": {"values": [attr("port", {"intValue": "9007199254740993"}),
                     attr("tags", {"arrayValue": {"values": [
                         {"stringValue": "雪"}, {"boolValue": True}, {"intValue": "42"},
                         {"kvlistValue": {"values": [attr("x", {"doubleValue": 2.5})]}}, {}]}})]}}
        record = preview(logs(log=[attr("net.peer", nested)]), profile())["records"][0]
        self.assertEqual(record["structuredMetadata"], {
            "net_peer_port": "9007199254740993", "net_peer_tags": '["雪",true,42,{"x":2.5},null]'})
        self.assertEqual(record["placements"][1]["sourcePath"], ["net.peer", "port"])
        self.assertTrue(record["placements"][1]["transformations"])

    def test_top_level_rule_applies_to_all_flattened_leaves(self):
        nested = {"kvlistValue": {"values": [attr("x"), attr("y")]}}
        record = preview(logs(log=[attr("parent", nested)]), profile(log_attributes=[
            {"action": "drop", "attributes": ["parent"]}]))["records"][0]
        self.assertEqual([x["key"] for x in record["dropped"]], ["parent_x", "parent_y"])

    def test_primitive_values_and_empty_anyvalue(self):
        values = [("b", {"boolValue": False}, "false"), ("i", {"intValue": "-7"}, "-7"),
                  ("d", {"doubleValue": 1.25}, "1.25"), ("bytes", {"bytesValue": "AAH/"}, "AAH/"),
                  ("empty", {}, ""), ("arr", {"arrayValue": {}}, "[]")]
        record = preview(logs(log=[attr(k, v) for k, v, _ in values]), profile())["records"][0]
        self.assertEqual(record["structuredMetadata"], {k: expected for k, _, expected in values})
        self.assertTrue(all(x["transformations"] for x in record["placements"][1:]))

    def test_nonstring_body_is_compact_json(self):
        body = {"kvlistValue": {"values": [attr("z", {"intValue": "42"}), attr("a", {"boolValue": True})]}}
        record = preview(logs(body=body), profile())["records"][0]
        self.assertEqual(record["line"], '{"a":true,"z":42}')
        self.assertEqual(len(record["transformations"]), 1)
        self.assertEqual(preview(logs(body={"bytesValue": "SGk="}), profile())["records"][0]["line"], "SGk=")

    def test_numeric_string_format_cutoffs(self):
        for number, expected in [(1.0, "1"), (1e20, "100000000000000000000"), (1e21, "1e+21"),
                                 (1e-6, "0.000001"), (1e-7, "1e-7"), (-0.0, "0"), (100.25, "100.25")]:
            with self.subTest(number=number):
                self.assertEqual(compact(number), expected)

    def test_observed_timestamp_fallback_and_both(self):
        record = preview(logs(timeUnixNano="0", observedTimeUnixNano="1700000000000000099"), profile())["records"][0]
        self.assertEqual(record["timestamp"], "1700000000000000099")
        self.assertEqual(record["timestampSource"], "observedTimeUnixNano")
        self.assertNotIn("observed_timestamp", record["structuredMetadata"])
        record = preview(logs(observedTimeUnixNano="1700000000000000099"), profile())["records"][0]
        self.assertEqual(record["structuredMetadata"]["observed_timestamp"], "1700000000000000099")

    def test_fixed_fields_are_metadata_and_not_subject_to_rules(self):
        data = logs(severityNumber=17, severityText="ERROR", traceId="AB" * 16, spanId="CD" * 8,
                    flags=1, droppedAttributesCount=2, eventName="payment.failed")
        scope = data["resourceLogs"][0]["scopeLogs"][0]["scope"]
        scope.update(name="demo", version="1", droppedAttributesCount=3)
        record = preview(data, profile(log_attributes=[{"action": "drop", "regex": ".*"}],
                                       scope_attributes=[{"action": "drop", "regex": ".*"}]))["records"][0]
        self.assertEqual(record["structuredMetadata"], {
            "severity_number": "17", "severity_text": "ERROR", "trace_id": "ab" * 16,
            "span_id": "cd" * 8, "flags": "1", "dropped_attributes_count": "2", "event_name": "payment.failed",
            "scope_name": "demo", "scope_version": "1", "scope_dropped_attributes_count": "3"})

    def test_schema_context_and_upstream_resource_loss_reported(self):
        data = logs()
        resource = data["resourceLogs"][0]
        resource["schemaUrl"] = "https://example.invalid/schema/1"
        resource["resource"]["droppedAttributesCount"] = 4
        resource["scopeLogs"][0]["schemaUrl"] = "https://example.invalid/schema/2"
        report = preview(data, profile())
        self.assertEqual(len(report["diagnostics"]), 3)
        self.assertEqual(report["diagnostics"][1]["value"], 4)
        self.assertNotIn("resource_dropped_attributes_count", report["records"][0]["structuredMetadata"])

    def test_push_order_sorted_by_exact_integer_timestamp(self):
        data = logs()
        records = data["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        records.append(copy.deepcopy(records[0]))
        records[0]["timeUnixNano"] = "1700000000000000002"
        self.assertEqual([v[0] for v in push_json(preview(data, profile()))["streams"][0]["values"]],
                         ["1700000000000000001", "1700000000000000002"])

    def test_empty_input_has_zero_streams(self):
        report = preview({"resourceLogs": []}, profile())
        self.assertEqual(report["summary"]["recordCount"], 0)
        self.assertEqual(report["summary"]["streamCount"], 0)
        self.assertEqual(push_json(report), {"streams": []})

    def test_empty_label_projection_warns_and_push_refuses(self):
        report = preview(logs(), profile(resource_attributes={"ignore_defaults": True}))
        self.assertTrue(any("no nonempty index label" in warning for warning in report["warnings"]))
        with self.assertRaisesRegex(PreviewError, "nonempty index label"):
            push_json(report)

    def test_uint64_timestamp_preserved_but_loki_push_range_checked(self):
        report = preview(logs(timeUnixNano=str(2**64 - 1)), profile())
        self.assertEqual(report["records"][0]["timestamp"], "18446744073709551615")
        with self.assertRaisesRegex(PreviewError, "signed int64"):
            push_json(report)

    def test_empty_index_label_values_require_explicit_demotion(self):
        with self.assertRaisesRegex(PreviewError, "empty index-label value"):
            preview(logs(resource=[attr("service.name", {"stringValue": ""})]), profile())
        report = preview(logs(resource=[attr("service.name", {"stringValue": ""})]),
                         profile(resource_attributes={"ignore_defaults": True}))
        self.assertEqual(report["records"][0]["structuredMetadata"], {"service_name": ""})

    def test_digit_and_special_character_normalization(self):
        record = preview(logs(log=[attr("9host"), attr("http-route"), attr("雪")]), profile())["records"][0]
        self.assertEqual(record["structuredMetadata"], {"_9host": "v", "http_route": "v", "_": "v"})

    def test_comparison_identity_has_no_changes(self):
        data = read_json(ROOT / "examples/loki/logs.json")
        config = load_profile(ROOT / "examples/loki/loki.yaml")
        diff = compare(data, config, config)
        self.assertEqual(diff["streamCountDelta"], 0)
        self.assertEqual(diff["changedPlacements"], [])
        self.assertEqual(diff["changedStreams"], [])

    def test_comparison_reports_changes_even_when_stream_count_same(self):
        data = logs(resource=[attr("service.name"), attr("app.name")])
        before = profile(resource_attributes={"ignore_defaults": True, "attributes_config": [
            {"action": "index_label", "attributes": ["service.name"]}]})
        after = profile(resource_attributes={"ignore_defaults": True, "attributes_config": [
            {"action": "index_label", "attributes": ["app.name"]}]})
        diff = compare(data, before, after)
        self.assertEqual(diff["streamCountDelta"], 0)
        self.assertEqual(diff["changedPlacementCount"], 2)
        self.assertEqual(len(diff["changedStreams"]), 1)
