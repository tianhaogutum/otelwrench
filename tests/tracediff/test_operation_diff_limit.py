# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest

from otelwrench.tracediff import InputError, compare, render_text

from .support import (BAD_CLI_VALUES, INVALID_API_VALUES, MAX_SAFE, QUERY_PATH, TUPLES, bad_cap_args,
                      bad_otlp_variants, cli_error_matrix, clone, deep_chain, format_interplay, group_corpus,
                      operation_corpus as corpus, otlp, otlp_span, run, summary, write_json)

COUNT_GATE = {"maxChangePercent": 0, "maxChangeCount": 0, "minSamples": 3}
ERROR_FRACTION_GATE = {"maxIncreasePercent": 0, "maxIncreasePercentagePoints": 0, "minSamples": 4}
BOTH = {"minSamples": 5, "countGate": COUNT_GATE, "errorFractionGate": ERROR_FRACTION_GATE}


def pair(removed, added, common=3):
    return corpus(removed, prefix="removed", common=common), corpus(added, format="jaeger", common=common)


def capped(a, b, n=2, opts=None, policy=BOTH):
    return compare(a, b, policy, {**(opts or {}), "operationDiffLimit": n})


def check_view(full, limited, n):
    ops = limited["diff"]["operations"]
    assert ops["operationDiffLimit"] == n
    assert re.search(r"distinct.*service/operation/kind.*sample", ops["operationDiffLimitNotice"], re.I)
    assert re.search(r"not a memory.*overall.output bound", ops["operationDiffLimitNotice"], re.I)
    expected = {}
    for action in ("added", "removed"):
        assert ops[action] == full["diff"]["operations"][action][:n]
        expected[action] = summary(len(full["diff"]["operations"][action]), n)
    assert ops["operationDiffSummary"] == expected
    restored = clone(limited)
    restored["diff"]["operations"] = full["diff"]["operations"]
    assert restored == full


def test_200_removed_201_added_distinct_groups_common_and_insufficient_groups_duplicates_exact_totals_and_text():
    a, b = pair(200, 201)
    f, limited = compare(a, b, BOTH), capped(a, b)
    check_view(f, limited, 2)
    assert limited["diff"]["operations"]["operationDiffSummary"] == {"added": summary(201, 2),
                                                                     "removed": summary(200, 2)}
    assert limited["baseline"]["duplicateSpanCount"] == 1 and limited["candidate"]["duplicateSpanCount"] == 1
    assert len(limited["baseline"]["groups"]) == 203 and len(limited["candidate"]["groups"]) == 204
    assert len(limited["comparisons"]) == 3 and limited["gate"]["insufficientSampleGroups"] == 3
    assert sum(1 for r in f["observationGates"]["count"] if re.fullmatch(r"(new|removed)-group", r["reason"])) == 401
    assert limited["gate"]["structuralChanges"] > 401 and limited["gate"]["failed"] is False
    text = render_text(limited)
    for action, total in (("added", 201), ("removed", 200)):
        assert f"{action} operations: listed=2 total={total} omitted={total - 2} truncated=true" in text
        assert sum(1 for s in text.split("\n") if s.startswith(action + " operation ")) == 2


def test_zero_below_exact_over_independently_on_each_list_each_no_observation_gate_and_structure_policy():
    for policy in ({}, {"countGate": COUNT_GATE}, {"errorFractionGate": ERROR_FRACTION_GATE}, BOTH):
        for fail_on_structure in (False, True):
            for removed, added in ((0, 0), (0, 3), (3, 0), (1, 2), (2, 1), (2, 2), (3, 200)):
                a, b = pair(removed, added)
                p = {**policy, "failOnStructure": fail_on_structure}
                for n in (2, MAX_SAFE):
                    f, limited = compare(a, b, p), capped(a, b, n, {}, p)
                    check_view(f, limited, n)
                    assert limited["diff"]["operations"]["operationDiffSummary"] == {
                        "added": summary(added, n), "removed": summary(removed, n)}
                    assert limited["gate"]["failed"] is (fail_on_structure and bool(removed + added))


