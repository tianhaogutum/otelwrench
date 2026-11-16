# SPDX-License-Identifier: Apache-2.0
"""Opt-in structural source witnesses: candidate sets per root-to-node path."""
from __future__ import annotations

from .jsontext import UNDEFINED, js_sorted, js_str, stringify, utf16_length
from .normalize import KINDS, group_key, sorted_canonical, span_key
from .validate import array, check, object_, string

PATH_COMPONENT_LIMIT = 100000
PATH_CHARACTER_LIMIT = 8 * 1024 * 1024
NOTICE = ("Structural candidate sets only: no individual causal alignment or workload equivalence. Duplicate paths "
          "and their descendants remain ambiguous. Missing/multiple-parent lineages are unresolved; aggregate edge "
          "evidence still includes all observed relations.")
SOURCE_LIMIT_NOTICE = ("Caps each side of subtree references and path/aggregate relation lists; totals describe full "
                       "deduplicated evidence. Unresolved diagnostics remain whole-corpus and uncapped. This is an "
                       "output cap, not a memory or path-expansion bound.")
UNRESOLVED_LIMIT_NOTICE = ("Caps unresolved entries independently per capture in existing canonical order; totals and "
                           "reason counts cover the whole corpus after span deduplication. Each listed entry retains "
                           "its complete parent list. This is an output cap, not a memory or overall-output bound.")
PARENT_LIMIT_NOTICE = ("Caps parent IDs independently per listed unresolved entry in existing canonical order, after "
                       "unresolved-entry selection. Parent totals cover complete deduplicated spans; reasons use full "
                       "parent lists. Ordinary diagnostics and aggregate relations are unchanged. This is a parent-list "
                       "output cap, not a memory or overall-output bound; path-expansion budgets still apply.")
SIDES = ("baseline", "candidate")


class _Node:
    __slots__ = ("parent", "group", "depth", "children", "refs", "ambiguous", "selected", "ambiguity_reasons")

    def __init__(self, parent, group, depth):
        self.parent, self.group, self.depth = parent, group, depth
        self.children: dict = {}
        self.refs = {"baseline": [], "candidate": []}
        self.ambiguous = False
        self.selected = None
        self.ambiguity_reasons: list = []


class _Unresolved:
    __slots__ = ("reason",)

    def __init__(self, reason):
        self.reason = reason


def _ref(span: dict) -> dict:
    return {"traceId": span["traceId"], "spanId": span["spanId"], "sourcePath": span["sourcePath"]}


def _path_key(parent_path: list, group: dict) -> str:
    return stringify({"parentPath": parent_path, "group": group})


def _relation_key(parent: dict, child: dict) -> str:
    return stringify([group_key(parent), group_key(child)])


def validate_witness_path(value) -> list:
    path = "options.witnessPath"
    array(value, path)
    check(len(value) > 0, path, "expected nonempty root-inclusive path")
    result = []
    for i, group in enumerate(value):
        p = f"{path}[{i}]"
        object_(group, p, ["service", "operation", "kind"])
        string(group.get("service", UNDEFINED), f"{p}.service", True)
        string(group.get("operation", UNDEFINED), f"{p}.operation", True)
        kind = group.get("kind")
        check(isinstance(kind, str) and kind in KINDS, f"{p}.kind", "expected canonical kind name")
        result.append({"service": group["service"], "operation": group["operation"], "kind": kind})
    return result


