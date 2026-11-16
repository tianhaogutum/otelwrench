# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest

from otelwrench.tracediff import InputError, compare, render_text
from otelwrench.tracediff.jsontext import UNDEFINED, stringify

from .support import clone, hex_, jaeger, jaeger_span, otlp, otlp_span, run, write_json


def group(operation):
    return {"service": "shop", "operation": operation, "kind": "server"}


def select(a, b, path, policy=None):
    return compare(a, b, {} if policy is None else policy, {"witnesses": True, "witnessPath": path})


def prefix(row, path):
    return [*row["key"]["parentPath"], row["key"]["group"]][:len(path)] == path


def simple():
    return otlp([otlp_span(), otlp_span(span=2, parent=1, name="query"), otlp_span(span=3, name="other")])


def without(report, key):
    return {k: v for k, v in report.items() if k != key}


def test_selected_path_is_an_inclusive_structural_prefix_aggregate_reports_and_source_evidence_stay_whole_corpus():
    a = simple()
    b = clone(a)
    b["resourceSpans"][0]["scopeSpans"][0]["spans"].append(otlp_span(span=4, parent=3, name="added-outside"))
    path = [group("checkout"), group("query")]
    full = compare(a, b, {"failOnStructure": True}, {"witnesses": True})
    selected = select(a, b, path, {"failOnStructure": True})
    sw, fw = selected["structuralWitnesses"], full["structuralWitnesses"]
    assert sw["subtrees"] == [s for s in fw["subtrees"] if prefix(s, path)]
    assert sw["selection"] == {"path": path, "baselineMatches": 1, "candidateMatches": 1}
    assert selected["gate"]["failed"] is True  # Changes outside selection still fail.
    assert without(selected, "structuralWitnesses") == without(full, "structuralWitnesses")
    assert sw["edgeChanges"] == fw["edgeChanges"] and sw["unresolved"] == fw["unresolved"]
    assert re.search(r"Witness selection:.*baseline=1.*candidate=1", render_text(selected))
    assert "aggregate edge evidence and unresolved diagnostics cover the whole corpus" in render_text(selected)


def test_root_selection_includes_descendants_excludes_other_roots_and_preserves_ordering():
    a = simple()
    a["resourceSpans"][0]["scopeSpans"][0]["spans"].append(otlp_span(span=4, parent=2, name="leaf"))
    full = compare(a, a, {}, {"witnesses": True})
    selected = select(a, a, [group("checkout")])
    expected = [s for s in full["structuralWitnesses"]["subtrees"] if prefix(s, [group("checkout")])]
    assert selected["structuralWitnesses"]["subtrees"] == expected
    assert len(selected["structuralWitnesses"]["subtrees"]) == 3


def test_unselected_duplicate_ancestors_still_make_unique_selected_descendants_ambiguous_across_formats_and_traces():
    a = otlp([otlp_span(), otlp_span(trace=2), otlp_span(span=2, parent=1, name="query")])
    b = jaeger([jaeger_span(trace=9, span=12, parent=11, name="query"), jaeger_span(trace=9, span=11)])
    r = select(a, b, [group("checkout"), group("query")])
    row = r["structuralWitnesses"]["subtrees"][0]
    assert len(row["baseline"]) == 1 and len(row["candidate"]) == 1
    assert row["pairing"] == "ambiguous" and row["ambiguityReasons"] == ["ambiguous-parent-path"]
    assert row["candidate"][0]["traceId"] == hex_(9, 32)


def test_one_sided_selector_is_valid_unresolved_spans_and_aggregate_evidence_remain_visible():
    a, b = simple(), otlp([otlp_span(), otlp_span(span=3, parent=99, name="orphan")])
    r = select(a, b, [group("checkout"), group("query")])
    assert r["structuralWitnesses"]["selection"] == {"path": [group("checkout"), group("query")],
                                                     "baselineMatches": 1, "candidateMatches": 0}
    assert r["structuralWitnesses"]["subtrees"][0]["presence"] == "baseline-only"
    assert r["structuralWitnesses"]["unresolved"]["candidate"][0]["reason"] == "missing-parent"
    reverse = select(b, a, [group("checkout"), group("query")])
    assert reverse["structuralWitnesses"]["subtrees"][0]["presence"] == "candidate-only"


def test_delimiter_labels_service_and_kind_match_exactly_without_interpreting_separators():
    g = {"service": "a/b", "operation": 'x/"[]', "kind": "client"}
    a = otlp([otlp_span(name=g["operation"], kind=3), otlp_span(span=2, name=g["operation"])], g["service"])
    r = select(a, a, [g])
    assert len(r["structuralWitnesses"]["subtrees"]) == 1
    assert r["structuralWitnesses"]["subtrees"][0]["key"]["group"] == g
    with pytest.raises(InputError, match=r"witnessPath.*not found"):
        select(a, a, [{**g, "service": "a"}])


@pytest.mark.parametrize("length,name", [(500, "deep"), (14, "x" * 100000)], ids=["deep", "long-label"])
def test_unselected_deep_and_long_label_lineages_do_not_spend_selected_expansion_budgets(length, name):
    spans = [otlp_span(name="selected"),
             *(otlp_span(span=i + 2, name=name, **({"parent": i + 1} if i else {})) for i in range(length))]
    a = otlp(spans)
    with pytest.raises(InputError, match="witness path budget exceeded"):
        compare(a, a, {}, {"witnesses": True})
    r = select(a, a, [group("selected")])
    assert r["baseline"]["spanCount"] == length + 1
    assert len(r["structuralWitnesses"]["subtrees"]) == 1
    with pytest.raises(InputError, match="witness path budget exceeded"):
        select(a, a, [group(name)])


