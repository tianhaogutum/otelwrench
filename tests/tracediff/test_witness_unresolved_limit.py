# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest

from otelwrench.tracediff import InputError, compare, render_text
from otelwrench.tracediff.normalize import sorted_canonical

from .support import (INVALID_API_VALUES, MAX_SAFE, PROG, QUERY_PATH, deep_chain, otlp, otlp_span, run,
                      unresolved_corpus as corpus, write_json)

OPTIONS = {"witnesses": True, "witnessPath": QUERY_PATH}


def counts(missing=0, multiple=0, ancestor=0):
    return {"missing-parent": missing, "multiple-parents": multiple, "unresolved-ancestor": ancestor}


def summary(total, limit, reason_counts):
    return {"total": total, "listed": min(total, limit), "omitted": max(total - limit, 0), "truncated": total > limit,
            "reasonCounts": reason_counts}


def capped(a, b, limit=2, extra=None, policy=None):
    return compare(a, b, policy or {}, {**OPTIONS, "witnessUnresolvedLimit": limit, **(extra or {})})


def without_witnesses(report):
    return {k: v for k, v in report.items() if k != "structuralWitnesses"}


def check_view(full, limited, limit):
    f, c = full["structuralWitnesses"], limited["structuralWitnesses"]
    assert without_witnesses(limited) == without_witnesses(full)
    assert c["subtrees"] == f["subtrees"] and c["edgeChanges"] == f["edgeChanges"]
    assert c.get("selection") == f.get("selection")
    assert c["unresolvedLimit"] == limit
    assert re.search(r"whole.corpus", c["unresolvedLimitNotice"], re.I)
    assert re.search(r"parent", c["unresolvedLimitNotice"], re.I)
    assert re.search(r"not a memory.*overall.output bound", c["unresolvedLimitNotice"], re.I)
    for side in ("baseline", "candidate"):
        assert c["unresolved"][side] == f["unresolved"][side][:limit]
        reasons = counts()
        for u in f["unresolved"][side]:
            reasons[u["reason"]] += 1
        assert c["unresolvedSummary"][side] == summary(len(f["unresolved"][side]), limit, reasons)


def test_200_outside_selection_unresolved_entries_cap_each_side_with_exact_full_corpus_reason_totals_and_deduplication():
    a, b = corpus(200, duplicate=True), corpus(201, duplicate=True)
    full, limited = compare(a, b, {}, OPTIONS), capped(a, b)
    check_view(full, limited, 2)
    c = limited["structuralWitnesses"]
    assert c["unresolvedSummary"]["baseline"] == summary(200, 2, counts(67, 67, 66))
    assert c["unresolvedSummary"]["candidate"] == summary(201, 2, counts(67, 67, 67))
    assert limited["baseline"]["duplicateSpanCount"] == 1 and limited["candidate"]["duplicateSpanCount"] == 1
    assert c["subtrees"][0]["pairing"] == "ambiguous" and c["subtrees"][1]["pairing"] == "ambiguous"
    assert c["subtrees"][1]["ambiguityReasons"] == ["ambiguous-parent-path"]
    assert c["selection"]["candidateMatches"] == 2
    assert [[e["baselineCount"], e["candidateCount"]] for e in c["edgeChanges"]] == [[66, 67]]
    assert len(c["edgeChanges"][0]["candidate"]) == 67
    assert len(limited["baseline"]["diagnostics"]["missingParents"]) == 67
    text = render_text(limited)
    for side in ("baseline", "candidate"):
        assert sum(1 for line in text.split("\n") if line.startswith(f"witness unresolved {side} ")) == 2
    assert "Witness unresolved limit: 2" in text
    assert ('baseline unresolved: listed=2 total=200 omitted=198 truncated=true reasonCounts={"missing-parent":67,'
            '"multiple-parents":67,"unresolved-ancestor":66}') in text


def test_exact_over_below_and_empty_unresolved_boundaries_work_independently_on_both_sides_without_selection_too():
    for a, b in ((0, 3), (3, 0), (2, 3), (3, 2), (1, 0), (0, 0)):
        full, limited = compare(corpus(a), corpus(b), {}, OPTIONS), capped(corpus(a), corpus(b))
        check_view(full, limited, 2)
        assert limited["structuralWitnesses"]["unresolvedSummary"]["baseline"]["truncated"] is (a > 2)
        assert limited["structuralWitnesses"]["unresolvedSummary"]["candidate"]["truncated"] is (b > 2)
    unselected = compare(corpus(3), corpus(3), {}, {"witnesses": True, "witnessUnresolvedLimit": 2})
    assert "selection" not in unselected["structuralWitnesses"]
    assert unselected["structuralWitnesses"]["unresolvedSummary"]["baseline"] == summary(3, 2, counts(1, 1, 1))


