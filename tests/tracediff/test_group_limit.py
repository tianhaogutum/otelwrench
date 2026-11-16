# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest

from otelwrench.tracediff import InputError, compare, normalize, render_text
from otelwrench.tracediff.normalize import group_key, sorted_canonical

from .support import (BAD_CLI_VALUES, INVALID_API_VALUES, MAX_SAFE, QUERY_PATH, bad_cap_args, cli_error_matrix, clone,
                      bad_otlp_variants, deep_chain, format_interplay, group_corpus as corpus, otlp, otlp_span, run, summary, write_json)

GROUP_KEYS = ("groupLimit", "groupLimitNotice", "groupSummary")


def check_view(full, limited, n):
    for side in ("baseline", "candidate"):
        f, c = full[side], limited[side]
        assert c["groupLimit"] == n
        assert re.search(r"group.statistic.*canonical order", c["groupLimitNotice"], re.I)
        assert re.search(r"not a memory.*overall.output bound", c["groupLimitNotice"], re.I)
        assert c["groups"] == f["groups"][:n]
        assert c["groupSummary"] == summary(len(f["groups"]), n)
    restored = clone(limited)
    for side in ("baseline", "candidate"):
        restored[side]["groups"] = full[side]["groups"]
        for key in GROUP_KEYS:
            del restored[side][key]
    assert restored == full


def capped(a, b, n=2, opts=None, policy=None):
    return compare(a, b, policy or {}, {**(opts or {}), "groupLimit": n})


def test_200_distinct_groups_per_side_cap_without_witnesses_exact_deduplicated_statistics():
    a, b = corpus(200), corpus(200, format="jaeger")
    full, limited = compare(a, b), capped(a, b)
    check_view(full, limited, 2)
    for side, data in (("baseline", a), ("candidate", b)):
        assert limited[side]["groupSummary"] == summary(200, 2)
        assert limited[side]["duplicateSpanCount"] == 1 and limited[side]["spanCount"] == 599
        assert limited[side]["inputSpanCount"] == 600 and limited[side]["traceCount"] == 1
        assert len(normalize(data)["groups"]) == 200
        assert next(s for s in full[side]["groups"] if s["group"]["operation"] == "checkout") == {
            "group": QUERY_PATH[0], "count": 2, "errorCount": 1, "errorFraction": 0.5,
            "durationMs": {"min": 10, "median": 10, "p95": 20, "max": 20}}
    assert len(limited["comparisons"]) == 200 and "structuralWitnesses" not in limited
    lines = render_text(limited).split("\n")
    assert sum(1 for line in lines if re.match(r"  \[.* count=", line)) == 4
    assert sum(1 for line in lines if "groups: listed=2 total=200 omitted=198 truncated=true" in line) == 2


def test_both_sides_below_exact_over_boundaries_and_maximum_safe_integer():
    for a, b in ((1, 1), (1, 200), (200, 1), (1, 2), (2, 3), (3, 2)):
        for n in (1, 2, 3, MAX_SAFE):
            x, y = corpus(a), corpus(b, format="jaeger")
            check_view(compare(x, y), capped(x, y, n), n)


def test_empty_otlp_jaeger_captures_stay_invalid_on_either_both_sides():
    for fmt in ("otlp", "jaeger"):
        empty, valid = corpus(0, format=fmt), corpus(1, format=fmt)
        for a, b in ((empty, valid), (valid, empty), (empty, empty)):
            with pytest.raises(InputError, match="capture contains no spans"):
                capped(a, b)


def test_canonical_prefix_sorts_full_statistic_objects_with_service_operation_kind_keys_intact():
    a = corpus(200)
    full, limited = compare(a, a), capped(a, a, 50)
    check_view(full, limited, 50)
    assert limited["baseline"]["groups"] == sorted_canonical(full["baseline"]["groups"])[:50]
    # The original compared with a locale-aware key sort; any group-key order differs from whole-row order.
    by_key = sorted(full["baseline"]["groups"], key=lambda g: group_key(g["group"]).casefold())
    assert limited["baseline"]["groups"] != by_key[:50]
    reordered = clone(a)
    reordered["resourceSpans"].reverse()
    for r in reordered["resourceSpans"]:
        r["scopeSpans"][0]["spans"].reverse()
    assert limited == capped(reordered, reordered, 50)
    same_name = otlp([otlp_span(), otlp_span(span=2, kind=3)])
    same_name["resourceSpans"].extend(otlp([otlp_span(span=3)], "other")["resourceSpans"])
    f = compare(same_name, same_name)
    assert len(f["baseline"]["groups"]) == 3
    check_view(f, capped(same_name, same_name, 2), 2)


CAPS = [{}, {"witnessSourceLimit": 1}, {"witnessUnresolvedLimit": 2}, {"witnessParentLimit": 1},
        {"missingParentLimit": 1}, {"upstreamWarningLimit": 2},
        {"witnessSourceLimit": 1, "witnessUnresolvedLimit": 2, "witnessParentLimit": 1, "missingParentLimit": 1,
         "upstreamWarningLimit": 2}]


def test_all_diagnostic_witness_caps_selected_path_reasons_evidence_and_ambiguity_are_independent():
    a, b = corpus(200, format="jaeger"), corpus(201, format="jaeger")
    for selection in ({}, {"witnessPath": QUERY_PATH}):
        for caps in CAPS:
            opts = {"witnesses": True, **selection, **caps}
            full, limited = compare(a, b, {}, opts), capped(a, b, 2, opts)
            check_view(full, limited, 2)
            query = next(s for s in limited["structuralWitnesses"]["subtrees"] if s["key"]["group"]["operation"] == "query")
            assert query["pairing"] == "ambiguous"


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
    assert len(limited["comparisons"]) == (199 if name == "structure" else 200)
    if name == "count":
        assert len(limited["observationGates"]["count"]) == 200
    if name == "error":
        assert len(limited["observationGates"]["errorFraction"]) == 200


def test_omission_preserves_complete_shape_cap_cannot_bypass_input_path_validation_or_budgets():
    a = corpus(200)
    full = compare(a, a)
    assert len(full["baseline"]["groups"]) == 200
    for key in GROUP_KEYS:
        assert key not in full["baseline"]
    assert not re.search(r"group limit|groups: listed=", render_text(full), re.I)
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
def test_invalid_api_group_cap_rejects(value):
    with pytest.raises(InputError, match="groupLimit"):
        compare(corpus(1), corpus(1), {}, {"groupLimit": value})


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
                      "--upstream-warning-limit", "2"]):
        for fmt in ("json", "text"):
            for failed in (False, True):
                r = cli(["--group-limit", "2", *opts, *(["--config", policy] if failed else []), "--format", fmt])
                assert r.status == (1 if failed else 0) and r.stderr == ""
                if fmt == "json":
                    assert json.loads(r.stdout)["candidate"]["groupSummary"] == summary(200, 2)
                else:
                    assert "groups: listed=2 total=200 omitted=198 truncated=true" in r.stdout
    for n in ("1", "2.0", "2e0", str(MAX_SAFE)):
        assert cli(["--group-limit", n]).status == 0
    cli_error_matrix(cli, "--group-limit", bad_cap_args("--group-limit", BAD_CLI_VALUES))
    format_interplay(cli, "--group-limit")
    assert "--group-limit" in run(["--help"]).stdout
