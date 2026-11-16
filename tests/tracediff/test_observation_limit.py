# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest

from otelwrench.tracediff import InputError, compare, render_text
from otelwrench.tracediff.jsontext import js_sorted, stringify
from otelwrench.tracediff.normalize import group_key

from .support import (BAD_CLI_VALUES, INVALID_API_VALUES, MAX_SAFE, QUERY_PATH, TUPLES, bad_cap_args, bad_otlp_variants,
                      cli_error_matrix, clone, deep_chain, format_interplay, group_corpus as corpus, otlp, otlp_span,
                      run, summary, tuple_capture, unpaired, write_json)

COUNT_GATE = {"maxChangePercent": 0, "maxChangeCount": 0, "minSamples": 3}
ERROR_FRACTION_GATE = {"maxIncreasePercent": 0, "maxIncreasePercentagePoints": 0, "minSamples": 4}
BOTH = {"minSamples": 5, "countGate": COUNT_GATE, "errorFractionGate": ERROR_FRACTION_GATE}
OBSERVATION_KEYS = ("observationLimit", "observationLimitNotice", "observationSummary")


def capped(a, b, n=2, opts=None, policy=BOTH):
    return compare(a, b, policy, {**(opts or {}), "observationLimit": n})


def check_view(full, limited, n):
    assert limited["observationLimit"] == n
    assert re.search(r"unpaired.*insufficient.samples", limited["observationLimitNotice"], re.I)
    assert re.search(r"not a memory.*overall.output bound", limited["observationLimitNotice"], re.I)
    expected = {}
    for name, rows in full.get("observationGates", {}).items():
        assert limited["observationGates"][name] == rows[:n]
        expected[name] = summary(len(rows), n)
    assert limited["observationSummary"] == expected
    restored = clone(limited)
    if "observationGates" in full:
        restored["observationGates"] = full["observationGates"]
    for key in OBSERVATION_KEYS:
        del restored[key]
    assert restored == full


def test_200_common_groups_plus_unpaired_rows_independent_minima_and_exact_per_enabled_gate_union_totals():
    a, b = unpaired(corpus(200), "removed"), unpaired(corpus(200, format="jaeger"), "added")
    f, limited = compare(a, b, BOTH), capped(a, b)
    check_view(f, limited, 2)
    for name in ("count", "errorFraction"):
        assert limited["observationSummary"][name] == summary(202, 2)
        assert sum(1 for r in f["observationGates"][name] if re.fullmatch(r"(new|removed)-group", r["reason"])) == 2
    assert sum(1 for r in f["observationGates"]["count"] if r["eligible"]) == 133
    assert sum(1 for r in f["observationGates"]["errorFraction"] if r["eligible"]) == 66
    assert limited["gate"]["insufficientSampleGroups"] == 200 and limited["baseline"]["duplicateSpanCount"] == 1
    assert len(limited["baseline"]["groups"]) == 201 and len(limited["candidate"]["groups"]) == 201
    text = render_text(limited)
    for name in ("count", "errorFraction"):
        assert f"{name} rows: listed=2 total=202 omitted=200 truncated=true" in text
        assert sum(1 for s in text.split("\n") if s.startswith(name + " ") and not s.startswith(name + " gate:")
                   and not s.startswith(name + " rows:")) == 2


def test_neither_each_alone_both_below_exact_over_union_boundaries_and_maximum_safe_limit():
    for policy in ({}, {"countGate": COUNT_GATE}, {"errorFractionGate": ERROR_FRACTION_GATE}, BOTH):
        for count in (1, 2, 3, 200):
            a, b = corpus(count), corpus(count, format="jaeger")
            for n in (1, 2, 3, MAX_SAFE):
                f, limited = compare(a, b, policy), capped(a, b, n, {}, policy)
                check_view(f, limited, n)
                assert list(limited["observationSummary"]) == list(f.get("observationGates", {}))
                for s in limited["observationSummary"].values():
                    assert s == summary(count, n)
                if not policy:
                    assert limited["observationSummary"] == {} and "observationGates" not in limited
                    assert "countGate" not in limited["policy"] and "countGateFailures" not in limited["gate"]
                    assert "errorFractionGate" not in limited["policy"]
                    assert "errorFractionGateFailures" not in limited["gate"]
                    assert "observation limit: " in render_text(limited)
                    assert "Count/error-fraction gates are" not in render_text(limited)


