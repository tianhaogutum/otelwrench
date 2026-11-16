# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest

from otelwrench.tracediff import InputError, compare, render_text
from otelwrench.tracediff.normalize import sorted_canonical

from .support import (INVALID_API_VALUES, MAX_SAFE, QUERY_PATH, bad_cap_args, cli_error_matrix, clone, deep_chain,
                      otlp, otlp_span, parent_corpus as corpus, run, summary, write_json)

OPTS = {"witnesses": True, "witnessPath": QUERY_PATH}
REASONS_1_1_1 = {"missing-parent": 1, "multiple-parents": 1, "unresolved-ancestor": 1}


def without_witnesses(report):
    return {k: v for k, v in report.items() if k != "structuralWitnesses"}


def check_view(full, capped, n):
    f, c = full["structuralWitnesses"], capped["structuralWitnesses"]
    assert without_witnesses(capped) == without_witnesses(full)
    assert c["parentLimit"] == n
    assert re.search(r"listed unresolved", c["parentLimitNotice"], re.I)
    assert re.search(r"not a memory.*overall.output bound", c["parentLimitNotice"], re.I)
    assert c["subtrees"] == f["subtrees"] and c["edgeChanges"] == f["edgeChanges"]
    assert c.get("selection") == f.get("selection") and c.get("unresolvedSummary") == f.get("unresolvedSummary")
    for side in ("baseline", "candidate"):
        assert len(c["unresolved"][side]) == len(f["unresolved"][side])
        for u, original in zip(c["unresolved"][side], f["unresolved"][side]):
            assert u == {**original, "parents": original["parents"][:n],
                         "parentSummary": summary(len(original["parents"]), n)}


def run_compare(a, b, n, extra=None, policy=None):
    return compare(a, b, policy or {}, {**OPTS, **(extra or {}), "witnessParentLimit": n})


def test_200_parents_cap_each_listed_unresolved_entry_on_both_sides_after_full_ordering_classification_and_dedup():
    a, b = corpus(2, duplicate=True), corpus(200, duplicate=True)
    extra = {"witnessUnresolvedLimit": 2}
    full, limited = compare(a, b, {}, {**OPTS, **extra}), run_compare(a, b, 2, extra)
    check_view(full, limited, 2)
    for side in ("baseline", "candidate"):
        u = next(u for u in limited["structuralWitnesses"]["unresolved"][side] if u["reason"] == "multiple-parents")
        assert u["parents"] == sorted_canonical(u["parents"]) and len(u["parents"]) == 2
        assert limited["structuralWitnesses"]["unresolvedSummary"][side]["reasonCounts"] == REASONS_1_1_1
        assert limited[side]["duplicateSpanCount"] == 1
    multi = next(u for u in limited["structuralWitnesses"]["unresolved"]["candidate"] if u["reason"] == "multiple-parents")
    assert multi["parentSummary"] == summary(200, 2)
    assert limited["structuralWitnesses"]["subtrees"][0]["pairing"] == "ambiguous"
    assert limited == run_compare(a, b, 2, extra)
    text = render_text(limited)
    assert "Witness parent limit: 2" in text
    assert 'parentSummary={"total":200,"listed":2,"omitted":198,"truncated":true}' in text


def test_independent_parent_boundaries_empty_captures_of_unresolved_entries_all_three_reasons_and_no_selector():
    for a, b in ((0, 200), (200, 0), (0, 0), (2, 3), (3, 2), (1, 2)):
        for n in (1, 2, 3, MAX_SAFE):
            full = compare(corpus(a), corpus(b), {}, {"witnesses": True})
            limited = compare(corpus(a), corpus(b), {}, {"witnesses": True, "witnessParentLimit": n})
            check_view(full, limited, n)
    a = corpus(200)
    full, limited = compare(a, a, {}, OPTS), run_compare(a, a, 1)
    check_view(full, limited, 1)
    assert sorted(u["reason"] for u in limited["structuralWitnesses"]["unresolved"]["baseline"]) == [
        "missing-parent", "multiple-parents", "unresolved-ancestor"]
    cross = next(u for u in run_compare(corpus(2), corpus(2), 2)["structuralWitnesses"]["unresolved"]["baseline"]
                 if u["reason"] == "multiple-parents")
    assert cross["parents"][0]["spanId"] == cross["parents"][1]["spanId"]
    assert cross["parents"][0]["traceId"] != cross["parents"][1]["traceId"]


