# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest

from otelwrench.tracediff import InputError, compare, normalize, render_text
from otelwrench.tracediff.jsontext import js_sorted, stringify
from otelwrench.tracediff.normalize import group_key

from .support import (BAD_CLI_VALUES, INVALID_API_VALUES, MAX_SAFE, QUERY_PATH, bad_cap_args, bad_otlp_variants,
                      TUPLES, cli_error_matrix, clone, deep_chain, format_interplay, group_corpus as corpus, run, summary,
                      tuple_capture, unpaired, write_json)

COMPARISON_KEYS = ("comparisonLimit", "comparisonLimitNotice", "comparisonSummary")


def capped(a, b, n=2, opts=None, policy=None):
    return compare(a, b, policy or {}, {**(opts or {}), "comparisonLimit": n})


def check_view(full, limited, n):
    assert limited["comparisonLimit"] == n
    assert re.search(r"paired.*insufficient.samples", limited["comparisonLimitNotice"], re.I)
    assert re.search(r"not a memory.*overall.output bound", limited["comparisonLimitNotice"], re.I)
    assert limited["comparisons"] == full["comparisons"][:n]
    assert limited["comparisonSummary"] == summary(len(full["comparisons"]), n)
    restored = clone(limited)
    restored["comparisons"] = full["comparisons"]
    for key in COMPARISON_KEYS:
        del restored[key]
    assert restored == full


def test_200_paired_rows_including_insufficient_samples_unpaired_groups_and_span_duplicates_excluded_from_total():
    a, b = unpaired(corpus(200), "removed"), unpaired(corpus(200, format="jaeger"), "added")
    policy = {"minSamples": 3}
    full, limited = compare(a, b, policy), capped(a, b, 2, {}, policy)
    check_view(full, limited, 2)
    assert limited["comparisonSummary"] == summary(200, 2)
    assert limited["gate"]["insufficientSampleGroups"] == 67
    assert sum(1 for c in full["comparisons"] if c["eligible"]) == 133
    assert len(limited["baseline"]["groups"]) == 201 and len(limited["candidate"]["groups"]) == 201
    assert len(normalize(a)["groups"]) == 201 and limited["baseline"]["duplicateSpanCount"] == 1
    assert len(limited["diff"]["operations"]["added"]) == 1 and len(limited["diff"]["operations"]["removed"]) == 1
    assert "structuralWitnesses" not in limited
    text = render_text(limited)
    assert sum(1 for line in text.split("\n") if re.match(r"(REGRESSION|within-budget|insufficient-samples) ", line)) == 2
    assert "comparisons: listed=2 total=200 omitted=198 truncated=true" in text


def test_zero_below_exact_over_paired_boundaries_with_without_new_removed_groups_and_maximum_safe_limit():
    for count in (0, 1, 2, 3, 200):
        for extras in (False, True):
            a, b = corpus(max(count, 1)), corpus(max(count, 1), format="jaeger")
            if not count:
                for s in b["data"][0]["spans"]:
                    s["operationName"] = "other"
            if extras:
                a, b = unpaired(a, "removed"), unpaired(b, "added")
            for n in (1, 2, 3, MAX_SAFE):
                f, limited = compare(a, b), capped(a, b, n)
                check_view(f, limited, n)
                assert limited["comparisonSummary"] == summary(count, n)


def test_all_service_operation_kind_dimensions_and_canonical_group_key_ordering_independent_of_counts_input_order():
    a = tuple_capture()
    full, limited = compare(a, a), capped(a, a, 2)
    check_view(full, limited, 2)
    assert [group_key(c["group"]) for c in full["comparisons"]] == js_sorted(
        stringify([s, o, "client" if k == 3 else "server"]) for s, o, k in TUPLES)
    assert [c["group"] for c in limited["comparisons"]] == [{"service": "a", "operation": "b|c", "kind": "client"},
                                                            {"service": "a", "operation": "b|c", "kind": "server"}]
    assert [g["group"] for g in full["baseline"]["groups"]] != [c["group"] for c in full["comparisons"]]
    rev = clone(a)
    rev["resourceSpans"].reverse()
    for r in rev["resourceSpans"]:
        r["scopeSpans"][0]["spans"].reverse()
    assert limited == capped(rev, rev, 2)


def test_all_insufficient_rows_count_toward_summary_regardless_of_latency_eligibility():
    a, b = corpus(200), corpus(200, format="jaeger", offsetMs=200)
    f, limited = compare(a, b), capped(a, b)
    check_view(f, limited, 2)
    assert limited["gate"]["insufficientSampleGroups"] == 200 and limited["gate"]["latencyRegressions"] == 0
    assert limited["comparisonSummary"]["total"] == 200 and limited["gate"]["failed"] is False


CAPS = [{"witnessSourceLimit": 1}, {"witnessUnresolvedLimit": 2}, {"witnessParentLimit": 1}, {"missingParentLimit": 1},
        {"upstreamWarningLimit": 2}, {"groupLimit": 1}, {"edgeLimit": 2}]