def test_no_paired_groups_still_yields_complete_unpaired_union_rows_for_enabled_gates():
    a, b = otlp([otlp_span(name="removed")]), otlp([otlp_span(name="added")])
    f, limited = compare(a, b, BOTH), capped(a, b, 1)
    check_view(f, limited, 1)
    assert len(limited["comparisons"]) == 0
    for name in ("count", "errorFraction"):
        assert [r["reason"] for r in f["observationGates"][name]] == ["new-group", "removed-group"]
        assert limited["observationSummary"][name] == summary(2, 1)


def test_canonical_group_key_ordering_covers_dimensions_delimiter_and_unicode_independent_of_counts_and_input_order():
    a = tuple_capture()
    f, limited = compare(a, a, BOTH), capped(a, a)
    check_view(f, limited, 2)
    expected = js_sorted(stringify([s, o, "client" if k == 3 else "server"]) for s, o, k in TUPLES)
    for rows in f["observationGates"].values():
        assert [group_key(r["group"]) for r in rows] == expected
    assert [r["group"] for r in limited["observationGates"]["count"]] == [
        {"service": "a", "operation": "b|c", "kind": "client"}, {"service": "a", "operation": "b|c", "kind": "server"}]
    rev = clone(a)
    rev["resourceSpans"].reverse()
    for r in rev["resourceSpans"]:
        r["scopeSpans"][0]["spans"].reverse()
    assert limited == capped(rev, rev)


CAPS = [{"witnessSourceLimit": 1}, {"witnessUnresolvedLimit": 2}, {"witnessParentLimit": 1}, {"missingParentLimit": 1},
        {"upstreamWarningLimit": 2}, {"groupLimit": 1}, {"edgeLimit": 2}, {"comparisonLimit": 1}]


def test_every_other_cap_and_selected_path_preserves_comparisons_statistics_diffs_diagnostics_and_witnesses():
    a = unpaired(corpus(200, format="jaeger"), "removed")
    b = unpaired(corpus(201, format="jaeger"), "added")
    combined = {k: v for cap in CAPS for k, v in cap.items()}
    for selection in ({}, {"witnessPath": QUERY_PATH}):
        for cap in ({}, *CAPS, combined):
            opts = {"witnesses": True, **selection, **cap}
            f, limited = compare(a, b, BOTH, opts), capped(a, b, 2, opts)
            check_view(f, limited, 2)
            assert limited["observationSummary"]["count"] == summary(203, 2)
            query = next(s for s in limited["structuralWitnesses"]["subtrees"] if s["key"]["group"]["operation"] == "query")
            assert query["pairing"] == "ambiguous"
            d = f["baseline"]["diagnostics"]
            assert (d.get("missingParentSummary") or {}).get("total") or len(d["missingParents"])


@pytest.mark.parametrize("kind", ["latency", "count", "error", "structure"])
def test_failure_outside_retained_rows_and_selected_subtree_remains_authoritative(kind):
    a, b = corpus(200), corpus(200, format="jaeger")
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
    if kind == "structure":
        for s in spans:
            s["operationName"] += "-new"
    policy = {"minSamples": 3, "absoluteP95Ms": 0, "relativeP95Percent": 0, "failOnStructure": True,
              "countGate": COUNT_GATE, "errorFractionGate": {**ERROR_FRACTION_GATE, "minSamples": 3}}
    opts = {"witnesses": True, "witnessPath": QUERY_PATH, "comparisonLimit": 2}
    f, limited = compare(a, b, policy, opts), capped(a, b, 2, opts, policy)
    check_view(f, limited, 2)
    assert limited["gate"]["failed"] is True
    for rows in limited["observationGates"].values():
        assert all(not r["failed"] for r in rows)
    if kind == "latency":
        assert limited["gate"]["latencyRegressions"] == 1
        assert all(not r["regression"] for r in limited["comparisons"])
    if kind == "count":
        assert limited["gate"]["countGateFailures"] == 1
    if kind == "error":
        assert limited["gate"]["errorFractionGateFailures"] == 1
    if kind == "structure":
        assert limited["gate"]["structuralChanges"] > 0
        assert limited["observationSummary"]["count"] == summary(201, 2)


