# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest

from otelwrench.tracediff import InputError, compare, normalize, render_text
from otelwrench.tracediff.normalize import sorted_canonical

from .support import (BAD_CLI_VALUES, INVALID_API_VALUES, MAX_SAFE, QUERY_PATH, bad_cap_args, cli_error_matrix, clone,
                      deep_chain, otlp, run, summary, warning_corpus as corpus, write_json)


def check_view(full, limited, n):
    for side in ("baseline", "candidate"):
        f, c = full[side]["diagnostics"], limited[side]["diagnostics"]
        assert c["upstreamWarningLimit"] == n
        assert re.search(r"occurrences.*before span deduplication", c["upstreamWarningLimitNotice"], re.I)
        assert re.search(r"not a memory.*overall.output bound", c["upstreamWarningLimitNotice"], re.I)
        assert c["upstreamWarnings"] == f["upstreamWarnings"][:n]
        assert c["upstreamWarningSummary"] == summary(len(f["upstreamWarnings"]), n)
    restored = clone(limited)
    for side in ("baseline", "candidate"):
        restored[side]["diagnostics"] = full[side]["diagnostics"]
    assert restored == full


def capped(a, b, n=2, opts=None, policy=None):
    return compare(a, b, policy or {}, {**(opts or {}), "upstreamWarningLimit": n})


def test_200_warning_occurrences_per_side_including_duplicates_cap_without_witnesses():
    a, b = corpus(200, duplicate=True), corpus(200, duplicate=True)
    full, limited = compare(a, b), capped(a, b)
    check_view(full, limited, 2)
    for side in ("baseline", "candidate"):
        assert limited[side]["diagnostics"]["upstreamWarningSummary"] == summary(200, 2)
        assert limited[side]["duplicateSpanCount"] == 1 and limited[side]["spanCount"] == 11
        assert len(normalize(a)["diagnostics"]["upstreamWarnings"]) == 200
    assert "structuralWitnesses" not in limited
    lines = render_text(limited).split("\n")
    assert sum(1 for line in lines if line.startswith("    warning ")) == 4
    assert sum(1 for line in lines if "; 200 upstream warnings" in line) == 2
    assert sum(1 for line in lines if "upstream warnings: listed=2 total=200 omitted=198 truncated=true" in line) == 2


def test_warning_totals_count_repeated_string_elements_and_exact_duplicate_span_occurrences():
    a = corpus(0)
    trace = a["data"][0]
    trace["warnings"] = ["same", "same"]
    trace["spans"][0]["warnings"] = ["same", "same"]
    trace["spans"].append(clone(trace["spans"][0]))
    full, limited = compare(a, a), capped(a, a, 4)
    check_view(full, limited, 4)
    assert full["baseline"]["spanCount"] == 11 and full["baseline"]["duplicateSpanCount"] == 1
    assert limited["baseline"]["diagnostics"]["upstreamWarningSummary"]["total"] == 6
    warnings = full["baseline"]["diagnostics"]["upstreamWarnings"]
    assert len({w["path"] for w in warnings}) == 3
    assert sum(1 for w in warnings if w["path"] == "$.data[0].warnings") == 2


def test_both_sides_empty_below_exact_and_over_boundaries_safe_maximum_and_otlp():
    for a, b in ((0, 0), (0, 200), (200, 0), (1, 2), (2, 3), (3, 2)):
        for n in (1, 2, 3, MAX_SAFE):
            x, y = corpus(a), corpus(b)
            check_view(compare(x, y, {}, {"witnesses": False}), capped(x, y, n, {"witnesses": False}), n)
    a = otlp()
    check_view(compare(a, a), capped(a, a), 2)


def test_canonical_order_uses_full_message_path_objects_identical_messages_retain_every_source():
    a = corpus(200, duplicate=True)
    b = clone(a)
    for trace in b["data"]:
        if trace.get("warnings"):
            trace["warnings"].reverse()
        for s in trace["spans"]:
            if s.get("warnings"):
                s["warnings"].reverse()
    full, limited = compare(a, a), capped(a, a, 150)
    check_view(full, limited, 150)
    assert limited["baseline"]["diagnostics"]["upstreamWarnings"] == \
        sorted_canonical(full["baseline"]["diagnostics"]["upstreamWarnings"])[:150]
    assert limited == capped(b, b, 150)
    assert limited == capped(a, a, 150)


CAPS = [{}, {"witnessSourceLimit": 1}, {"witnessUnresolvedLimit": 2}, {"witnessParentLimit": 1},
        {"missingParentLimit": 1},
        {"witnessSourceLimit": 1, "witnessUnresolvedLimit": 2, "witnessParentLimit": 1, "missingParentLimit": 1}]


def test_witness_reasons_evidence_ambiguity_missing_parents_and_all_other_caps_are_independent():
    a, b = corpus(200, duplicate=True), corpus(201, duplicate=True, unresolved=9)
    for selection in ({}, {"witnessPath": QUERY_PATH}):
        for caps in CAPS:
            opts = {"witnesses": True, **selection, **caps}
            full, limited = compare(a, b, {}, opts), capped(a, b, 2, opts)
            check_view(full, limited, 2)
            query = next(s for s in limited["structuralWitnesses"]["subtrees"] if s["key"]["group"]["operation"] == "query")
            assert query["pairing"] == "ambiguous"
            assert limited["baseline"]["diagnostics"]["missingParents"] == full["baseline"]["diagnostics"]["missingParents"]


