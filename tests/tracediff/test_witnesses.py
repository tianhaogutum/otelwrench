# SPDX-License-Identifier: Apache-2.0
import re

import pytest

from otelwrench.tracediff import InputError, compare, normalize, render_text

from .support import clone, deep_chain, hex_, jaeger, jaeger_span, otlp, otlp_span

OPTIONS = {"witnesses": True}


def witness(a, b, config=None):
    return compare(a, b, {} if config is None else config, OPTIONS)


def source_span(data, reference):
    value = data
    for token in reference["sourcePath"][2:].split("."):
        key, index = re.fullmatch(r"(\w+)\[(\d+)\]", token).groups()
        value = value[key][int(index)]
    assert value.get("traceId", value.get("traceID")) == reference["traceId"]
    assert value.get("spanId", value.get("spanID")) == reference["spanId"]
    return value


def group(operation):
    return {"service": "shop", "operation": operation, "kind": "server"}


def row(r, operation, ancestors=()):
    return next((s for s in r["structuralWitnesses"]["subtrees"] if s["key"]["group"]["operation"] == operation
                 and [g["operation"] for g in s["key"]["parentPath"]] == list(ancestors)), None)


def simple():
    return otlp([otlp_span(), otlp_span(span=2, parent=1, name="query")])


def repeated():
    return jaeger([jaeger_span(trace=9, span=13, parent=11, name="query"),
                   jaeger_span(trace=9, span=12, parent=11, name="query"), jaeger_span(trace=9, span=11)])


def test_opt_in_source_witnesses_expose_changed_multiplicities_and_never_arbitrarily_pair_duplicates():
    r = witness(simple(), repeated(), {"failOnStructure": True})
    assert r["structuralWitnesses"], "missing structural witness view"
    assert r["gate"]["failed"] is False  # Count changes still do not gate edge presence.
    root, child = row(r, "checkout"), row(r, "query", ["checkout"])
    assert root["key"] == {"parentPath": [], "group": group("checkout")}
    assert root["pairing"] == "unique-structural-candidates"
    assert child["pairing"] == "ambiguous" and child["presence"] == "both"
    assert child["ambiguityReasons"] == ["duplicate-path"]
    assert len(child["baseline"]) == 1 and len(child["candidate"]) == 2
    assert "pairs" not in child
    edge = root["edgeChanges"][0]
    assert edge["baselineCount"] == 1 and edge["candidateCount"] == 2
    assert edge["child"] == group("query")
    assert edge["baseline"][0]["parent"]["spanId"] == hex_(1)
    assert len(edge["candidate"]) == 2
    assert child["baseline"][0]["sourcePath"] == "$.resourceSpans[0].scopeSpans[0].spans[1]"
    assert child["candidate"][0]["traceId"] == hex_(9, 32)
    assert r["structuralWitnesses"]["edgeChanges"][0]["candidateCount"] == r["diff"]["edges"]["countChanged"][0]["candidateCount"]
    text = render_text(r)
    assert "witness ambiguous" in text
    assert re.search(r"no individual causal alignment", text, re.I)
    assert re.search(r"\$\.data\[0\]\.spans", text)


def test_reordered_spans_renamed_ids_and_otlp_jaeger_formats_keep_the_same_structural_keys():
    b = jaeger([jaeger_span(trace=9, span=12, parent=11, name="query"), jaeger_span(trace=9, span=11)])
    r = witness(simple(), b)
    assert [s["key"] for s in r["structuralWitnesses"]["subtrees"]] == [
        {"parentPath": [], "group": group("checkout")},
        {"parentPath": [group("checkout")], "group": group("query")}]
    assert all(s["pairing"] == "unique-structural-candidates" for s in r["structuralWitnesses"]["subtrees"])
    assert r["structuralWitnesses"]["edgeChanges"] == []
    assert all(len(s["edgeChanges"]) == 0 for s in r["structuralWitnesses"]["subtrees"])
    assert r == witness(simple(), b)


def test_ancestor_paths_separate_equal_operations_and_surface_changes_hidden_by_aggregate_edges():
    a = otlp([otlp_span(name="left"), otlp_span(span=2, name="right"), otlp_span(span=3, parent=1, name="worker"),
              otlp_span(span=4, parent=2, name="worker"), otlp_span(span=5, parent=3, name="query")])
    b = clone(a)
    b["resourceSpans"][0]["scopeSpans"][0]["spans"][4]["parentSpanId"] = hex_(4)
    r = witness(a, b)
    assert r["diff"]["edges"] == {"added": [], "removed": [], "countChanged": []}
    assert row(r, "worker", ["left"])["edgeChanges"][0]["candidateCount"] == 0
    assert row(r, "worker", ["right"])["edgeChanges"][0]["baselineCount"] == 0
    assert row(r, "query", ["left", "worker"])["presence"] == "baseline-only"
    assert row(r, "query", ["right", "worker"])["presence"] == "candidate-only"
    assert row(r, "query", ["left", "worker"])["pairing"] == "unpaired"


def test_ambiguity_propagates_from_duplicate_ancestors_even_when_a_descendant_path_is_unique():
    a = otlp([otlp_span(), otlp_span(span=2), otlp_span(span=3, parent=1, name="query")])
    r = witness(a, a)
    assert row(r, "checkout")["pairing"] == "ambiguous"
    assert row(r, "query", ["checkout"])["pairing"] == "ambiguous"
    assert row(r, "query", ["checkout"])["ambiguityReasons"] == ["ambiguous-parent-path"]
    corpus = otlp([otlp_span(), otlp_span(trace=2)])
    assert row(witness(corpus, corpus), "checkout")["pairing"] == "ambiguous"


