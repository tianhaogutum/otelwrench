# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest

from otelwrench.tracediff import InputError, compare, normalize, render_text
from otelwrench.tracediff.normalize import sorted_canonical

from .support import (BAD_CLI_VALUES, INVALID_API_VALUES, MAX_SAFE, QUERY_PATH, bad_cap_args, bad_otlp_variants,
                      cli_error_matrix, clone, deep_chain, edge_corpus as corpus, format_interplay, hex_, jaeger,
                      jaeger_span, otlp, otlp_span, run, summary, write_json)

EDGE_KEYS = ("edgeLimit", "edgeLimitNotice", "edgeSummary")


def check_view(full, limited, n):
    for side in ("baseline", "candidate"):
        f, c = full[side], limited[side]
        assert c["edgeLimit"] == n
        assert re.search(r"edge.statistic.*canonical order", c["edgeLimitNotice"], re.I)
        assert re.search(r"not a memory.*overall.output bound", c["edgeLimitNotice"], re.I)
        assert c["edges"] == f["edges"][:n]
        assert c["edgeSummary"] == summary(len(f["edges"]), n)
    restored = clone(limited)
    for side in ("baseline", "candidate"):
        restored[side]["edges"] = full[side]["edges"]
        for key in EDGE_KEYS:
            del restored[side][key]
    assert restored == full


def capped(a, b, n=2, opts=None, policy=None):
    return compare(a, b, policy or {}, {**(opts or {}), "edgeLimit": n})


def test_200_distinct_edge_rows_per_side_exact_deduplication_and_full_relation_multiplicity():
    a, b = corpus(200), corpus(200, format="jaeger")
    full, limited = compare(a, b), capped(a, b)
    check_view(full, limited, 2)
    for side, data in (("baseline", a), ("candidate", b)):
        assert limited[side]["edgeSummary"] == summary(200, 2)
        assert limited[side]["duplicateSpanCount"] == 1 and limited[side]["spanCount"] == 601
        assert limited[side]["inputSpanCount"] == 602
        assert len(normalize(data)["edges"]) == 200
        assert sum(e["count"] for e in full[side]["edges"]) == 599
        assert all(e["count"] >= 2 for e in limited[side]["edges"])
    assert len(limited["comparisons"]) == 201 and "structuralWitnesses" not in limited
    lines = render_text(limited).split("\n")
    assert sum(1 for line in lines if re.match(r"  edge .* count=", line)) == 4
    assert sum(1 for line in lines if "edges: listed=2 total=200 omitted=198 truncated=true" in line) == 2


def test_both_sides_zero_below_exact_over_boundaries_and_maximum_safe_integer():
    for left in (0, 1, 2, 3, 200):
        for right in (0, 1, 2, 3, 200):
            a, b = corpus(left), corpus(right, format="jaeger")
            for n in (1, 2, 3, MAX_SAFE):
                check_view(compare(a, b), capped(a, b, n), n)


def test_entirely_empty_otlp_jaeger_captures_stay_invalid_on_either_both_sides():
    for empty in ({"resourceSpans": []}, {"data": []}):
        valid = corpus(0)
        for a, b in ((empty, valid), (valid, empty), (empty, empty)):
            with pytest.raises(InputError, match="capture contains no spans"):
                capped(a, b)