def test_selecting_a_deep_leaf_retains_full_ancestry_without_expanding_intermediate_rows():
    a = otlp([otlp_span(span=i + 1, **({"parent": i} if i else {})) for i in range(500)])
    r = select(a, a, [group("checkout")] * 500)
    assert len(r["structuralWitnesses"]["subtrees"]) == 1
    assert len(r["structuralWitnesses"]["subtrees"][0]["key"]["parentPath"]) == 499
    assert r["structuralWitnesses"]["subtrees"][0]["baseline"][0]["spanId"] == hex_(500)


def test_selector_distinguishes_identical_operation_names_below_different_roots_and_supports_cross_trace_parents():
    child = jaeger_span(trace=2, span=3, name="query")
    child["references"] = [{"refType": "CHILD_OF", "traceID": hex_(1, 32), "spanID": hex_(1)}]
    a = jaeger([child, jaeger_span(name="left"), jaeger_span(span=2, name="right"),
                jaeger_span(span=4, parent=2, name="query")])
    r = select(a, a, [group("left"), group("query")])
    assert len(r["structuralWitnesses"]["subtrees"]) == 1
    assert r["structuralWitnesses"]["subtrees"][0]["baseline"][0]["traceId"] == hex_(2, 32)
    assert r["structuralWitnesses"]["subtrees"][0]["key"]["parentPath"] == [group("left")]


def test_selection_does_not_hide_cyclic_or_conflicting_input_outside_the_selected_root():
    a = simple()
    a["resourceSpans"][0]["scopeSpans"][0]["spans"][2]["parentSpanId"] = hex_(3)
    with pytest.raises(InputError, match="cyclic"):
        select(a, simple(), [group("checkout")])
    b = simple()
    b["resourceSpans"][0]["scopeSpans"][0]["spans"].append(otlp_span(span=3, name="conflict"))
    with pytest.raises(InputError, match="conflicting duplicate"):
        select(simple(), b, [group("checkout")])


INVALID_PATHS = [None, UNDEFINED, [], {}, "checkout", [None], [[]], [{}], [{**group("x"), "extra": True}],
                 [{"service": "shop", "operation": "x"}], [{**group("x"), "kind": 2}], [{**group("x"), "kind": "SERVER"}],
                 [{**group("x"), "service": " "}], [{**group("x"), "operation": ""}]]


@pytest.mark.parametrize("path", INVALID_PATHS, ids=[repr(p) for p in INVALID_PATHS])
def test_invalid_witness_path_rejects(path):
    options = {"witnesses": True, "witnessPath": None if path is UNDEFINED else path}
    with pytest.raises(InputError, match="witnessPath"):
        compare(simple(), simple(), {}, options)


def test_selector_requires_witnesses_and_a_full_root_path_present_on_at_least_one_side():
    for options in ({"witnesses": False, "witnessPath": [group("checkout")]}, {"witnesses": None,
                                                                              "witnessPath": [group("checkout")]}):
        with pytest.raises(InputError):
            compare(simple(), simple(), {}, options)
    with pytest.raises(InputError, match=r"witnessPath.*not found"):
        select(simple(), simple(), [group("query")])
    with pytest.raises(InputError, match=r"witnessPath.*not found"):
        select(simple(), simple(), [group("checkout"), group("absent")])


def test_cli_selector_file_supports_json_text_gate_failure_strict_input_and_option_errors(tmp_path):
    a, b, path, policy = (tmp_path / n for n in ("a.json", "b.json", "path.json", "policy.json"))
    write_json(a, simple())
    candidate = simple()
    candidate["resourceSpans"][0]["scopeSpans"][0]["spans"].append(otlp_span(span=4, name="outside"))
    write_json(b, candidate)
    policy.write_text('{"failOnStructure":true}')
    write_json(path, [group("checkout"), group("query")])

    def cli(args):
        return run([a, b, *args])

    args = ["--witnesses", "--witness-path", path]
    ok = cli([*args, "--format", "json"])
    assert ok.status == 0 and ok.stderr == ""
    assert len(json.loads(ok.stdout)["structuralWitnesses"]["subtrees"]) == 1
    fail = cli([*args, "--config", policy, "--format", "json"])
    assert fail.status == 1 and fail.stderr == "" and json.loads(fail.stdout)["gate"]["failed"] is True
    assert "Witness selection:" in cli(args).stdout
    for bad in (["--witnesses", "--witness-path"], ["--witness-path", path], [*args, "--witness-path", path],
                ["--witnesses", "--witness-path", tmp_path / "missing.json"]):
        r = cli(["--format", "json", *bad])
        assert r.status == 2 and r.stdout == "" and json.loads(r.stderr)["error"]["code"] == "INPUT_ERROR"
    for content in ("[]", "null", '[{"service":"shop","service":"other","operation":"checkout","kind":"server"}]',
                    stringify([group("absent")])):
        path.write_text(content)
        r = cli(["--format", "json", *args])
        assert r.status == 2 and r.stdout == "" and json.loads(r.stderr)["error"]["code"] == "INPUT_ERROR"