def test_listed_entries_retain_every_deduplicated_sorted_parent_deterministic_order_uses_full_entries_and_first_source_paths():
    a = corpus(30, duplicate=True)
    a["data"][0]["spans"].reverse()
    full, limited = compare(a, a, {}, OPTIONS), capped(a, a, 29)
    check_view(full, limited, 29)
    assert limited == capped(a, a, 29)
    listed = limited["structuralWitnesses"]["unresolved"]["baseline"]
    assert listed == sorted_canonical(full["structuralWitnesses"]["unresolved"]["baseline"])[:29]
    assert listed[0]["span"]["spanId"] != a["data"][0]["spans"][0]["spanID"]
    multiple = [u for u in listed if u["reason"] == "multiple-parents"]
    assert multiple
    for u in multiple:
        assert len(u["parents"]) == 2 and u["parents"] == sorted_canonical(u["parents"])
    cap1 = capped(corpus(2), corpus(2), 1)["structuralWitnesses"]
    assert cap1["unresolved"]["baseline"][0]["reason"] == "multiple-parents"
    assert len(cap1["unresolved"]["baseline"][0]["parents"]) == 2
    assert cap1["unresolvedSummary"]["baseline"]["reasonCounts"] == counts(1, 1, 0)
    for u in listed:
        index = int(re.search(r"spans\[(\d+)\]$", u["span"]["sourcePath"]).group(1))
        assert a["data"][0]["spans"][index]["spanID"] == u["span"]["spanId"]


def test_multiple_parents_take_precedence_over_absent_parents_and_descendants_keep_their_own_unresolved_ancestor_reason():
    a = corpus(3)
    parent = a["data"][0]["spans"][5]
    parent["references"][0]["spanID"] = "000000000000ffff"
    a["data"][0]["spans"][6]["references"][0]["spanID"] = parent["spanID"]
    limited = capped(a, a, 1)["structuralWitnesses"]
    assert limited["unresolvedSummary"]["baseline"]["reasonCounts"] == counts(1, 1, 1)
    full = compare(a, a, {}, OPTIONS)["structuralWitnesses"]["unresolved"]["baseline"]
    assert next(u for u in full if u["span"]["spanId"] == parent["spanID"])["reason"] == "multiple-parents"
    assert next(u for u in full if u["span"]["spanId"] == a["data"][0]["spans"][6]["spanID"])["reason"] == \
        "unresolved-ancestor"


def test_otlp_missing_parents_and_descendants_use_the_same_cap_and_whole_corpus_summaries():
    a = otlp([otlp_span(), otlp_span(span=2, parent=999, name="outside"), otlp_span(span=3, parent=2, name="descendant")])
    full = compare(a, a, {}, {"witnesses": True})
    limited = compare(a, a, {}, {"witnesses": True, "witnessUnresolvedLimit": 1})
    check_view(full, limited, 1)
    assert limited["structuralWitnesses"]["unresolvedSummary"]["baseline"]["reasonCounts"] == counts(1, 0, 1)


def test_unresolved_and_source_caps_are_independent_combined_notices_label_scope_accurately():
    a, b = corpus(3), corpus(200)
    source_only = compare(a, b, {}, {**OPTIONS, "witnessSourceLimit": 1})
    both = capped(a, b, 2, {"witnessSourceLimit": 1})
    assert both["structuralWitnesses"]["subtrees"] == source_only["structuralWitnesses"]["subtrees"]
    assert both["structuralWitnesses"]["edgeChanges"] == source_only["structuralWitnesses"]["edgeChanges"]
    assert len(both["structuralWitnesses"]["unresolved"]["candidate"]) == 2
    assert both["structuralWitnesses"]["unresolvedSummary"]["candidate"]["total"] == 200
    edge = next(e for e in both["structuralWitnesses"]["edgeChanges"] if e["parent"]["operation"] == "unresolved")
    assert edge["sourceSummary"]["candidate"]["total"] == 66
    assert re.search(r"unresolved.*uncapped", source_only["structuralWitnesses"]["sourceLimitNotice"], re.I)
    assert not re.search(r"uncapped", both["structuralWitnesses"]["sourceLimitNotice"], re.I)
    assert not re.search(r"unresolved.*uncapped", render_text(both), re.I)


GATE_POLICIES = [
    ("latency", {"minSamples": 1, "absoluteP95Ms": 0, "relativeP95Percent": 0}),
    ("count", {"countGate": {"maxChangePercent": 0, "minSamples": 1}}),
    ("error", {"errorFractionGate": {"maxIncreasePercent": 0, "minSamples": 1}}),
    ("structure", {"failOnStructure": True}),
]