def test_parent_reference_order_is_canonical_and_multiple_parent_precedence_survives_an_absent_reference_at_cap1():
    a = corpus(200)
    b = clone(a)
    multi = next(s for s in b["data"][0]["spans"] if s["spanID"].endswith("0064"))
    multi["references"].reverse()
    assert run_compare(a, a, 2) == run_compare(b, b, 2)
    multi["references"][0]["spanID"] = "000000000000ffff"
    full = compare(b, b, {}, OPTS)
    limited = run_compare(b, b, 1, {"witnessUnresolvedLimit": 2})
    check_view(compare(b, b, {}, {**OPTS, "witnessUnresolvedLimit": 2}), limited, 1)
    assert next(u for u in full["structuralWitnesses"]["unresolved"]["baseline"]
                if u["span"]["spanId"] == multi["spanID"])["reason"] == "multiple-parents"
    assert limited["structuralWitnesses"]["unresolvedSummary"]["baseline"]["reasonCounts"] == REASONS_1_1_1


def test_otlp_missing_and_ancestor_parents_get_complete_per_entry_summaries():
    a = otlp([otlp_span(), otlp_span(span=2, parent=999), otlp_span(span=3, parent=2)])
    full = compare(a, a, {}, {"witnesses": True})
    limited = compare(a, a, {}, {"witnesses": True, "witnessParentLimit": 2})
    check_view(full, limited, 2)
    assert [u["parentSummary"] for u in limited["structuralWitnesses"]["unresolved"]["baseline"]] == [
        summary(1, 2), summary(1, 2)]


def test_source_unresolved_entry_and_parent_caps_remain_independent_notices_accurately_describe_combined_scope():
    a, b = corpus(2), corpus(200)
    for extra in ({}, {"witnessSourceLimit": 1}, {"witnessUnresolvedLimit": 2},
                  {"witnessSourceLimit": 1, "witnessUnresolvedLimit": 2}):
        full, limited = compare(a, b, {}, {**OPTS, **extra}), run_compare(a, b, 1, extra)
        check_view(full, limited, 1)
        c = limited["structuralWitnesses"]
        if extra.get("witnessSourceLimit"):
            assert not re.search(r"unresolved.*uncapped", c["sourceLimitNotice"], re.I)
        if extra.get("witnessUnresolvedLimit"):
            assert "retains its complete parent list" not in c["unresolvedLimitNotice"]
    c = run_compare(a, b, 1, {"witnessSourceLimit": 1, "witnessUnresolvedLimit": 2})["structuralWitnesses"]
    edge = next(e for e in c["edgeChanges"] if e["parent"]["operation"] == "parent")
    assert edge["candidateCount"] == 200 and edge["sourceSummary"]["candidate"]["total"] == 200
    assert c["unresolvedSummary"]["candidate"]["total"] == 3


GATE_POLICIES = [
    ("latency", {"minSamples": 1, "absoluteP95Ms": 0, "relativeP95Percent": 0}),
    ("count", {"countGate": {"maxChangePercent": 0, "maxChangeCount": 0, "minSamples": 1}}),
    ("error", {"errorFractionGate": {"maxIncreasePercent": 0, "maxIncreasePercentagePoints": 0, "minSamples": 1}}),
    ("structure", {"failOnStructure": True}),
]


@pytest.mark.parametrize("name,policy", GATE_POLICIES, ids=[g[0] for g in GATE_POLICIES])
def test_gate_still_fails_using_full_corpus(name, policy):
    a, b = corpus(0 if name == "structure" else 2), corpus(200, ms=200, error=True)
    full, limited = compare(a, b, policy, OPTS), run_compare(a, b, 1, {}, policy)
    check_view(full, limited, 1)
    assert limited["gate"]["failed"] is True