def test_no_common_groups_sample_minima_never_filter_operation_presence():
    a, b = otlp([otlp_span(name="removed")]), otlp([otlp_span(name="added")])
    policy = {**BOTH, "failOnStructure": True}
    f, limited = compare(a, b, policy), capped(a, b, 2, {}, policy)
    check_view(f, limited, 2)
    assert len(limited["comparisons"]) == 0 and limited["gate"]["insufficientSampleGroups"] == 0
    assert limited["gate"]["structuralChanges"] == 2 and limited["gate"]["failed"] is True
    assert limited["diff"]["operations"]["operationDiffSummary"] == {"added": summary(1, 2), "removed": summary(1, 2)}


def test_existing_canonical_whole_object_order_includes_all_key_dimensions_escaping_and_unicode():
    a = otlp([otlp_span(name="common", span=9999)])
    b = {"resourceSpans": [*a["resourceSpans"], *(r for i, (service, name, kind) in enumerate(TUPLES)
                                                  for r in otlp([otlp_span(span=1 + i * 10 + j, name=name, kind=kind)
                                                                 for j in range(5 - i)], service)["resourceSpans"])]}
    expected = [
        {"service": "a", "operation": "b|c", "kind": "client"},
        {"service": "Ω", "operation": '"\\\n', "kind": "server"},
        {"service": "a", "operation": "b|c", "kind": "server"},
        {"service": "a", "operation": "b|d", "kind": "server"},
        {"service": "a|b", "operation": "c", "kind": "server"},
    ]
    for x, y, action in ((a, b, "added"), (b, a, "removed")):
        f, limited = compare(x, y), capped(x, y)
        check_view(compare(x, y, BOTH), limited, 2)
        assert f["diff"]["operations"][action] == expected
        assert limited["diff"]["operations"][action] == expected[:2]
        rev = clone(b)
        rev["resourceSpans"].reverse()
        for r in rev["resourceSpans"]:
            r["scopeSpans"][0]["spans"].reverse()
        assert limited == (capped(a, rev) if action == "added" else capped(rev, a))


CAPS = [{"witnessSourceLimit": 1}, {"witnessUnresolvedLimit": 2}, {"witnessParentLimit": 1}, {"missingParentLimit": 1},
        {"upstreamWarningLimit": 2}, {"groupLimit": 1}, {"edgeLimit": 2}, {"comparisonLimit": 1},
        {"observationLimit": 1}]


def test_all_nine_other_caps_selected_path_witness_evidence_reasons_ambiguity_and_ordinary_sections_independent():
    a, b = pair(200, 201)
    combined = {k: v for cap in CAPS for k, v in cap.items()}
    for selection in ({}, {"witnessPath": QUERY_PATH}):
        for cap in ({}, *CAPS, combined):
            opts = {"witnesses": True, **selection, **cap}
            f, limited = compare(a, b, BOTH, opts), capped(a, b, 2, opts)
            check_view(f, limited, 2)
            query = next(s for s in limited["structuralWitnesses"]["subtrees"] if s["key"]["group"]["operation"] == "query")
            assert query["pairing"] == "ambiguous"
            d = f["candidate"]["diagnostics"]
            assert (d.get("missingParentSummary") or {}).get("total") or len(d["missingParents"])


@pytest.mark.parametrize("kind", ["latency", "count", "error", "structure"])
def test_failure_outside_selected_subtree_and_retained_prefixes_remains_authoritative(kind):
    a, b = pair(3, 4, 200)
    last = compare(a, b)["comparisons"][-1]["group"]
    assert last["operation"] != "query"
    spans = [s for s in b["data"][0]["spans"] if s["operationName"] == last["operation"]]
    if kind == "latency":
        for s in spans:
            s["duration"] += 200000
    if kind == "count":
        b["data"][0]["spans"].append({**clone(spans[0]), "spanID": "00000000000fffff"})
    if kind == "error":
        for s in spans:
            next(t for t in s["tags"] if t["key"] == "error")["value"] = True
    policy = {"minSamples": 3, "absoluteP95Ms": 0, "relativeP95Percent": 0, "failOnStructure": kind == "structure",
              "countGate": COUNT_GATE, "errorFractionGate": {**ERROR_FRACTION_GATE, "minSamples": 3}}
    opts = {"witnesses": True, "witnessPath": QUERY_PATH, "comparisonLimit": 1, "observationLimit": 1}
    f, limited = compare(a, b, policy, opts), capped(a, b, 2, opts, policy)
    check_view(f, limited, 2)
    assert limited["gate"]["failed"] is True and limited["gate"]["structuralChanges"] > 7
    assert all(not r["regression"] for r in limited["comparisons"])
    for rows in limited["observationGates"].values():
        assert all(not r["failed"] for r in rows)
    if kind == "latency":
        assert limited["gate"]["latencyRegressions"] == 1
    if kind == "count":
        assert limited["gate"]["countGateFailures"] == 1
    if kind == "error":
        assert limited["gate"]["errorFractionGateFailures"] == 1


