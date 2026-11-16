# SPDX-License-Identifier: Apache-2.0
import re

from otelwrench.tracediff import compare, normalize, render_text

from .support import clone, hex_, jaeger, jaeger_span, otlp, otlp_span

NO_DIFF = {"operations": {"added": [], "removed": []}, "edges": {"added": [], "removed": [], "countChanged": []}}


def test_ids_order_object_key_order_and_absolute_start_times_do_not_change_metrics_structure():
    a = otlp([otlp_span(), otlp_span(span=2, parent=1, name="query", ms=20)])
    b = otlp([otlp_span(trace=50, span=52, parent=51, name="query", ms=20, start=5000000000),
              otlp_span(trace=50, span=51, start=7000000000)])
    r = compare(a, b, {"minSamples": 1})
    assert r["diff"] == NO_DIFF
    assert r["baseline"]["groups"] == r["candidate"]["groups"] and r["baseline"]["edges"] == r["candidate"]["edges"]
    assert r["regression"] is False and r["baseline"]["edges"][0]["count"] == 1


def test_cross_service_parent_preserved_even_when_child_occurs_first_in_the_file():
    a = otlp([otlp_span()], "frontend")
    a["resourceSpans"][:0] = otlp([otlp_span(span=2, parent=1, name="select", kind=3, ms=5)], "database")["resourceSpans"]
    r = compare(a, a)
    assert r["baseline"]["edges"][0] == {"parent": {"service": "frontend", "operation": "checkout", "kind": "server"},
                                         "child": {"service": "database", "operation": "select", "kind": "client"},
                                         "count": 1}


def test_parent_operation_edge_change_distinguishes_topology_while_operation_groups_stay_identical():
    a = otlp([otlp_span(), otlp_span(span=2, name="cache"), otlp_span(span=3, parent=1, name="query")])
    b = clone(a)
    b["resourceSpans"][0]["scopeSpans"][0]["spans"][2]["parentSpanId"] = hex_(2)
    r = compare(a, b, {"failOnStructure": True})
    assert r["diff"]["operations"] == {"added": [], "removed": []}
    assert r["diff"]["edges"]["added"][0]["parent"]["operation"] == "cache"
    assert r["diff"]["edges"]["removed"][0]["parent"]["operation"] == "checkout"
    assert r["regression"] is True
    assert compare(a, b)["regression"] is False


def test_added_and_removed_operations_are_visible_and_optionally_gated():
    a, b = otlp([otlp_span()]), otlp([otlp_span(name="pay")])
    r = compare(a, b, {"failOnStructure": True})
    assert r["diff"]["operations"]["added"][0]["operation"] == "pay"
    assert r["diff"]["operations"]["removed"][0]["operation"] == "checkout"
    assert r["gate"]["latencyRegressions"] == 0 and r["regression"] is True and len(r["comparisons"]) == 0


def test_repeated_operations_pool_samples_and_edge_multiplicity_group_level_self_edge_is_valid():
    a = otlp([otlp_span(ms=10), otlp_span(span=2, parent=1, ms=20, error=True), otlp_span(span=3, parent=2, ms=30)])
    b = clone(a)
    b["resourceSpans"][0]["scopeSpans"][0]["spans"].append(otlp_span(span=4, parent=3, ms=40))
    r = compare(a, b, {"failOnStructure": True})
    assert r["baseline"]["groups"][0]["count"] == 3 and r["baseline"]["groups"][0]["errorFraction"] == 1 / 3
    assert r["baseline"]["groups"][0]["durationMs"]["median"] == 20 and r["baseline"]["edges"][0]["count"] == 2
    assert r["diff"]["edges"]["countChanged"][0]["candidateCount"] == 3 and r["regression"] is False


def test_same_span_id_in_separate_traces_is_not_deduplicated():
    assert normalize(otlp([otlp_span(), otlp_span(trace=2)]))["spanCount"] == 2


def test_missing_parent_diagnostics_are_explicit_and_do_not_invent_an_edge_or_fail_the_gate():
    a = otlp([otlp_span(parent=99)])
    r = compare(a, a)
    assert r["regression"] is False and len(r["baseline"]["edges"]) == 0
    assert r["baseline"]["diagnostics"]["missingParents"][0]["parentSpanId"] == hex_(99)
    assert re.search(r"1 missing parents \(incomplete capture\)", render_text(r))


def test_parent_lookup_uses_trace_id_not_only_span_id():
    a = otlp([otlp_span(trace=1, parent=2), otlp_span(trace=2, span=2)])
    assert len(compare(a, a)["baseline"]["diagnostics"]["missingParents"]) == 1


def test_jaeger_child_of_supports_multiple_parents_and_cross_trace_edges_follows_from_is_diagnosed():
    child = jaeger_span(trace=2, span=3)
    child["references"] = [{"refType": "CHILD_OF", "traceID": hex_(1, 32), "spanID": hex_(1)},
                           {"refType": "CHILD_OF", "traceID": hex_(1, 32), "spanID": hex_(2)},
                           {"refType": "FOLLOWS_FROM", "traceID": hex_(3, 32), "spanID": hex_(1)}]
    a = jaeger([child, jaeger_span(), jaeger_span(span=2, name="cache")])
    r = compare(a, a)
    assert len(r["baseline"]["edges"]) == 2
    assert any(d["path"] == "span.references.FOLLOWS_FROM" for d in r["baseline"]["diagnostics"]["omittedFields"])


def test_jaeger_process_service_and_integer_otlp_kind_canonicalize_identically():
    a = otlp([otlp_span()], "frontend")
    a["resourceSpans"].extend(otlp([otlp_span(span=2, parent=1, name="sql", kind=3, ms=10)], "db")["resourceSpans"])
    b = jaeger([jaeger_span(), jaeger_span(span=2, parent=1, name="sql", kind="client", ms=10, processID="p2")],
               "frontend")
    b["data"][0]["processes"]["p2"] = {"serviceName": "db", "tags": []}
    r = compare(a, b)
    assert r["diff"] == NO_DIFF
    assert r["baseline"]["groups"] == r["candidate"]["groups"] and r["baseline"]["edges"] == r["candidate"]["edges"]


def test_metadata_and_upstream_warnings_are_visible_while_not_part_of_comparison():
    a = otlp()
    a["resourceSpans"][0]["scopeSpans"][0]["spans"][0].update({
        "attributes": [{"key": "http.method", "value": {"stringValue": "GET"}}],
        "events": [{"name": "started", "timeUnixNano": "1"}],
        "links": [{"traceId": hex_(9, 32), "spanId": hex_(9)}]})
    r = compare(a, a)
    paths = [d["path"] for d in r["baseline"]["diagnostics"]["omittedFields"]]
    assert "span.events" in paths and "span.links" in paths and "span.attributes" in paths
    b = jaeger()
    b["data"][0]["warnings"] = ["truncated"]
    assert 'warning "truncated"' in render_text(compare(b, b))


def test_long_span_graph_validates_without_recursive_stack_overflow():
    spans = [otlp_span(span=i + 1, **({"parent": i + 2} if i < 11999 else {})) for i in range(12000)]
    a = normalize(otlp(spans))
    assert a["spanCount"] == 12000 and len(a["edges"]) == 1