def test_canonical_complete_row_prefix_and_all_six_parent_child_key_dimensions():
    a = corpus(200)
    full, limited = compare(a, a), capped(a, a, 50)
    check_view(full, limited, 50)
    assert limited["baseline"]["edges"] == sorted_canonical(full["baseline"]["edges"])[:50]
    order_input = otlp([otlp_span(span=1, name="z"), otlp_span(span=2, name="a"),
                        otlp_span(span=3, parent=1, name="query"), otlp_span(span=4, parent=2, name="query"),
                        otlp_span(span=5, parent=2, name="query")])
    ordered = compare(order_input, order_input)
    # complete-row count precedes parent
    assert [e["parent"]["operation"] for e in ordered["baseline"]["edges"]] == ["z", "a"]
    check_view(ordered, capped(order_input, order_input, 1), 1)
    reordered = clone(a)
    reordered["resourceSpans"].reverse()
    for r in reordered["resourceSpans"]:
        r["scopeSpans"][0]["spans"].reverse()
    assert limited == capped(reordered, reordered, 50)
    data = otlp([otlp_span(span=1, name="parent"), otlp_span(span=2, parent=1, name="child")])
    for side in ("parent", "child"):
        for dimension in ("service", "operation", "kind"):
            n = len(data["resourceSpans"]) * 2
            parent = otlp_span(span=10 + n, name="other" if side == "parent" and dimension == "operation" else "parent",
                               **({"kind": 3} if side == "parent" and dimension == "kind" else {}))
            child = otlp_span(span=11 + n, parent=int(parent["spanId"], 16),
                              name="other" if side == "child" and dimension == "operation" else "child",
                              **({"kind": 3} if side == "child" and dimension == "kind" else {}))
            data["resourceSpans"].extend([
                *otlp([parent], "other" if side == "parent" and dimension == "service" else "shop")["resourceSpans"],
                *otlp([child], "other" if side == "child" and dimension == "service" else "shop")["resourceSpans"]])
    f = compare(data, data)
    assert len(f["baseline"]["edges"]) == 7
    check_view(f, capped(data, data), 2)


def test_cross_trace_identities_and_child_of_relations_preserve_multiplicity_not_row_count():
    p, c = jaeger_span(span=1), jaeger_span(span=2, parent=1, name="query")
    p2, c2 = jaeger_span(trace=2, span=1), jaeger_span(trace=2, span=2, parent=1, name="query")
    c2["references"].append({"refType": "CHILD_OF", "traceID": hex_(1, 32), "spanID": hex_(1)})
    a = jaeger([p, c, p2, c2, clone(c2)])
    f, limited = compare(a, a, {}, {"witnesses": True}), capped(a, a, 1, {"witnesses": True})
    check_view(f, limited, 1)
    assert limited["baseline"]["edgeSummary"] == summary(1, 1)
    assert limited["baseline"]["edges"][0]["count"] == 3 and limited["baseline"]["spanCount"] == 4
    assert limited["baseline"]["traceCount"] == 2 and limited["baseline"]["duplicateSpanCount"] == 1
    invalid = clone(a)
    invalid["data"][1]["spans"][1]["references"].append(clone(c2["references"][0]))
    with pytest.raises(InputError, match="duplicate CHILD_OF reference"):
        capped(a, invalid)


CAPS = [{}, {"witnessSourceLimit": 1}, {"witnessUnresolvedLimit": 2}, {"witnessParentLimit": 1},
        {"missingParentLimit": 1}, {"upstreamWarningLimit": 2}, {"groupLimit": 1},
        {"witnessSourceLimit": 1, "witnessUnresolvedLimit": 2, "witnessParentLimit": 1, "missingParentLimit": 1,
         "upstreamWarningLimit": 2, "groupLimit": 1}]


def test_all_diagnostic_witness_caps_selected_path_reasons_evidence_and_ambiguity_are_independent():
    a, b = corpus(200, format="jaeger", diagnostics=True), corpus(201, format="jaeger", diagnostics=True)
    for selection in ({}, {"witnessPath": QUERY_PATH}):
        for caps in CAPS:
            opts = {"witnesses": True, **selection, **caps}
            full, limited = compare(a, b, {}, opts), capped(a, b, 2, opts)
            check_view(full, limited, 2)
            query = next(s for s in limited["structuralWitnesses"]["subtrees"] if s["key"]["group"]["operation"] == "query")
            assert query["pairing"] == "ambiguous"
            d = full["baseline"]["diagnostics"]
            assert (d.get("missingParentSummary") or {}).get("total") or len(d["missingParents"])


def test_complete_edge_count_changed_and_witness_evidence_survive_200_omitted_statistic_rows():
    a, b = corpus(200), corpus(200, extra=1)
    # Change every child relation multiplicity without introducing a new aggregate row.
    for i in range(1, len(b["resourceSpans"])):
        spans = b["resourceSpans"][i]["scopeSpans"][0]["spans"]
        spans.append({**spans[0], "spanId": hex_(10000 + i)})
    opts = {"witnesses": True, "witnessPath": QUERY_PATH, "witnessSourceLimit": 1}
    full, limited = compare(a, b, {}, opts), capped(a, b, 1, opts)
    check_view(full, limited, 1)
    assert len(limited["diff"]["edges"]["countChanged"]) == 200
    assert len(limited["structuralWitnesses"]["edgeChanges"]) == 200
    assert all(r["candidateCount"] > r["baselineCount"] for r in limited["structuralWitnesses"]["edgeChanges"])