def _index(capture: dict, root: _Node, side: str) -> dict:
    spans = capture["spans"]
    states: dict = {}
    edges: dict = {}
    outgoing: dict = {}  # id(node) -> {child group key: {child, relations}}

    def resolved(span: dict, parent: _Node = root) -> _Node:
        # Intern structural paths by parent node and exact group. Both captures
        # share the trie so unselected ancestors still participate in ambiguity.
        key = group_key(span["group"])
        node = parent.children.get(key)
        if node is None:
            node = parent.children[key] = _Node(parent, span["group"], parent.depth + 1)
        node.refs[side].append(_ref(span))
        return node

    # The graph is already validated as acyclic. Follow single-parent chains
    # iteratively; never expand the potentially exponential paths of a DAG.
    for start in spans:
        chain = []
        current = start
        while current not in states:
            span = spans[current]
            if not span["parents"]:
                states[current] = resolved(span)
                break
            if len(span["parents"]) > 1:
                states[current] = _Unresolved("multiple-parents")
                break
            parent_key = span_key(span["parents"][0]["traceId"], span["parents"][0]["spanId"])
            if parent_key not in spans:
                states[current] = _Unresolved("missing-parent")
                break
            chain.append(current)
            current = parent_key
        while chain:
            key = chain.pop()
            span = spans[key]
            parent = span["parents"][0]
            state = states[span_key(parent["traceId"], parent["spanId"])]
            states[key] = _Unresolved("unresolved-ancestor") if isinstance(state, _Unresolved) \
                else resolved(span, state)
    unresolved = []
    for key, span in spans.items():
        state = states[key]
        if isinstance(state, _Unresolved):
            unresolved.append({"span": _ref(span), "group": span["group"], "reason": state.reason,
                               "parents": sorted_canonical(span["parents"])})
        for p in span["parents"]:
            parent = spans.get(span_key(p["traceId"], p["spanId"]))
            if parent is None:
                continue  # Missing parents never invent an aggregate edge.
            rel = {"parent": _ref(parent), "child": _ref(span)}
            edge_key = _relation_key(parent["group"], span["group"])
            if edge_key not in edges:
                edges[edge_key] = {"parent": parent["group"], "child": span["group"], "relations": []}
            edges[edge_key]["relations"].append(rel)
            parent_state = states[span_key(parent["traceId"], parent["spanId"])]
            if isinstance(parent_state, _Unresolved):
                continue
            children = outgoing.setdefault(id(parent_state), {})
            child_key = group_key(span["group"])
            if child_key not in children:
                children[child_key] = {"child": span["group"], "relations": []}
            children[child_key]["relations"].append(rel)
    return {"edges": edges, "outgoing": outgoing, "unresolved": sorted_canonical(unresolved)}


def _summary(total: int, limit: int) -> dict:
    listed = min(total, limit)
    return {"total": total, "listed": listed, "omitted": total - listed, "truncated": total > listed}


def _sources(baseline: list, candidate: list, limit) -> dict:
    """Summaries describe full deduplicated evidence, never the sliced list length."""
    lists = {"baseline": sorted_canonical(baseline), "candidate": sorted_canonical(candidate)}
    if limit is None:
        return lists
    source_summary = {}
    for side in SIDES:
        source_summary[side] = _summary(len(lists[side]), limit)
        lists[side] = lists[side][:limit]
    return {**lists, "sourceSummary": source_summary}


def _unresolved_entries(baseline: list, candidate: list, limit) -> dict:
    unresolved = {"baseline": baseline, "candidate": candidate}
    if limit is None:
        return {"unresolved": unresolved}
    unresolved_summary = {}
    for side in SIDES:
        entries = unresolved[side]
        reason_counts = {"missing-parent": 0, "multiple-parents": 0, "unresolved-ancestor": 0}
        for entry in entries:
            reason_counts[entry["reason"]] += 1
        unresolved_summary[side] = {**_summary(len(entries), limit), "reasonCounts": reason_counts}
        unresolved[side] = entries[:limit]
    return {"unresolvedLimit": limit, "unresolvedLimitNotice": UNRESOLVED_LIMIT_NOTICE,
            "unresolvedSummary": unresolved_summary, "unresolved": unresolved}


def _unresolved_parents(view: dict, limit) -> dict:
    if limit is None:
        return view
    unresolved = {side: [{**entry, "parents": entry["parents"][:limit],
                          "parentSummary": _summary(len(entry["parents"]), limit)}
                         for entry in view["unresolved"][side]] for side in SIDES}
    replaced = {}
    if view.get("unresolvedLimitNotice"):
        replaced["unresolvedLimitNotice"] = view["unresolvedLimitNotice"].replace(
            "Each listed entry retains its complete parent list.",
            "Listed parent lists use the independent parent cap.", 1)
    return {**view, **replaced, "parentLimit": limit, "parentLimitNotice": PARENT_LIMIT_NOTICE,
            "unresolved": unresolved}