def test_omission_preserves_full_shape_cap_cannot_hide_empty_input_graph_path_budget_policy_errors():
    a = corpus(200)
    f = compare(a, a, BOTH)
    for key in OBSERVATION_KEYS:
        assert key not in f
    assert len(f["observationGates"]["count"]) == 200
    assert not re.search(r"observation limit|rows: listed=", render_text(f), re.I)
    deep = deep_chain(500)
    with pytest.raises(InputError, match="witness path budget exceeded"):
        capped(deep, deep, 1, {"witnesses": True})
    with pytest.raises(InputError, match="path not found"):
        capped(a, a, 1, {"witnesses": True, "witnessPath": [{"service": "absent", "operation": "absent",
                                                              "kind": "server"}]})
    for empty in ({"resourceSpans": []}, {"data": []}):
        for policy in ({}, BOTH):
            for x, y in ((empty, a), (a, empty), (empty, empty)):
                with pytest.raises(InputError, match="capture contains no spans"):
                    capped(x, y, 2, {}, policy)
    for bad in bad_otlp_variants(lambda: corpus(200)):
        with pytest.raises(InputError):
            capped(a, bad, 1, {"witnesses": True, "witnessPath": QUERY_PATH})
    with pytest.raises(InputError, match="countGate"):
        capped(a, a, 1, {}, {"countGate": {"maxChangePercent": -1}})


@pytest.mark.parametrize("value", INVALID_API_VALUES, ids=repr)
def test_invalid_api_observation_cap_rejects(value):
    with pytest.raises(InputError, match="observationLimit"):
        compare(corpus(1), corpus(1), {}, {"observationLimit": value})


def test_cli_json_text_optional_witnesses_exits_0_1_2_all_gate_configurations_and_first_format(tmp_path):
    a, b, path, policy = (tmp_path / n for n in ("a.json", "b.json", "path.json", "policy.json"))
    write_json(a, corpus(200))
    write_json(b, corpus(200, format="jaeger", extra=1, error=True))
    write_json(path, QUERY_PATH)

    def cli(args):
        return run([a, b, *args])

    for opts in ([], ["--witnesses", "--witness-path", path, "--witness-source-limit", "1",
                      "--witness-unresolved-limit", "2", "--witness-parent-limit", "1", "--missing-parent-limit", "1",
                      "--upstream-warning-limit", "2", "--group-limit", "1", "--edge-limit", "1",
                      "--comparison-limit", "1"]):
        for fmt in ("json", "text"):
            for p in ({}, {"countGate": COUNT_GATE}, {"errorFractionGate": ERROR_FRACTION_GATE}, BOTH):
                write_json(policy, p)
                r = cli(["--observation-limit", "2", *opts, "--config", policy, "--format", fmt])
                assert r.status == (1 if p else 0) and r.stderr == ""
                if fmt == "json":
                    expected = {}
                    if "countGate" in p:
                        expected["count"] = summary(200, 2)
                    if "errorFractionGate" in p:
                        expected["errorFraction"] = summary(200, 2)
                    assert json.loads(r.stdout)["observationSummary"] == expected
                else:
                    assert "observation limit: 2" in r.stdout
                    if "countGate" in p:
                        assert "count rows: listed=2 total=200 omitted=198 truncated=true" in r.stdout
    for n in ("1", "2.0", "2e0", str(MAX_SAFE)):
        assert cli(["--observation-limit", n]).status == 0
    cli_error_matrix(cli, "--observation-limit", bad_cap_args("--observation-limit", BAD_CLI_VALUES))
    format_interplay(cli, "--observation-limit")
    assert "--observation-limit" in run(["--help"]).stdout