GATES = [
    ("latency", {"minSamples": 1, "absoluteP95Ms": 0, "relativeP95Percent": 0}, {"offsetMs": 200}),
    ("count", {"countGate": {"maxChangePercent": 0, "maxChangeCount": 0, "minSamples": 1}}, {"extra": 1}),
    ("error", {"errorFractionGate": {"maxIncreasePercent": 0, "maxIncreasePercentagePoints": 0, "minSamples": 1}},
     {"error": True}),
    ("structure", {"failOnStructure": True}, {"rename": True}),
]


@pytest.mark.parametrize("name,policy,change", GATES, ids=[g[0] for g in GATES])
def test_gate_and_complete_comparison_diff_rows_survive_cap_and_query_selection(name, policy, change):
    a, b = corpus(200), corpus(200, **change)
    opts = {"witnesses": True, "witnessPath": QUERY_PATH}
    full, limited = compare(a, b, policy, opts), capped(a, b, 1, opts, policy)
    check_view(full, limited, 1)
    assert limited["gate"]["failed"] is True
    assert len(limited["comparisons"]) == (200 if name == "structure" else 201)
    if name == "count":
        assert len(limited["observationGates"]["count"]) == 201
    if name == "error":
        assert len(limited["observationGates"]["errorFraction"]) == 201


def test_omission_preserves_complete_shape_cap_cannot_bypass_input_path_validation_or_budgets():
    a = corpus(200)
    full = compare(a, a)
    assert len(full["baseline"]["edges"]) == 200
    for key in EDGE_KEYS:
        assert key not in full["baseline"]
    assert not re.search(r"edge limit|edges: listed=", render_text(full), re.I)
    deep = deep_chain(500)
    with pytest.raises(InputError, match="witness path budget exceeded"):
        capped(deep, deep, 1, {"witnesses": True})
    with pytest.raises(InputError, match="path not found"):
        capped(a, a, 1, {"witnesses": True, "witnessPath": [{"service": "absent", "operation": "absent",
                                                              "kind": "server"}]})
    for bad in bad_otlp_variants(lambda: corpus(200)):
        with pytest.raises(InputError):
            capped(a, bad, 1, {"witnesses": True, "witnessPath": QUERY_PATH})


@pytest.mark.parametrize("value", INVALID_API_VALUES, ids=repr)
def test_invalid_api_edge_cap_rejects(value):
    with pytest.raises(InputError, match="edgeLimit"):
        compare(corpus(1), corpus(1), {}, {"edgeLimit": value})


def test_cli_json_text_witnesses_optional_exits_0_1_2_and_first_format_encoding_before_after_cap(tmp_path):
    a, b, path, policy = (tmp_path / n for n in ("a.json", "b.json", "path.json", "policy.json"))
    write_json(a, corpus(200))
    write_json(b, corpus(200, format="jaeger", extra=1))
    write_json(path, QUERY_PATH)
    policy.write_text('{"countGate":{"maxChangePercent":0,"minSamples":1}}')

    def cli(args):
        return run([a, b, *args])

    for opts in ([], ["--witnesses", "--witness-path", path, "--witness-source-limit", "1",
                      "--witness-unresolved-limit", "2", "--witness-parent-limit", "1", "--missing-parent-limit", "1",
                      "--upstream-warning-limit", "2", "--group-limit", "1"]):
        for fmt in ("json", "text"):
            for failed in (False, True):
                r = cli(["--edge-limit", "2", *opts, *(["--config", policy] if failed else []), "--format", fmt])
                assert r.status == (1 if failed else 0) and r.stderr == ""
                if fmt == "json":
                    assert json.loads(r.stdout)["candidate"]["edgeSummary"] == summary(200, 2)
                else:
                    assert "edges: listed=2 total=200 omitted=198 truncated=true" in r.stdout
    for n in ("1", "2.0", "2e0", str(MAX_SAFE)):
        assert cli(["--edge-limit", n]).status == 0
    cli_error_matrix(cli, "--edge-limit", bad_cap_args("--edge-limit", BAD_CLI_VALUES))
    format_interplay(cli, "--edge-limit")
    assert "--edge-limit" in run(["--help"]).stdout
