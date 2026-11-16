# SPDX-License-Identifier: Apache-2.0
from copy import deepcopy
from decimal import localcontext
import hashlib
import json
from pathlib import Path
import unittest

from prometheus_client.parser import text_string_to_metric_families

from otelwrench.spanmetrics.core import Config, InputError, convert, parse_buckets, parse_dimensions

ROOT = Path(__file__).resolve().parents[2]
TRACE = "a" * 32


def span(i=1, ns=100_000_000, **changes):
    start = 1_700_000_000_000_000_001
    result = {"traceId": TRACE, "spanId": f"{i:016x}", "name": "GET /shop", "kind": 2,
              "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + ns)}
    result.update(changes)
    return result


def document(spans, resource_attrs=None, scope=None):
    return {"resourceSpans": [{"resource": {"attributes": resource_attrs if resource_attrs is not None else
            [{"key": "service.name", "value": {"stringValue": "web"}}]},
            "scopeSpans": [{"scope": scope or {"name": "test"}, "spans": spans}]}]}


def encode(doc):
    return json.dumps(doc).encode()


def run_spans(spans, config=None, **kw):
    return convert(encode(document(spans, **kw)), config)


def samples(result):
    return [s for f in text_string_to_metric_families(result.prometheus) for s in f.samples]


def sample_values(result, name):
    return [s for s in samples(result) if s.name == name]


class CoreTests(unittest.TestCase):
    def test_fixture_independent_parser_and_hand_counts(self):
        data = (ROOT / "examples/spanmetrics/traces.json").read_bytes()
        result = convert(data, Config())
        self.assertEqual(result.report["status"], "ok")
        self.assertEqual(result.report["unique_records"], 2)
        self.assertEqual(result.report["errors"], 1)
        self.assertEqual(result.report["duration_sum_seconds"], "0.4")
        self.assertEqual(result.report["input_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(result.report["prometheus_sha256"], hashlib.sha256(result.prometheus.encode()).hexdigest())
        parsed = samples(result)
        self.assertEqual(len(parsed), 16)
        self.assertEqual(sum(s.value for s in parsed if s.name == "spanmetrics_calls_total"), 2)
        self.assertEqual(sum(s.value for s in parsed if s.name == "spanmetrics_errors_total"), 1)
        expected = {"0": {"buckets": [1, 1, 1, 1], "sum": 0.1, "errors": 0},
                    "2": {"buckets": [0, 1, 1, 1], "sum": 0.3, "errors": 1}}
        for status, values in expected.items():
            group = [s for s in parsed if s.labels["status_code"] == status]
            for sample in group:
                labels = {"service_name": "web", "span_name": "GET /shop", "span_kind": "2", "status_code": status}
                if sample.name.endswith("_bucket"):
                    labels["le"] = sample.labels["le"]
                self.assertEqual(sample.labels, labels)
            self.assertEqual([s.value for s in group if s.name.endswith("_bucket")], values["buckets"])
            self.assertEqual([s.value for s in group if s.name.endswith("_sum")], [values["sum"]])
            self.assertEqual([s.value for s in group if s.name.endswith("_count")], [1])
            self.assertEqual([s.value for s in group if s.name == "spanmetrics_errors_total"], [values["errors"]])

    def test_zero_boundary_equality_and_one_nanosecond_difference(self):
        config = Config(buckets=parse_buckets("0,0.000000001,0.1,0.100000001,0.5"))
        durations = [0, 1, 100_000_000, 100_000_001, 300_000_000]
        result = run_spans([span(i + 1, ns) for i, ns in enumerate(durations)], config)
        self.assertEqual(result.report["duration_sum_seconds"], "0.500000002")
        self.assertEqual([s.value for s in sample_values(result, "spanmetrics_duration_seconds_bucket")], [1, 2, 3, 4, 5, 5])
        self.assertIn("} 0.500000002\n", result.prometheus)

    def test_exact_large_timestamps_and_sum_independent_of_decimal_context(self):
        max_ns = 2**64 - 1
        records = [span(i, startTimeUnixNano="0", endTimeUnixNano=str(max_ns)) for i in (1, 2)]
        with localcontext() as ctx:
            ctx.prec = 6
            result = run_spans(records, Config(buckets=parse_buckets("0,1e-9,1e30")))
        self.assertEqual(result.report["duration_sum_seconds"], "36893488147.41910323")
        self.assertEqual([s.value for s in sample_values(result, "spanmetrics_duration_seconds_bucket")], [0, 0, 2, 2])

    def test_json_integer_timestamps_supported_without_float_coercion(self):
        result = run_spans([span(startTimeUnixNano=2**63, endTimeUnixNano=2**63 + 1)])
        self.assertEqual(result.report["duration_sum_seconds"], "0.000000001")

    def test_all_kinds_and_statuses_include_children(self):
        records = [span(i + 1, kind=i, status={"code": i % 3}, parentSpanId="f" * 16) for i in range(6)]
        records.append(span(7))  # absent status defaults to UNSET
        result = run_spans(records)
        self.assertEqual(result.report["unique_records"], 7)
        self.assertEqual(result.report["errors"], 2)
        self.assertEqual({s.labels["span_kind"] for s in sample_values(result, "spanmetrics_calls_total")}, set("012345"))
        self.assertEqual(sum(s.value for s in sample_values(result, "spanmetrics_calls_total")), 7)

    def test_missing_kind_and_empty_status_are_unset(self):
        s = span(status={})
        del s["kind"]
        result = run_spans([s])
        self.assertEqual(result.report["errors"], 0)
        self.assertEqual(sample_values(result, "spanmetrics_calls_total")[0].labels["span_kind"], "0")
        self.assertEqual(sample_values(result, "spanmetrics_calls_total")[0].labels["status_code"], "0")

    def test_deduplicates_full_records_with_reordered_object_keys(self):
        s = span()
        reverse = dict(reversed(list(s.items())))
        result = run_spans([s, reverse, deepcopy(s)])
        self.assertEqual((result.report["input_records"], result.report["unique_records"], result.report["duplicate_records"]), (3, 1, 2))
        self.assertEqual(result.report["duration_sum_seconds"], "0.1")

    def test_deduplication_across_equal_resource_and_scope_envelopes(self):
        doc = document([span()])
        doc["resourceSpans"].append(deepcopy(doc["resourceSpans"][0]))
        result = convert(encode(doc))
        self.assertEqual(result.report["duplicate_records"], 1)
        self.assertEqual(result.report["unique_records"], 1)

    def test_same_span_id_in_different_traces_is_unique(self):
        result = run_spans([span(), span(traceId="b" * 32)])
        self.assertEqual(result.report["unique_records"], 2)
        self.assertEqual(result.report["duplicate_records"], 0)

    def test_conflicting_duplicate_span_fields_are_rejected(self):
        changes = [{"name": "other"}, {"endTimeUnixNano": "1700000000200000001"},
                   {"status": {"code": 0}}, {"parentSpanId": "e" * 16},
                   {"attributes": [{"key": "unused", "value": {"stringValue": "different"}}]},
                   {"traceId": TRACE.upper()}]
        for change in changes:
            with self.subTest(change=change):
                result = run_spans([span(), span(**change)])
                self.assertIsNone(result.prometheus)
                self.assertEqual(result.report["unique_records"], 1)
                self.assertEqual(result.report["rejected_records"], 1)
                self.assertIn("conflicting duplicate", result.report["diagnostics"][0])

    def test_conflicting_resource_scope_and_schema_metadata_rejected(self):
        for field in ("resource", "scope", "resource_schema", "scope_schema"):
            with self.subTest(field=field):
                doc = document([span()])
                second = deepcopy(doc["resourceSpans"][0])
                if field == "resource":
                    second["resource"]["attributes"].append({"key": "unused", "value": {"stringValue": "other"}})
                elif field == "scope":
                    second["scopeSpans"][0]["scope"]["version"] = "2"
                elif field == "resource_schema":
                    second["schemaUrl"] = "new"
                else:
                    second["scopeSpans"][0]["schemaUrl"] = "new"
                doc["resourceSpans"].append(second)
                result = convert(encode(doc))
                self.assertIsNone(result.prometheus)
                self.assertIn("conflicting duplicate", result.report["diagnostics"][0])

    def test_explicit_empty_metadata_is_not_an_exact_duplicate_of_omission(self):
        doc = document([span()])
        second = deepcopy(doc["resourceSpans"][0])
        second["schemaUrl"] = ""
        doc["resourceSpans"].append(second)
        result = convert(encode(doc))
        self.assertIsNone(result.prometheus)
        self.assertEqual(result.report["rejected_records"], 1)
        self.assertIn("conflicting duplicate", result.report["diagnostics"][0])

    def test_failures_and_duplicates_conserve_record_counts(self):
        result = run_spans([span(), span(), span(name="conflict"), span(2, name=""), span(3)])
        r = result.report
        self.assertIsNone(result.prometheus)
        self.assertEqual([r[k] for k in ("input_records", "unique_records", "duplicate_records", "rejected_records")], [5, 2, 1, 2])
        self.assertTrue(r["record_counts_complete"])

    def test_invalid_span_fields_table(self):
        changes = [{"name": ""}, {"name": " \n"}, {"name": None}, {"name": 1}, {"traceId": "0" * 32},
                   {"traceId": "g" * 32}, {"traceId": "aaa"}, {"spanId": "0" * 16}, {"spanId": None},
                   {"parentSpanId": "0" * 16}, {"traceState": 4}, {"flags": True}, {"flags": 2**32},
                   {"startTimeUnixNano": None}, {"startTimeUnixNano": "1.1"}, {"startTimeUnixNano": "1e9"},
                   {"startTimeUnixNano": "01"}, {"startTimeUnixNano": -1}, {"startTimeUnixNano": 1.0},
                   {"startTimeUnixNano": "-0"},
                   {"startTimeUnixNano": True}, {"endTimeUnixNano": str(2**64)}, {"endTimeUnixNano": "0"},
                   {"kind": -1}, {"kind": 6}, {"kind": "SPAN_KIND_SERVER"}, {"kind": True},
                   {"status": {"code": 3}}, {"status": {"code": "STATUS_CODE_ERROR"}},
                   {"status": {"code": True}}, {"status": None}, {"status": {"message": 2}},
                   {"droppedEventsCount": -1}, {"unsupported": 1}, {"events": {}}, {"links": {}},
                   {"events": [{"name": 1}]}, {"events": [{"timeUnixNano": "-1"}]},
                   {"links": [{"traceId": TRACE, "spanId": "0" * 16}]}, {"name": "\ud800"}]
        for change in changes:
            with self.subTest(change=change):
                result = run_spans([span(**change)])
                self.assertIsNone(result.prometheus)
                self.assertEqual(result.report["rejected_records"], 1)
                self.assertTrue(result.report["diagnostics"])

    def test_missing_required_span_fields(self):
        for key in ("traceId", "spanId", "name", "startTimeUnixNano", "endTimeUnixNano"):
            with self.subTest(key=key):
                s = span()
                del s[key]
                self.assertEqual(run_spans([s]).report["rejected_records"], 1)

    def test_missing_or_invalid_resource_service(self):
        for attrs in ([], [{"key": "service.name", "value": {"intValue": "1"}}],
                      [{"key": "service.name", "value": {"stringValue": ""}}]):
            with self.subTest(attrs=attrs):
                result = run_spans([span()], resource_attrs=attrs)
                self.assertIsNone(result.prometheus)
                self.assertIn("service.name", result.report["diagnostics"][0])

    def test_allowlist_resource_and_span_values_and_escaped_labels(self):
        name = 'GET /"quoted"\\path\n☃'
        resources = [{"key": "service.name", "value": {"stringValue": 'web"\\\n'}},
                     {"key": "env", "value": {"stringValue": "staging"}}]
        attrs = [{"key": "env", "value": {"stringValue": "span-env"}},
                 {"key": "ok", "value": {"boolValue": False}},
                 {"key": "retry", "value": {"intValue": "-3"}},
                 {"key": "fraction", "value": {"doubleValue": 1.25}}]
        config = Config(dimensions=parse_dimensions(["resource:env=environment", "span:env=span_env",
                         "span:ok=ok", "span:retry=retry", "span:fraction=fraction", "span:absent=absent"]))
        result = run_spans([span(name=name, attributes=attrs)], config, resource_attrs=resources)
        labels = sample_values(result, "spanmetrics_calls_total")[0].labels
        self.assertEqual(labels, {"service_name": 'web"\\\n', "span_name": name, "span_kind": "2", "status_code": "0",
                                 "environment": "s:staging", "span_env": "s:span-env", "ok": "b:false",
                                 "retry": "i:-3", "fraction": "d:1.25", "absent": "missing"})
        for sample in samples(result):
            self.assertEqual({k: v for k, v in sample.labels.items() if k != "le"}, labels)
        self.assertEqual(result.report["omitted_attribute_keys"]["span"], [])

    def test_missing_empty_and_differently_typed_attributes_never_collide(self):
        values = [None, {"stringValue": ""}, {"stringValue": "missing"}, {"stringValue": "1"},
                  {"intValue": "1"}, {"doubleValue": 1.0}, {"boolValue": True}]
        records = [span(i + 1, attributes=[] if v is None else [{"key": "custom", "value": v}]) for i, v in enumerate(values)]
        result = run_spans(records, Config(dimensions=parse_dimensions(["span:custom=custom"])))
        self.assertEqual(result.report["groups"], 7)
        labels = {s.labels["custom"] for s in sample_values(result, "spanmetrics_calls_total")}
        self.assertEqual(labels, {"missing", "s:", "s:missing", "s:1", "i:1", "d:1", "b:true"})

    def test_custom_dimension_cardinality_cap_counts_actual_series(self):
        config = Config(dimensions=parse_dimensions(["span:customer=customer"]), max_series=8)
        first = span(attributes=[{"key": "customer", "value": {"stringValue": "a"}}])
        second = span(2, attributes=[{"key": "customer", "value": {"stringValue": "b"}}])
        same = span(3, attributes=first["attributes"])
        within = run_spans([first, same], config)
        self.assertEqual(within.report["status"], "ok")
        self.assertEqual(within.report["series"], 8)
        self.assertEqual(len(samples(within)), 8)
        over = run_spans([first, second], config)
        self.assertIsNone(over.prometheus)
        self.assertEqual(over.report["rejected_records"], 1)
        self.assertIn("series cap", over.report["diagnostics"][0])
        self.assertIsNone(run_spans([first], Config(max_series=7)).prometheus)

    def test_nonallowlisted_cardinality_does_not_split_groups(self):
        records = [span(i + 1, attributes=[{"key": "customer", "value": {"stringValue": str(i)}}]) for i in range(20)]
        result = run_spans(records, Config(max_series=8))
        self.assertEqual(result.report["groups"], 1)
        self.assertEqual(result.report["unique_records"], 20)
        self.assertEqual(result.report["omitted_attribute_keys"]["span"], ["customer"])

    def test_unselected_complex_attributes_and_metadata_loss_are_explicit(self):
        attrs = [{"key": "array", "value": {"arrayValue": {"values": [{"intValue": "1"}]}}},
                 {"key": "kv", "value": {"kvlistValue": {"values": [{"key": "x", "value": {"boolValue": True}}]}}},
                 {"key": "bytes", "value": {"bytesValue": "YWJj"}}]
        s = span(attributes=attrs, events=[{"name": "exception", "timeUnixNano": "1"}],
                 links=[{"traceId": "f" * 32, "spanId": "f" * 16}], status={"code": 2, "message": "failure"})
        result = run_spans([s], scope={"name": "lib", "attributes": [{"key": "scope-only", "value": {"intValue": 3}}]})
        self.assertEqual(result.report["status"], "ok")
        self.assertEqual(result.report["omitted_attribute_keys"], {"resource": [], "span": ["array", "bytes", "kv"], "scope": ["scope-only"]})
        self.assertEqual(result.report["omitted_fields"], {"events": 1, "links": 1, "status.message": 1})
        self.assertTrue(any("sampling" in loss for loss in result.report["losses"]))
        selected = run_spans([s], Config(dimensions=parse_dimensions(["span:array=array"])))
        self.assertIsNone(selected.prometheus)
        self.assertIn("only string/bool/int/finite double", selected.report["diagnostics"][0])

    def test_malformed_attributes_are_rejected_even_when_unselected(self):
        invalid = [{}, {"intValue": str(2**63)}, {"intValue": "-9223372036854775809"},
                   {"intValue": True}, {"stringValue": 1}, {"doubleValue": "NaN"},
                   {"doubleValue": True}, {"bytesValue": "???"}, {"boolValue": 0},
                   {"stringValue": "s", "intValue": 1}, {"arrayValue": {"values": [None]}},
                   {"kvlistValue": {"values": [{"key": "a", "value": None}]}}, {"unknownValue": 1}]
        for value in invalid:
            with self.subTest(value=value):
                result = run_spans([span(attributes=[{"key": "unused", "value": value}])])
                self.assertIsNone(result.prometheus)
        duplicate = [{"key": "x", "value": {"intValue": 1}}] * 2
        self.assertIsNone(run_spans([span(attributes=duplicate)]).prometheus)
        self.assertIsNone(run_spans([span(attributes={})]).prometheus)

    def test_excess_attribute_nesting_is_rejected(self):
        value = {"stringValue": "deep"}
        for _ in range(18):
            value = {"arrayValue": {"values": [value]}}
        result = run_spans([span(attributes=[{"key": "deep", "value": value}])])
        self.assertIsNone(result.prometheus)
        self.assertIn("nesting", result.report["diagnostics"][0])

    def test_container_validation_including_empty_containers(self):
        invalid = [[], {}, {"resourceSpans": None}, {"resourceSpans": [], "logs": []},
                   {"resourceSpans": [{"scopeSpans": {}}]}, {"resourceSpans": [{"resource": {"newField": 0}}]},
                   {"resourceSpans": [{"scopeSpans": [{"scope": {"name": 1}}]}]},
                   {"resourceSpans": [{"schemaUrl": 1}]},
                   {"resourceSpans": [{"scopeSpans": [{"schemaUrl": 1}]}]},
                   {"resourceSpans": [{"scopeSpans": [{"spans": None}]}]}]
        for doc in invalid:
            with self.subTest(doc=doc):
                result = convert(encode(doc))
                self.assertIsNone(result.prometheus)
                self.assertFalse(result.report["record_counts_complete"])

    def test_empty_capture_is_valid_zero_metrics(self):
        result = convert(b'{"resourceSpans": []}')
        self.assertEqual(result.report["status"], "ok")
        self.assertEqual(result.report["series"], 0)
        self.assertEqual(samples(result), [])

    def test_invalid_json_and_unsupported_formats_fail_explicitly(self):
        payloads = [b'{', b'\xff', b'{"resourceSpans": [], "resourceSpans": []}', b'NaN',
                    b'{"resourceSpans": []}\n{"resourceSpans": []}', b'\x1f\x8b', b'Infinity',
                    b'{"resourceSpans": [NaN]}', b'[' * 2000 + b']' * 2000]
        for data in payloads:
            with self.subTest(data=data[:80]):
                result = convert(data)
                self.assertIsNone(result.prometheus)
                self.assertEqual(result.report["status"], "rejected")
                self.assertFalse(result.report["record_counts_complete"])

    def test_numeric_expansion_and_nonfinite_double_rejected(self):
        for token in ("1e999999999", "0e-999999999", "1e309", "1e-325", "1." + "1" * 128):
            data = encode(document([span(attributes=[{"key": "x", "value": {"doubleValue": "TOKEN"}}])]))
            result = convert(data.replace(b'"TOKEN"', token.encode()))
            self.assertIsNone(result.prometheus, token)

    def test_byte_and_span_caps_are_hard_failures(self):
        data = encode(document([span(), span()]))
        result = convert(data, Config(max_input_bytes=len(data) - 1))
        self.assertIsNone(result.prometheus)
        self.assertIn("byte cap", result.report["diagnostics"][0])
        result = convert(data, Config(max_spans=1))
        self.assertIsNone(result.prometheus)
        self.assertIn("span cap", result.report["diagnostics"][0])
        self.assertFalse(result.report["record_counts_complete"])
        self.assertEqual(convert(data, Config(max_spans=2)).report["duplicate_records"], 1)

    def test_diagnostics_bounded_but_rejections_are_not_lost(self):
        result = run_spans([span(i + 1, name="") for i in range(110)])
        self.assertEqual(result.report["rejected_records"], 110)
        self.assertEqual(len(result.report["diagnostics"]), 100)
        self.assertEqual(result.report["diagnostics_omitted"], 10)

    def test_prometheus_output_deterministic_for_unique_record_order(self):
        records = [span(1, name="z"), span(2, name="a"), span(3, name="a")]
        self.assertEqual(run_spans(records).prometheus, run_spans(list(reversed(records))).prometheus)

    def test_bucket_validation(self):
        for spec in ("", "0.5,0.1", "0.1,0.10", "-1,0", "NaN", "Infinity", "1,,2", "1e31",
                     "1e-31", "0e999999999999999999999999", "0e-999999999999999999999999", ",".join(map(str, range(101)))):
            with self.subTest(spec=spec), self.assertRaises(InputError):
                parse_buckets(spec)
        self.assertEqual(len(parse_buckets("0,1e-9,0.1,5E+1")), 4)

    def test_dimension_collisions_reserved_and_invalid_specs(self):
        for specs in (["span:x=service_name"], ["span:x=le"], ["span:x=__internal"], ["span:x=bad-label"],
                      ["span:x=same", "resource:y=same"], ["span:x=a", "span:x=b"],
                      ["resource:service.name=service"], ["scope:x=scope"], ["span:x"], ["span:=empty"],
                      [f"span:x{i}=label{i}" for i in range(33)]):
            with self.subTest(specs=specs), self.assertRaises(InputError):
                parse_dimensions(specs)

    def test_library_config_validation(self):
        for kw in ({"max_series": 0}, {"max_spans": -1}, {"max_input_bytes": True}, {"buckets": ()},
                   {"buckets": (2, 1)}):
            with self.subTest(kw=kw), self.assertRaises(InputError):
                Config(**kw)


if __name__ == "__main__":
    unittest.main()