def _edge_changes(baseline, candidate, source_limit) -> list:
    baseline = baseline if baseline is not None else {}
    candidate = candidate if candidate is not None else {}
    changes = []
    for key in dict.fromkeys([*baseline, *candidate]):
        b, c = baseline.get(key), candidate.get(key)
        baseline_count = len(b["relations"]) if b is not None else 0
        candidate_count = len(c["relations"]) if c is not None else 0
        if baseline_count == candidate_count:
            continue
        shape = b if b is not None else c
        changes.append({**({"parent": shape["parent"]} if shape.get("parent") else {}), "child": shape["child"],
                        "change": "added" if b is None else "removed" if c is None else "count-changed",
                        "baselineCount": baseline_count, "candidateCount": candidate_count,
                        **_sources(b["relations"] if b is not None else [],
                                   c["relations"] if c is not None else [], source_limit)})
    return sorted_canonical(changes)


def structural_witnesses(baseline: dict, candidate: dict, witness_path=None, source_limit=None,
                         unresolved_limit=None, parent_limit=None) -> dict:
    root = _Node(None, None, 0)
    b = _index(baseline, root, "baseline")
    c = _index(candidate, root, "candidate")
    selected_root = root
    if witness_path:
        for group in witness_path:
            selected_root = selected_root.children.get(group_key(group)) if selected_root is not None else None
        check(selected_root is not None, "options.witnessPath",
              "path not found in either capture (complete captured root path required)")
    # Visit all trie nodes once, but only expand selected paths. Ambiguity from
    # ancestors is shared across captures and cannot be hidden by the selector.
    pending = list(root.children.values())
    selected = []
    while pending:
        node = pending.pop()
        node.ambiguity_reasons = []
        if len(node.refs["baseline"]) > 1 or len(node.refs["candidate"]) > 1:
            node.ambiguity_reasons.append("duplicate-path")
        if node.parent.ambiguous:
            node.ambiguity_reasons.append("ambiguous-parent-path")
        node.ambiguous = len(node.ambiguity_reasons) > 0
        node.selected = selected_root is root or node is selected_root or node.parent.selected is True
        if node.selected:
            selected.append(node)
        pending.extend(node.children.values())
    budgets = {side: {"components": 0, "characters": 0} for side in SIDES}
    rows = []
    for node in selected:
        for side in SIDES:
            budgets[side]["components"] += node.depth * len(node.refs[side])
            check(budgets[side]["components"] <= PATH_COMPONENT_LIMIT, "witnesses",
                  "witness path budget exceeded (100000 total root-to-span components per capture); narrow "
                  "--witness-path or omit --witnesses for aggregate comparison")
        parent_path = []
        parent = node.parent
        while parent is not root:
            parent_path.append(parent.group)
            parent = parent.parent
        parent_path.reverse()
        key = {"parentPath": parent_path, "group": node.group}
        serialized = _path_key(parent_path, node.group)
        length = utf16_length(serialized)
        for side in SIDES:
            budgets[side]["characters"] += length * len(node.refs[side])
            check(budgets[side]["characters"] <= PATH_CHARACTER_LIMIT, "witnesses",
                  "witness path budget exceeded (8388608 serialized path-key characters per capture); narrow "
                  "--witness-path or omit --witnesses for aggregate comparison")
        nb, nc = node.refs["baseline"], node.refs["candidate"]
        row = {"key": key,
               "presence": "candidate-only" if not nb else "baseline-only" if not nc else "both",
               "pairing": "ambiguous" if node.ambiguous else "unique-structural-candidates" if nb and nc
               else "unpaired",
               "ambiguityReasons": node.ambiguity_reasons, **_sources(nb, nc, source_limit),
               "edgeChanges": _edge_changes(b["outgoing"].get(id(node)), c["outgoing"].get(id(node)), source_limit)}
        rows.append((serialized, row))
    # Order by parent-path length, then serialized key (UTF-16 code-unit order).
    rows = js_sorted(rows, key=lambda r: r[0])
    rows.sort(key=lambda r: len(r[1]["key"]["parentPath"]))
    source_notice = SOURCE_LIMIT_NOTICE
    if parent_limit is not None:
        source_notice = source_notice.replace(
            "Unresolved diagnostics remain whole-corpus and uncapped.",
            "Unresolved diagnostics cover the whole corpus; parent lists use the independent parent cap"
            + (" and entries use the independent unresolved-entry cap" if unresolved_limit is not None else "")
            + ".", 1)
    elif unresolved_limit is not None:
        source_notice = source_notice.replace(
            "Unresolved diagnostics remain whole-corpus and uncapped.",
            "Unresolved diagnostics cover the whole corpus and use the independent unresolved-entry cap.", 1)
    return {"notice": NOTICE,
            **({"sourceLimit": source_limit, "sourceLimitNotice": source_notice} if source_limit is not None else {}),
            **({"selection": {"path": witness_path, "baselineMatches": len(selected_root.refs["baseline"]),
                              "candidateMatches": len(selected_root.refs["candidate"])}} if witness_path else {}),
            "subtrees": [row for _, row in rows],
            "edgeChanges": _edge_changes(b["edges"], c["edges"], source_limit),
            **_unresolved_parents(_unresolved_entries(b["unresolved"], c["unresolved"], unresolved_limit),
                                  parent_limit)}


