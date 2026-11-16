# SPDX-License-Identifier: Apache-2.0
import json
import re

import pytest

from otelwrench.tracediff import InputError, compare, render_text

from .support import (INVALID_API_VALUES, MAX_SAFE, clone, deep_chain, hex_, jaeger, jaeger_span, otlp, otlp_span, run,
                      summary as stats, write_json)


def group(operation):
    return {"service": "shop", "operation": operation, "kind": "server"}


PATH = [group("checkout"), group("query")]
OPTIONS = {"witnesses": True, "witnessPath": PATH}


def corpus(n):
    return otlp([otlp_span(), *(otlp_span(span=i + 2, parent=1, name="query") for i in range(n))])


def view(a, b, limit, extra=None, policy=None):
    return compare(a, b, policy or {}, {**OPTIONS, "witnessSourceLimit": limit, **(extra or {})})


def check_lists(capped, full, limit):
    for side in ("baseline", "candidate"):
        assert capped[side] == full[side][:limit]
        assert capped["sourceSummary"][side] == stats(len(full[side]), limit)


def without_witnesses(report):
    return {k: v for k, v in report.items() if k != "structuralWitnesses"}


def test_selected_1_to_200_candidates_cap_references_and_aggregate_relations_without_changing_corpus_gates_counts():
    a, b = corpus(1), corpus(200)
    policy = {"countGate": {"maxChangePercent": 0, "minSamples": 1}}
    full, capped = compare(a, b, policy, OPTIONS), view(a, b, 2, {}, policy)
    f, c = full["structuralWitnesses"], capped["structuralWitnesses"]
    assert without_witnesses(capped) == without_witnesses(full) and capped["gate"]["failed"] is True
    assert c["sourceLimit"] == 2 and re.search(r"unresolved.*whole-corpus.*uncapped", c["sourceLimitNotice"], re.I)
    assert c["selection"] == {"path": PATH, "baselineMatches": 1, "candidateMatches": 200}
    query = c["subtrees"][0]
    check_lists(query, f["subtrees"][0], 2)
    assert query["pairing"] == "ambiguous" and query["presence"] == "both"
    assert query["ambiguityReasons"] == ["duplicate-path"]
    assert [[e["baselineCount"], e["candidateCount"]] for e in c["edgeChanges"]] == [[1, 200]]
    check_lists(c["edgeChanges"][0], f["edgeChanges"][0], 2)
    assert [r["spanId"] for r in query["candidate"]] == [hex_(101), hex_(102)]
    text = render_text(capped)
    assert "Witness source limit: 2" in text
    assert "candidate sources: listed=2 total=200 omitted=198 truncated=true" in text
    assert "1 -> 200" in text and "witness ambiguous" in text


def test_cap_applies_independently_to_both_subtree_sides_and_path_aggregate_relations_at_exact_and_over_boundaries():
    a, b, root = corpus(2), corpus(3), [group("checkout")]
    full = compare(a, b, {}, {**OPTIONS, "witnessPath": root})
    capped = view(a, b, 2, {"witnessPath": root})
    f, c = full["structuralWitnesses"], capped["structuralWitnesses"]
    for i, subtree in enumerate(c["subtrees"]):
        check_lists(subtree, f["subtrees"][i], 2)
        for j, edge in enumerate(subtree["edgeChanges"]):
            check_lists(edge, f["subtrees"][i]["edgeChanges"][j], 2)
    check_lists(c["edgeChanges"][0], f["edgeChanges"][0], 2)
    assert c["edgeChanges"][0]["sourceSummary"]["baseline"] == stats(2, 2)
    assert c["edgeChanges"][0]["sourceSummary"]["candidate"] == stats(3, 2)
    assert "baseline sources: listed=2 total=2 omitted=0 truncated=false" in render_text(capped)