def test_every_independent_cap_and_selected_path_preserves_full_observations_statistics_diagnostics_and_witnesses():
    a = unpaired(corpus(200, format="jaeger"), "removed")
    b = unpaired(corpus(201, format="jaeger"), "added")
    policy = {"minSamples": 3, "countGate": {"maxChangePercent": 0, "minSamples": 3},
              "errorFractionGate": {"maxIncreasePercent": 0, "minSamples": 3}}
    combined = {k: v for cap in CAPS for k, v in cap.items()}
    for selection in ({}, {"witnessPath": QUERY_PATH}):
        for cap in ({}, *CAPS, combined):
            opts = {"witnesses": True, **selection, **cap}
            f, limited = compare(a, b, policy, opts), capped(a, b, 2, opts, policy)
            check_view(f, limited, 2)
            assert limited["comparisonSummary"]["total"] == 200
            assert len(limited["observationGates"]["count"]) == 203
            assert len(limited["observationGates"]["errorFraction"]) == 203
            query = next(s for s in limited["structuralWitnesses"]["subtrees"] if s["key"]["group"]["operation"] == "query")
            assert query["pairing"] == "ambiguous"
            d = f["baseline"]["diagnostics"]
            assert (d.get("missingParentSummary") or {}).get("total") or len(d["missingParents"])


@pytest.mark.parametrize("kind", ["latency", "count", "error", "structure"])
def test_gate_remains_complete_outside_retained_rows_and_selected_subtree(kind):
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
              "countGate": {"maxChangePercent": 0, "maxChangeCount": 0, "minSamples": 3},
              "errorFractionGate": {"maxIncreasePercent": 0, "maxIncreasePercentagePoints": 0, "minSamples": 3}}
    opts = {"witnesses": True, "witnessPath": QUERY_PATH}
    f, limited = compare(a, b, policy, opts), capped(a, b, 2, opts, policy)
    check_view(f, limited, 2)
    assert limited["gate"]["failed"] is True
    assert all(not c["regression"] for c in limited["comparisons"])
    if kind == "latency":
        assert limited["gate"]["latencyRegressions"] == 1 and f["comparisons"][-1]["regression"] is True
    if kind == "count":
        assert limited["gate"]["countGateFailures"] == 1
    if kind == "error":
        assert limited["gate"]["errorFractionGateFailures"] == 1
    if kind == "structure":
        assert limited["gate"]["structuralChanges"] > 0 and limited["comparisonSummary"]["total"] == 199


def test_omission_keeps_complete_shape_input_graph_path_budget_errors_cannot_be_hidden_by_cap():
    a = corpus(200)
    full = compare(a, a)
    for key in COMPARISON_KEYS:
        assert key not in full
    assert len(full["comparisons"]) == 200
    assert not re.search(r"comparison limit|comparisons: listed=", render_text(full), re.I)
    deep = deep_chain(500)
    with pytest.raises(InputError, match="witness path budget exceeded"):
        capped(deep, deep, 1, {"witnesses": True})
    with pytest.raises(InputError, match="path not found"):
        capped(a, a, 1, {"witnesses": True, "witnessPath": [{"service": "absent", "operation": "absent",
                                                              "kind": "server"}]})
    for empty in ({"resourceSpans": []}, {"data": []}):
        for x, y in ((empty, a), (a, empty), (empty, empty)):
            with pytest.raises(InputError, match="capture contains no spans"):
                capped(x, y)
    for bad in bad_otlp_variants(lambda: corpus(200)):
        with pytest.raises(InputError):
            capped(a, bad, 1, {"witnesses": True, "witnessPath": QUERY_PATH})
    with pytest.raises(InputError, match="minSamples"):
        capped(a, a, 1, {}, {"minSamples": 0})


@pytest.mark.parametrize("value", INVALID_API_VALUES, ids=repr)
def test_invalid_api_comparison_cap_rejects(value):
    with pytest.raises(InputError, match="comparisonLimit"):
        compare(corpus(1), corpus(1), {}, {"comparisonLimit": value})


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
                      "--upstream-warning-limit", "2", "--group-limit", "1", "--edge-limit", "1"]):
        for fmt in ("json", "text"):
            for failed in (False, True):
                r = cli(["--comparison-limit", "2", *opts, *(["--config", policy] if failed else []), "--format", fmt])
                assert r.status == (1 if failed else 0) and r.stderr == ""
                if fmt == "json":
                    assert json.loads(r.stdout)["comparisonSummary"] == summary(200, 2)
                else:
                    assert "comparisons: listed=2 total=200 omitted=198 truncated=true" in r.stdout
    for n in ("1", "2.0", "2e0", str(MAX_SAFE)):
        assert cli(["--comparison-limit", n]).status == 0
    cli_error_matrix(cli, "--comparison-limit", bad_cap_args("--comparison-limit", BAD_CLI_VALUES))
    format_interplay(cli, "--comparison-limit")
    assert "--comparison-limit" in run(["--help"]).stdout