@pytest.mark.parametrize("name,policy", GATE_POLICIES, ids=[g[0] for g in GATE_POLICIES])
def test_gate_outside_selection_remains_failing_with_complete_aggregate_evidence(name, policy):
    a, b = corpus(0 if name == "structure" else 3), corpus(200, ms=200, error=True)
    full, limited = compare(a, b, policy, OPTIONS), capped(a, b, 2, {}, policy)
    check_view(full, limited, 2)
    assert limited["gate"]["failed"] is True


def test_omission_adds_no_schema_text_fields_cap_cannot_bypass_path_budgets_or_graph_validation():
    a = corpus(3)
    full = compare(a, a, {}, OPTIONS)
    assert len(full["structuralWitnesses"]["unresolved"]["baseline"]) == 3
    assert "unresolvedSummary" not in full["structuralWitnesses"]
    assert "unresolvedLimit" not in full["structuralWitnesses"]
    assert not re.search(r"unresolved limit|unresolved: listed=", render_text(full), re.I)
    deep = deep_chain(500)
    with pytest.raises(InputError, match="witness path budget exceeded"):
        compare(deep, deep, {}, {"witnesses": True, "witnessUnresolvedLimit": 1})
    bad = corpus(3)
    bad["data"][0]["spans"].append({**bad["data"][0]["spans"][4], "operationName": "conflict"})
    with pytest.raises(InputError, match="conflicting duplicate"):
        capped(a, bad)


@pytest.mark.parametrize("value", INVALID_API_VALUES, ids=repr)
def test_invalid_api_unresolved_cap_rejects(value):
    with pytest.raises(InputError, match="witnessUnresolvedLimit"):
        compare(corpus(1), corpus(1), {}, {**OPTIONS, "witnessUnresolvedLimit": value})


def test_api_safe_integer_endpoints_work_and_witnesses_are_required():
    for n in (1, MAX_SAFE):
        check_view(compare(corpus(3), corpus(3), {}, OPTIONS), capped(corpus(3), corpus(3), n), n)
    for options in ({}, {"witnesses": False}):
        with pytest.raises(InputError, match=r"witnessUnresolvedLimit.*requires witnesses"):
            compare(corpus(1), corpus(1), {}, {**options, "witnessUnresolvedLimit": 2})


def test_cli_json_text_and_exits_0_1_2_include_invalid_missing_and_duplicate_values_before_or_after_format(tmp_path):
    a, b, path, policy = (tmp_path / n for n in ("a.json", "b.json", "path.json", "policy.json"))
    write_json(a, corpus(3))
    write_json(b, corpus(200))
    write_json(path, QUERY_PATH)
    policy.write_text('{"countGate":{"maxChangePercent":0,"minSamples":1}}')

    def cli(args):
        return run([a, b, *args])

    args = ["--witnesses", "--witness-path", path, "--witness-unresolved-limit", "2"]
    ok = cli([*args, "--format", "json"])
    assert ok.status == 0 and ok.stderr == ""
    assert json.loads(ok.stdout)["structuralWitnesses"]["unresolvedSummary"]["candidate"] == \
        summary(200, 2, counts(67, 67, 66))
    failed = cli([*args, "--config", policy, "--format", "json"])
    assert failed.status == 1 and json.loads(failed.stdout)["gate"]["failed"] is True
    text = cli(args)
    assert text.status == 0 and "candidate unresolved: listed=2 total=200 omitted=198 truncated=true" in text.stdout
    for n in ("1", "2.0", "2e0", str(MAX_SAFE)):
        assert cli(["--witnesses", "--witness-unresolved-limit", n]).status == 0
    values = ["null", "0", "-1", "1.5", "NaN", "Infinity", "-Infinity", "1e999", "9007199254740992", "true", '"2"',
              "0x2", " ", ""]
    bad_args = [*(["--witnesses", "--witness-unresolved-limit", n] for n in values),
                ["--witnesses", "--witness-unresolved-limit"], ["--witness-unresolved-limit", "2"],
                [*args, "--witness-unresolved-limit", "2"]]
    for bad in bad_args:
        for placement in ("before", "after", "default", "text"):
            fmt = [] if placement == "default" else ["--format", "text" if placement == "text" else "json"]
            r = cli([*fmt, *bad] if placement == "before" else [*bad, *fmt])
            assert r.status == 2 and r.stdout == "", bad
            if placement in ("before", "after"):
                assert json.loads(r.stderr)["error"]["code"] == "INPUT_ERROR"
            else:
                assert r.stderr.startswith(f"{PROG}:")
            assert re.search(r"witness.unresolved.limit", r.stderr, re.I)
    help_text = run(["--help"])
    assert help_text.status == 0 and "--witness-unresolved-limit" in help_text.stdout