def test_omission_preserves_uncapped_json_text_shape_and_cap_supports_witnesses_without_selection():
    a, b = corpus(1), corpus(3)
    full, capped = compare(a, b, {}, OPTIONS), view(a, b, 2)
    expected = clone(capped)
    del expected["structuralWitnesses"]["sourceLimit"]
    del expected["structuralWitnesses"]["sourceLimitNotice"]
    for i, row in enumerate(expected["structuralWitnesses"]["subtrees"]):
        del row["sourceSummary"]
        row["baseline"] = full["structuralWitnesses"]["subtrees"][i]["baseline"]
        row["candidate"] = full["structuralWitnesses"]["subtrees"][i]["candidate"]
    expected["structuralWitnesses"]["edgeChanges"] = full["structuralWitnesses"]["edgeChanges"]
    assert expected == full
    assert not re.search(r"source limit|sources: listed=|truncated=", render_text(full), re.I)
    unselected = compare(a, b, {}, {"witnesses": True, "witnessSourceLimit": 2})
    assert len(unselected["structuralWitnesses"]["subtrees"]) == 2
    assert "selection" not in unselected["structuralWitnesses"]
    assert unselected["structuralWitnesses"]["subtrees"][0]["edgeChanges"][0]["sourceSummary"]["candidate"]["total"] == 3


def test_one_sided_paths_keep_full_presence_counts_and_explicit_zero_side_summaries():
    for a, b, side in ((corpus(0), corpus(3), "candidate"), (corpus(3), corpus(0), "baseline")):
        c = view(a, b, 2)["structuralWitnesses"]
        row = c["subtrees"][0]
        assert row["presence"] == f"{side}-only" and row["pairing"] == "ambiguous"
        assert row["sourceSummary"][side] == stats(3, 2)
        assert row["sourceSummary"]["candidate" if side == "baseline" else "baseline"] == stats(0, 2)
        assert c["selection"][f"{side}Matches"] == 3
        assert c["edgeChanges"][0]["sourceSummary"] == row["sourceSummary"]
    unique = view(corpus(0), corpus(1), 2)["structuralWitnesses"]["subtrees"][0]
    assert unique["pairing"] == "unpaired"


def test_deduplication_and_repeated_ancestor_ambiguity_are_decided_before_cap_across_formats():
    a = corpus(3)
    spans = a["resourceSpans"][0]["scopeSpans"][0]["spans"]
    spans.extend([clone(spans[1]), otlp_span(span=10, parent=2, name="leaf")])
    b = jaeger([jaeger_span(span=20), *(jaeger_span(span=s, parent=20, name="query") for s in (23, 22, 21)),
                jaeger_span(span=30, parent=21, name="leaf")])
    r = view(a, b, 1)
    c = r["structuralWitnesses"]
    assert r["baseline"]["duplicateSpanCount"] == 1
    assert c["subtrees"][0]["sourceSummary"]["baseline"]["total"] == 3
    assert len(c["subtrees"][0]["baseline"]) == 1
    assert c["subtrees"][0]["pairing"] == "ambiguous" and c["subtrees"][1]["pairing"] == "ambiguous"
    assert c["subtrees"][1]["ambiguityReasons"] == ["ambiguous-parent-path"]
    assert c["subtrees"][1]["sourceSummary"]["candidate"] == stats(1, 1)


def test_ordering_is_the_deterministic_prefix_of_full_json_lexical_source_order_not_input_traversal_order():
    a, b = corpus(1), corpus(20)
    b["resourceSpans"][0]["scopeSpans"][0]["spans"].reverse()
    full = compare(a, b, {}, OPTIONS)["structuralWitnesses"]
    first, again = view(a, b, 2)["structuralWitnesses"], view(a, b, 2)["structuralWitnesses"]
    assert first == again
    check_lists(first["subtrees"][0], full["subtrees"][0], 2)
    check_lists(first["edgeChanges"][0], full["edgeChanges"][0], 2)
    assert [r["spanId"] for r in first["subtrees"][0]["candidate"]] == [hex_(21), hex_(11)]
    # Source paths correctly point into the reordered input.
    for r in first["subtrees"][0]["candidate"]:
        index = int(re.search(r"spans\[(\d+)\]$", r["sourcePath"]).group(1))
        assert b["resourceSpans"][0]["scopeSpans"][0]["spans"][index]["spanId"] == r["spanId"]