def test_source_relations_reconcile_every_aggregate_added_removed_and_count_changed_edge_after_deduplication():
    a = simple()
    spans = a["resourceSpans"][0]["scopeSpans"][0]["spans"]
    spans.append(otlp_span(span=3, parent=1, name="old"))
    spans.append(clone(spans[1]))
    b = repeated()
    b["data"][0]["spans"].append(jaeger_span(trace=9, span=14, parent=11, name="new"))
    r = witness(a, b, {"failOnStructure": True})
    assert r["gate"]["failed"] is True
    expected = [*({**e, "change": "added"} for e in r["diff"]["edges"]["added"]),
                *({**e, "change": "removed"} for e in r["diff"]["edges"]["removed"]),
                *({**e, "change": "count-changed"} for e in r["diff"]["edges"]["countChanged"])]
    assert len(r["structuralWitnesses"]["edgeChanges"]) == len(expected)
    for e in r["structuralWitnesses"]["edgeChanges"]:
        assert any(x["change"] == e["change"] and x["child"]["operation"] == e["child"]["operation"] for x in expected)
        for side in ("baseline", "candidate"):
            assert e[f"{side}Count"] == len(e[side])
            for rel in e[side]:
                data = a if side == "baseline" else b
                child, parent = source_span(data, rel["child"]), source_span(data, rel["parent"])
                if "resourceSpans" in data:
                    assert child["parentSpanId"] == parent["spanId"] and child["traceId"] == parent["traceId"]
                else:
                    assert any(p["refType"] == "CHILD_OF" and p["traceID"] == parent["traceID"]
                               and p["spanID"] == parent["spanID"] for p in child["references"])


def test_missing_and_multi_parent_lineages_remain_unresolved_but_valid_edges_retain_source_evidence():
    child = jaeger_span(span=3, name="join")
    child["references"] = [{"refType": "CHILD_OF", "traceID": hex_(1, 32), "spanID": hex_(s)} for s in (1, 2)]
    a = jaeger([jaeger_span(), jaeger_span(span=2, name="other"), child,
                jaeger_span(span=4, parent=3, name="descendant"), jaeger_span(span=5, parent=99, name="orphan")])
    r = witness(simple(), a)
    assert row(r, "join") is None
    unresolved = r["structuralWitnesses"]["unresolved"]["candidate"]
    assert sorted(s["reason"] for s in unresolved) == ["missing-parent", "multiple-parents", "unresolved-ancestor"]
    assert len(next(s for s in unresolved if s["reason"] == "multiple-parents")["parents"]) == 2
    assert next(e for e in r["structuralWitnesses"]["edgeChanges"] if e["child"]["operation"] == "join"
                and e["parent"]["operation"] == "checkout")["candidateCount"] == 1
    assert len(r["candidate"]["diagnostics"]["missingParents"]) == 1


def test_keys_retain_service_kind_and_delimiter_containing_names_without_collisions():
    a = otlp([otlp_span(name='x/"y'), otlp_span(span=2, name='x/"y', kind=3)], "svc/[]")
    a["resourceSpans"].extend(otlp([otlp_span(span=3, name='x/"y')], "other")["resourceSpans"])
    assert len(witness(a, a)["structuralWitnesses"]["subtrees"]) == 3


def test_witness_mode_is_optional_and_strictly_validated_malformed_graphs_never_emit_witnesses():
    assert "structuralWitnesses" not in compare(simple(), simple())
    assert "spans" not in normalize(simple())
    assert "spans" in normalize(simple(), {"retainSpans": True})
    for opt in (None, [], {"retainSpans": 1}, {"unknown": True}):
        with pytest.raises(InputError):
            normalize(simple(), opt)
    for opt in (None, [], {"witnesses": 1}, {"unknown": True}):
        with pytest.raises(InputError):
            compare(simple(), simple(), {}, opt)
    with pytest.raises(InputError, match="cyclic"):
        witness(otlp([otlp_span(parent=1)]), simple())


def test_excessively_expanded_witness_paths_fail_explicitly_while_ordinary_aggregation_remains_available():
    a = deep_chain(500)
    assert compare(a, a)["baseline"]["spanCount"] == 500
    with pytest.raises(InputError, match="witness path budget exceeded"):
        witness(a, a)


def test_cross_trace_jaeger_child_of_resolves_a_path_with_both_trace_identities_preserved():
    child = jaeger_span(trace=2, span=2, name="query")
    child["references"] = [{"refType": "CHILD_OF", "traceID": hex_(1, 32), "spanID": hex_(1)}]
    a = jaeger([child, jaeger_span()])
    r = witness(a, a)
    query = row(r, "query", ["checkout"])
    assert query["pairing"] == "unique-structural-candidates"
    assert query["baseline"][0]["traceId"] == hex_(2, 32)
    assert r["structuralWitnesses"]["unresolved"]["baseline"] == []
    b = jaeger([child])
    missing = witness(a, b)
    e = missing["structuralWitnesses"]["edgeChanges"][0]
    assert e["baseline"][0]["parent"]["traceId"] == hex_(1, 32)
    assert e["baseline"][0]["child"]["traceId"] == hex_(2, 32)
    assert e["change"] == "removed"
    assert missing["structuralWitnesses"]["unresolved"]["candidate"][0]["reason"] == "missing-parent"


def test_serialized_path_key_character_budget_rejects_large_labels_before_emitting_a_partial_view():
    a = otlp([otlp_span(span=i + 1, name="x" * 100000, **({"parent": i} if i else {})) for i in range(14)])
    with pytest.raises(InputError, match=r"witness path budget exceeded \(8388608 serialized path-key characters"):
        witness(a, a)
