# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest

from otelwrench.tracediff import InputError, compare, normalize, render_text
from otelwrench.tracediff.normalize import sorted_canonical

from .support import (BAD_CLI_VALUES, INVALID_API_VALUES, MAX_SAFE, QUERY_PATH, bad_cap_args, cli_error_matrix, clone,
                      deep_chain, hex_, missing_parent_corpus as corpus, otlp, otlp_span, run, summary,
                      unresolved_corpus, write_json)


def check_view(full, limited, n):
    for side in ("baseline", "candidate"):
        f, c = full[side]["diagnostics"], limited[side]["diagnostics"]
        assert c["missingParentLimit"] == n
        assert re.search(r"diagnostic.*entries", c["missingParentLimitNotice"], re.I)
        assert re.search(r"not a memory.*overall.output bound", c["missingParentLimitNotice"], re.I)
        assert c["missingParents"] == f["missingParents"][:n]
        assert c["missingParentSummary"] == summary(len(f["missingParents"]), n)
    restored = clone(limited)
    for side in ("baseline", "candidate"):
        restored[side]["diagnostics"] = full[side]["diagnostics"]
    assert restored == full


def capped(a, b, n=2, opts=None, policy=None):
    return compare(a, b, policy or {}, {**(opts or {}), "missingParentLimit": n})


def test_200_ordinary_missing_parent_entries_cap_each_side_without_witnesses_after_deduplication():
    a, b = corpus(200, duplicate=True), corpus(200, duplicate=True)
    full, limited = compare(a, b), capped(a, b)
    check_view(full, limited, 2)
    for side in ("baseline", "candidate"):
        assert limited[side]["diagnostics"]["missingParentSummary"] == summary(200, 2)
        assert limited[side]["duplicateSpanCount"] == 1 and limited[side]["spanCount"] == 204
    assert "structuralWitnesses" not in limited
    lines = render_text(limited).split("\n")
    assert sum(1 for line in lines if line.startswith("    missing parent ")) == 4
    assert sum(1 for line in lines if "diagnostics: 200 missing parents" in line) == 2
    assert sum(1 for line in lines if "missing parents: listed=2 total=200 omitted=198 truncated=true" in line) == 2
    assert re.search(r"missing parent limit: 2", "\n".join(lines), re.I)


def test_both_sides_empty_below_exact_and_over_boundaries_safe_maximum_and_false_witnesses():
    for a, b in ((0, 0), (0, 200), (200, 0), (1, 2), (2, 3), (3, 2)):
        for n in (1, 2, 3, MAX_SAFE):
            x, y = corpus(a), corpus(b)
            check_view(compare(x, y, {}, {"witnesses": False}), capped(x, y, n, {"witnesses": False}), n)


def test_canonical_relation_order_ignores_span_reference_order_totals_count_relations_not_children():
    a = corpus(30)
    a["data"][0]["spans"][4]["references"].append({"refType": "CHILD_OF", "traceID": hex_(4, 32), "spanID": hex_(1)})
    b = clone(a)
    b["data"][0]["spans"].reverse()
    for s in b["data"][0]["spans"]:
        s["references"].reverse()
    f, c, reordered = compare(a, a), capped(a, a, 5), capped(b, b, 5)
    check_view(f, c, 5)
    assert c["baseline"]["diagnostics"]["missingParentSummary"]["total"] == 31
    assert c["baseline"]["diagnostics"]["missingParents"] == sorted_canonical(f["baseline"]["diagnostics"]["missingParents"])[:5]
    assert c["baseline"]["diagnostics"] == reordered["baseline"]["diagnostics"]
    first_child = [d for d in f["baseline"]["diagnostics"]["missingParents"] if d["spanId"] == hex_(100)]
    assert len(first_child) == 2 and first_child[0]["parentSpanId"] == first_child[1]["parentSpanId"]
    assert first_child[0]["parentTraceId"] != first_child[1]["parentTraceId"]
    assert c == capped(a, a, 5)


WITNESS_CAPS = [{}, {"witnessSourceLimit": 1}, {"witnessUnresolvedLimit": 2}, {"witnessParentLimit": 1},
                {"witnessSourceLimit": 1, "witnessUnresolvedLimit": 2, "witnessParentLimit": 1}]


def test_witness_classification_ambiguity_aggregate_evidence_and_independent_caps_stay_complete():
    a, b = unresolved_corpus(200, duplicate=True), unresolved_corpus(201, duplicate=True)
    for selection in ({}, {"witnessPath": QUERY_PATH}):
        for caps in WITNESS_CAPS:
            opts = {"witnesses": True, **selection, **caps}
            full, limited = compare(a, b, {}, opts), capped(a, b, 2, opts)
            check_view(full, limited, 2)
            assert limited["baseline"]["diagnostics"]["missingParentSummary"]["total"] == 67
            subtrees = limited["structuralWitnesses"]["subtrees"]
            query = next((s for s in subtrees if s["key"]["group"]["operation"] == "query"), subtrees[0])
            assert query["pairing"] == "ambiguous"
    opts = {"witnesses": True, "witnessPath": QUERY_PATH, "witnessSourceLimit": 1, "witnessUnresolvedLimit": 2,
            "witnessParentLimit": 1}
    limited = capped(corpus(200), corpus(200), 2, opts)
    assert limited["structuralWitnesses"]["unresolvedSummary"]["baseline"]["reasonCounts"] == {
        "missing-parent": 200, "multiple-parents": 0, "unresolved-ancestor": 0}