def render_witnesses(view: dict) -> list:
    lines = [f"Structural witnesses: {view['notice']}"]
    if "selection" in view:
        sel = view["selection"]
        lines.append(f"Witness selection: {stringify(sel['path'])} baseline={sel['baselineMatches']} "
                     f"candidate={sel['candidateMatches']}; subtree paths are selected; aggregate edge evidence and "
                     "unresolved diagnostics cover the whole corpus.")
    if "sourceLimit" in view:
        lines.append(f"Witness source limit: {js_str(view['sourceLimit'])}. {view['sourceLimitNotice']}")
    if "unresolvedLimit" in view:
        lines.append(f"Witness unresolved limit: {js_str(view['unresolvedLimit'])}. {view['unresolvedLimitNotice']}")
    if "parentLimit" in view:
        lines.append(f"Witness parent limit: {js_str(view['parentLimit'])}. {view['parentLimitNotice']}")

    def source(r: dict) -> str:
        return f"{r['traceId']}/{r['spanId']} at {r['sourcePath']}"

    def summary(entry: dict, side: str, indent: str) -> None:
        if not entry.get("sourceSummary"):
            return
        s = entry["sourceSummary"][side]
        lines.append(f"{indent}{side} sources: listed={s['listed']} total={s['total']} omitted={s['omitted']} "
                     f"truncated={js_str(s['truncated'])}")

    def relations(edge: dict, indent: str) -> None:
        for side in SIDES:
            summary(edge, side, indent)
            for rel in edge[side]:
                lines.append(f"{indent}{side} {source(rel['parent'])} -> {source(rel['child'])}")

    for s in view["subtrees"]:
        reasons = f" reasons={','.join(s['ambiguityReasons'])}" if s["ambiguityReasons"] else ""
        lines.append(f"witness {s['pairing']} ({s['presence']}) {stringify(s['key'])}{reasons}")
        for side in SIDES:
            summary(s, side, "  ")
            for span in s[side]:
                lines.append(f"  {side} {source(span)}")
        for e in s["edgeChanges"]:
            lines.append(f"  path edge {stringify(e['child'])}: {e['baselineCount']} -> {e['candidateCount']}")
            relations(e, "    ")
    for e in view["edgeChanges"]:
        lines.append(f"aggregate witness edge {e['change']} {stringify(e['parent'])} -> {stringify(e['child'])}: "
                     f"{e['baselineCount']} -> {e['candidateCount']}")
        relations(e, "  ")
    for side in SIDES:
        if "unresolvedSummary" in view:
            s = view["unresolvedSummary"][side]
            lines.append(f"{side} unresolved: listed={s['listed']} total={s['total']} omitted={s['omitted']} "
                         f"truncated={js_str(s['truncated'])} reasonCounts={stringify(s['reasonCounts'])}")
        for u in view["unresolved"][side]:
            parent_summary = f" parentSummary={stringify(u['parentSummary'])}" if "parentSummary" in u else ""
            lines.append(f"witness unresolved {side} {u['reason']} {source(u['span'])} "
                         f"parents={stringify(u['parents'])}{parent_summary}")
    return lines