def test_omission_keeps_full_report_shape_cap_cannot_hide_empty_input_graph_path_policy_budget_errors():
    a, b = pair(200, 200)
    f = compare(a, b)
    assert list(f["diff"]["operations"]) == ["added", "removed"] and len(f["diff"]["operations"]["added"]) == 200
    assert not re.search(r"operation diff limit|operations: listed=", render_text(f), re.I)
    deep = deep_chain(500)
    with pytest.raises(InputError, match="witness path budget exceeded"):
        capped(deep, deep, 1, {"witnesses": True})
    for opts in ({"witnesses": True, "witnessPath": [{"service": "absent", "operation": "absent", "kind": "server"}]},
                 {"witnessPath": QUERY_PATH}):
        with pytest.raises(InputError, match="path not found|requires witnesses"):
            capped(a, b, 1, opts)
    for empty in ({"resourceSpans": []}, {"data": []}):
        for x, y in ((empty, a), (a, empty), (empty, empty)):
            with pytest.raises(InputError, match="capture contains no spans"):
                capped(x, y)
    for bad in bad_otlp_variants(lambda: group_corpus(200)):
        with pytest.raises(InputError):
            capped(a, bad, 1, {"witnesses": True, "witnessPath": QUERY_PATH})
    with pytest.raises(InputError, match="countGate"):
        capped(a, b, 1, {}, {"countGate": {"maxChangePercent": -1}})


@pytest.mark.parametrize("value", INVALID_API_VALUES, ids=repr)
def test_invalid_api_operation_diff_cap_rejects(value):
    with pytest.raises(InputError, match="operationDiffLimit"):
        compare(otlp(), otlp(), {}, {"operationDiffLimit": value})


def test_cli_optional_witnesses_and_all_gates_json_text_exits_0_1_2_first_format_encoding(tmp_path):
    a, b, path, policy = (tmp_path / n for n in ("a.json", "b.json", "path.json", "policy.json"))
    captures = pair(200, 201)
    write_json(a, captures[0])
    write_json(b, captures[1])
    write_json(path, QUERY_PATH)

    def cli(args):
        return run([a, b, *args])

    for opts in ([], ["--witnesses", "--witness-path", path, "--witness-source-limit", "1",
                      "--witness-unresolved-limit", "2", "--witness-parent-limit", "1", "--missing-parent-limit", "1",
                      "--upstream-warning-limit", "2", "--group-limit", "1", "--edge-limit", "1",
                      "--comparison-limit", "1", "--observation-limit", "1"]):
        for fmt in ("json", "text"):
            for p in ({}, {"countGate": COUNT_GATE}, {"errorFractionGate": ERROR_FRACTION_GATE}, BOTH):
                for fail_on_structure in (False, True):
                    write_json(policy, {**p, "failOnStructure": fail_on_structure})
                    r = cli(["--operation-diff-limit", "2", *opts, "--config", policy, "--format", fmt])
                    assert r.status == (1 if fail_on_structure else 0) and r.stderr == ""
                    if fmt == "json":
                        assert json.loads(r.stdout)["diff"]["operations"]["operationDiffSummary"] == {
                            "added": summary(201, 2), "removed": summary(200, 2)}
                    else:
                        assert "operation diff limit: 2" in r.stdout
                        assert "added operations: listed=2 total=201 omitted=199 truncated=true" in r.stdout
    for n in ("1", "2.0", "2e0", str(MAX_SAFE)):
        assert cli(["--operation-diff-limit", n]).status == 0
    values = [v for v in BAD_CLI_VALUES if v != "-Infinity"]
    cli_error_matrix(cli, "--operation-diff-limit", bad_cap_args("--operation-diff-limit", values))
    format_interplay(cli, "--operation-diff-limit")
    assert "--operation-diff-limit" in run(["--help"]).stdout