def test_otlp_diagnostics_and_exported_normalize_retain_full_corpus():
    a = otlp([otlp_span(), *(otlp_span(span=i + 2, parent=999) for i in range(4))])
    check_view(compare(a, a), capped(a, a, 2), 2)
    assert len(normalize(a)["diagnostics"]["missingParents"]) == 4


GATE_POLICIES = [
    ("latency", {"minSamples": 1, "absoluteP95Ms": 0, "relativeP95Percent": 0}),
    ("count", {"countGate": {"maxChangePercent": 0, "maxChangeCount": 0, "minSamples": 1}}),
    ("error", {"errorFractionGate": {"maxIncreasePercent": 0, "maxIncreasePercentagePoints": 0, "minSamples": 1}}),
    ("structure", {"failOnStructure": True}),
]


@pytest.mark.parametrize("name,policy", GATE_POLICIES, ids=[g[0] for g in GATE_POLICIES])
def test_gate_uses_complete_corpus_outside_selection(name, policy):
    a, b = corpus(0 if name == "structure" else 2), corpus(200, ms=200, error=True)
    opts = {"witnesses": True, "witnessPath": QUERY_PATH}
    full, limited = compare(a, b, policy, opts), capped(a, b, 1, opts, policy)
    check_view(full, limited, 1)
    assert limited["gate"]["failed"] is True


def test_omission_preserves_full_shape_and_validation_path_budgets_cannot_be_bypassed():
    a = corpus(200)
    f = compare(a, a)
    assert len(f["baseline"]["diagnostics"]["missingParents"]) == 200
    assert "missingParentSummary" not in f["baseline"]["diagnostics"]
    assert "missingParentLimit" not in f["baseline"]["diagnostics"]
    assert not re.search(r"missing parent limit|missing parents: listed=", render_text(f), re.I)
    deep = deep_chain(500)
    with pytest.raises(InputError, match="witness path budget exceeded"):
        capped(deep, deep, 1, {"witnesses": True})
    with pytest.raises(InputError, match="path not found"):
        capped(a, a, 1, {"witnesses": True, "witnessPath": [{"service": "absent", "operation": "absent",
                                                              "kind": "server"}]})
    for kind in ("duplicate", "conflict", "cycle", "invalid"):
        bad = corpus(3)
        spans = bad["data"][0]["spans"]
        if kind == "duplicate":
            spans[4]["references"].append(clone(spans[4]["references"][0]))
        if kind == "conflict":
            spans.append({**spans[4], "operationName": "conflict"})
        if kind == "cycle":
            spans[0]["references"] = [{"refType": "CHILD_OF", "traceID": spans[1]["traceID"],
                                       "spanID": spans[1]["spanID"]}]
        if kind == "invalid":
            spans[-1]["duration"] = -1
        with pytest.raises(InputError):
            capped(a, bad, 1)


@pytest.mark.parametrize("value", INVALID_API_VALUES, ids=repr)
def test_invalid_api_missing_parent_cap_rejects(value):
    with pytest.raises(InputError, match="missingParentLimit"):
        compare(corpus(1), corpus(1), {}, {"missingParentLimit": value})


def test_cli_json_text_no_witnesses_requirement_exits_0_1_2_and_first_format_error_encoding(tmp_path):
    a, b, path, policy = (tmp_path / n for n in ("a.json", "b.json", "path.json", "policy.json"))
    write_json(a, corpus(2, duplicate=True))
    write_json(b, corpus(200, duplicate=True))
    write_json(path, QUERY_PATH)
    policy.write_text('{"countGate":{"maxChangePercent":0,"minSamples":1}}')

    def cli(args):
        return run([a, b, *args])

    for opts in ([], ["--witnesses", "--witness-path", path, "--witness-source-limit", "1",
                      "--witness-unresolved-limit", "2", "--witness-parent-limit", "1"]):
        args = ["--missing-parent-limit", "2", *opts]
        r = cli([*args, "--format", "json"])
        assert r.status == 0 and r.stderr == ""
        assert json.loads(r.stdout)["candidate"]["diagnostics"]["missingParentSummary"] == summary(200, 2)
        text = cli(args)
        assert text.status == 0
        assert "missing parents: listed=2 total=200 omitted=198 truncated=true" in text.stdout
        failed = cli([*args, "--config", policy, "--format", "json"])
        assert failed.status == 1 and json.loads(failed.stdout)["gate"]["failed"] is True
    for n in ("1", "2.0", "2e0", str(MAX_SAFE)):
        assert cli(["--missing-parent-limit", n]).status == 0
    cli_error_matrix(cli, "--missing-parent-limit", bad_cap_args("--missing-parent-limit", BAD_CLI_VALUES))
    assert "--missing-parent-limit" in run(["--help"]).stdout