def test_whole_corpus_unresolved_entries_remain_uncapped_their_present_relations_are_capped():
    a, b = corpus(1), corpus(1)
    join = otlp_span(span=50, parent=1, name="missing")
    join["parentSpanId"] = hex_(999)
    b["resourceSpans"][0]["scopeSpans"][0]["spans"].extend(
        [join, *(otlp_span(span=s, parent=50, name="unresolved-child") for s in (51, 52, 53))])
    full, c = compare(a, b, {}, OPTIONS)["structuralWitnesses"], view(a, b, 2)["structuralWitnesses"]
    assert len(c["unresolved"]["candidate"]) == 4 and c["unresolved"] == full["unresolved"]
    edge = next(e for e in c["edgeChanges"] if e["child"]["operation"] == "unresolved-child")
    assert edge["candidateCount"] == 3 and edge["sourceSummary"]["candidate"] == stats(3, 2)
    assert re.search(r"unresolved.*whole-corpus.*uncapped", render_text(view(a, b, 2)), re.I)


def test_cap_does_not_bypass_full_selected_path_budgets_or_input_validation_outside_selection():
    deep = deep_chain(500)
    with pytest.raises(InputError, match="witness path budget exceeded"):
        compare(deep, deep, {}, {"witnesses": True, "witnessSourceLimit": 1})
    invalid = corpus(1)
    invalid["resourceSpans"][0]["scopeSpans"][0]["spans"].append(otlp_span(span=10, parent=10, name="outside"))
    with pytest.raises(InputError, match="cyclic"):
        view(corpus(1), invalid, 1)


@pytest.mark.parametrize("value", INVALID_API_VALUES, ids=repr)
def test_invalid_api_witness_source_limit_rejects(value):
    with pytest.raises(InputError, match="witnessSourceLimit"):
        view(corpus(1), corpus(1), value)


def test_valid_caps_include_1_and_max_safe_integer_witnesses_are_required():
    for n in (1, MAX_SAFE):
        assert view(corpus(1), corpus(3), n)["structuralWitnesses"]["subtrees"][0]["sourceSummary"]["candidate"] == \
            stats(3, n)
    for opt in ({}, {"witnesses": False}):
        with pytest.raises(InputError, match=r"witnessSourceLimit.*requires witnesses"):
            compare(corpus(1), corpus(1), {}, {**opt, "witnessSourceLimit": 2})


def test_cli_caps_selected_json_text_preserves_full_gate_exits_and_rejects_invalid_missing_duplicate_values(tmp_path):
    a, b, selector, policy = (tmp_path / n for n in ("a.json", "b.json", "path.json", "policy.json"))
    write_json(a, corpus(1))
    write_json(b, corpus(200))
    write_json(selector, PATH)
    write_json(policy, {"countGate": {"maxChangePercent": 0, "minSamples": 1}})

    def cli(args):
        return run([a, b, *args])

    args = ["--witnesses", "--witness-path", selector, "--witness-source-limit", "2"]
    ok = cli(["--format", "json", *args])
    assert ok.status == 0 and ok.stderr == ""
    assert json.loads(ok.stdout)["structuralWitnesses"]["subtrees"][0]["sourceSummary"]["candidate"] == stats(200, 2)
    fail = cli(["--format", "json", *args, "--config", policy])
    assert fail.status == 1 and json.loads(fail.stdout)["gate"]["failed"] is True
    text = cli(args)
    assert text.status == 0 and "listed=2 total=200 omitted=198 truncated=true" in text.stdout
    for n in ("1", "2.0", "2e0", str(MAX_SAFE)):
        assert cli(["--witnesses", "--witness-source-limit", n]).status == 0
    values = ["null", "0", "-1", "1.5", "NaN", "Infinity", "-Infinity", "1e999", "9007199254740992", "true", '"2"',
              "0x2", " ", ""]
    for bad in [*(["--witnesses", "--witness-source-limit", n] for n in values),
                ["--witnesses", "--witness-source-limit"], ["--witness-source-limit", "2"],
                [*args, "--witness-source-limit", "2"]]:
        r = cli(["--format", "json", *bad])
        assert r.status == 2 and r.stdout == "", bad
        error = json.loads(r.stderr)["error"]
        assert error["code"] == "INPUT_ERROR"
        assert re.search(r"witness.source.limit|witnessSourceLimit", error["message"], re.I)
    help_text = run(["--help"])
    assert help_text.status == 0 and "--witness-source-limit" in help_text.stdout