@pytest.mark.parametrize("kind,message", [("duplicate", "duplicate CHILD_OF"), ("conflict", "conflicting duplicate"),
                                          ("cycle", "cyclic parent graph")])
def test_omission_preserves_complete_lists_shape_cap_does_not_relieve_budgets_conflicts_duplicate_refs_or_cycles(
        kind, message):
    a = corpus(200)
    full = compare(a, a, {}, OPTS)
    multi = next(u for u in full["structuralWitnesses"]["unresolved"]["baseline"] if u["reason"] == "multiple-parents")
    assert len(multi["parents"]) == 200
    assert "parentLimit" not in full["structuralWitnesses"]
    assert all("parentSummary" not in u for u in full["structuralWitnesses"]["unresolved"]["baseline"])
    assert not re.search(r"parentSummary|Witness parent limit", render_text(full))
    deep = deep_chain(500)
    with pytest.raises(InputError, match="witness path budget exceeded"):
        compare(deep, deep, {}, {"witnesses": True, "witnessParentLimit": 1})
    bad = corpus(2)
    spans = bad["data"][0]["spans"]
    m = next(s for s in spans if s["spanID"].endswith("0064"))
    if kind == "duplicate":
        m["references"].append(clone(m["references"][0]))
    if kind == "conflict":
        spans.append({**m, "operationName": "conflict"})
    if kind == "cycle":
        m["references"][0] = {"refType": "CHILD_OF", "traceID": m["traceID"], "spanID": "0000000000000066"}
    with pytest.raises(InputError, match=message):
        run_compare(a, bad, 1)


@pytest.mark.parametrize("value", INVALID_API_VALUES, ids=repr)
def test_invalid_api_parent_cap_rejects(value):
    with pytest.raises(InputError, match="witnessParentLimit"):
        run_compare(corpus(2), corpus(2), value)


def test_api_cap_requires_witnesses():
    for options in ({}, {"witnesses": False}):
        with pytest.raises(InputError, match=r"witnessParentLimit.*requires witnesses"):
            compare(corpus(2), corpus(2), {}, {**options, "witnessParentLimit": 2})


def test_cli_json_text_witnesses_option_ordering_and_exits_0_1_2_invalid_missing_duplicate_before_or_after_format(
        tmp_path):
    a, b, path, policy = (tmp_path / n for n in ("a.json", "b.json", "path.json", "policy.json"))
    write_json(a, corpus(2))
    write_json(b, corpus(200))
    write_json(path, QUERY_PATH)
    policy.write_text('{"countGate":{"maxChangePercent":0,"minSamples":1}}')

    def cli(args):
        return run([a, b, *args])

    args = ["--witnesses", "--witness-path", path, "--witness-unresolved-limit", "2", "--witness-parent-limit", "2"]
    ok = cli([*args, "--format", "json"])
    assert ok.status == 0 and ok.stderr == ""
    multi = next(u for u in json.loads(ok.stdout)["structuralWitnesses"]["unresolved"]["candidate"]
                 if u["reason"] == "multiple-parents")
    assert multi["parentSummary"] == summary(200, 2)
    failed = cli([*args, "--config", policy, "--format", "json"])
    assert failed.status == 1 and json.loads(failed.stdout)["gate"]["failed"] is True
    text = cli(args)
    assert text.status == 0
    assert 'parentSummary={"total":200,"listed":2,"omitted":198,"truncated":true}' in text.stdout
    for n in ("1", "2.0", "2e0", str(MAX_SAFE)):
        assert cli(["--witness-parent-limit", n, "--witnesses"]).status == 0
    values = ["0", "-1", "1.5", "null", "true", '"2"', "[]", "{}", "NaN", "Infinity", "1e999", "9007199254740992", "wat"]
    bad = bad_cap_args("--witness-parent-limit", values, prefix=["--witnesses"]) + [["--witness-parent-limit", "2"]]
    cli_error_matrix(cli, "--witness-parent-limit", bad)
    assert "--witness-parent-limit" in run(["--help"]).stdout