GATE_POLICIES = [
    ("latency", {"minSamples": 1, "absoluteP95Ms": 0, "relativeP95Percent": 0}),
    ("count", {"countGate": {"maxChangePercent": 0, "maxChangeCount": 0, "minSamples": 1}}),
    ("error", {"errorFractionGate": {"maxIncreasePercent": 0, "maxIncreasePercentagePoints": 0, "minSamples": 1}}),
    ("structure", {"failOnStructure": True}),
]


@pytest.mark.parametrize("name,policy", GATE_POLICIES, ids=[g[0] for g in GATE_POLICIES])
def test_gate_uses_complete_corpus_outside_selected_query(name, policy):
    a = corpus(200, unresolved=0 if name == "structure" else 3)
    b = corpus(200, unresolved=9, ms=200, error=True)
    opts = {"witnesses": True, "witnessPath": QUERY_PATH}
    full, limited = compare(a, b, policy, opts), capped(a, b, 1, opts, policy)
    check_view(full, limited, 1)
    assert limited["gate"]["failed"] is True


def test_omission_preserves_full_shape_cap_cannot_bypass_validation_or_path_budgets():
    a = corpus(200)
    full = compare(a, a)
    assert len(full["baseline"]["diagnostics"]["upstreamWarnings"]) == 200
    for k in ("upstreamWarningSummary", "upstreamWarningLimit", "upstreamWarningLimitNotice"):
        assert k not in full["baseline"]["diagnostics"]
    assert not re.search(r"upstream warning limit|upstream warnings: listed=", render_text(full), re.I)
    deep = deep_chain(500)
    with pytest.raises(InputError, match="witness path budget exceeded"):
        capped(deep, deep, 1, {"witnesses": True})
    with pytest.raises(InputError, match="path not found"):
        capped(a, a, 1, {"witnesses": True, "witnessPath": [{"service": "absent", "operation": "absent",
                                                              "kind": "server"}]})
    for kind in ("warning", "trace warning", "conflict", "cycle", "invalid"):
        bad = corpus(200)
        spans = bad["data"][0]["spans"]
        if kind == "warning":
            spans[-1]["warnings"] = ["valid", 42]
        if kind == "trace warning":
            bad["data"][-1]["warnings"] = ["valid", False]
        if kind == "conflict":
            spans.append({**spans[0], "operationName": "conflict"})
        if kind == "cycle":
            spans[0]["references"] = [{"refType": "CHILD_OF", "traceID": spans[1]["traceID"],
                                       "spanID": spans[1]["spanID"]}]
        if kind == "invalid":
            spans[-1]["duration"] = -1
        with pytest.raises(InputError):
            capped(a, bad, 1, {"witnesses": True, "witnessPath": QUERY_PATH})


@pytest.mark.parametrize("value", INVALID_API_VALUES, ids=repr)
def test_invalid_api_upstream_warning_cap_rejects(value):
    with pytest.raises(InputError, match="upstreamWarningLimit"):
        compare(corpus(1), corpus(1), {}, {"upstreamWarningLimit": value})


def test_cli_json_text_witnesses_optional_exits_0_1_2_and_first_format_encoding_before_after_cap(tmp_path):
    a, b, path, policy = (tmp_path / n for n in ("a.json", "b.json", "path.json", "policy.json"))
    write_json(a, corpus(200, duplicate=True, unresolved=3))
    write_json(b, corpus(200, duplicate=True))
    write_json(path, QUERY_PATH)
    policy.write_text('{"countGate":{"maxChangePercent":0,"minSamples":1}}')

    def cli(args):
        return run([a, b, *args])

    for opts in ([], ["--witnesses", "--witness-path", path, "--witness-source-limit", "1",
                      "--witness-unresolved-limit", "2", "--witness-parent-limit", "1", "--missing-parent-limit", "1"]):
        args = ["--upstream-warning-limit", "2", *opts]
        for fmt in ("json", "text"):
            for failed in (False, True):
                r = cli([*args, *(["--config", policy] if failed else []), "--format", fmt])
                assert r.status == (1 if failed else 0) and r.stderr == ""
                if fmt == "json":
                    assert json.loads(r.stdout)["candidate"]["diagnostics"]["upstreamWarningSummary"] == summary(200, 2)
                else:
                    assert "upstream warnings: listed=2 total=200 omitted=198 truncated=true" in r.stdout
    for n in ("1", "2.0", "2e0", str(MAX_SAFE)):
        assert cli(["--upstream-warning-limit", n]).status == 0
    cli_error_matrix(cli, "--upstream-warning-limit", bad_cap_args("--upstream-warning-limit", BAD_CLI_VALUES))
    assert "--upstream-warning-limit" in run(["--help"]).stdout
