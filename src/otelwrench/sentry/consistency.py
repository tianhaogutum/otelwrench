"""Read-only consistency diagnostics over explicitly supplied transaction spans."""
from collections import Counter, defaultdict


PARENT_STATES = ("not_supplied", "resolved", "ambiguous", "missing", "other_trace_only")


class TraceConsistency:
    def __init__(self):
        self.spans = []
        self.uncorrelated_logs = []

    def add_transaction(self, item_index, resource, spans, output_pointer):
        for index, span in enumerate(spans):
            location = {"itemIndex": item_index,
                        "path": "/contexts/trace" if index == 0 else f"/spans/{index - 1}",
                        "outputPointer": f"{output_pointer}/{index}"}
            self.spans.append((span, resource, location))

    def add_log(self, item_index, record, output_pointer):
        if "traceId" not in record:
            self.uncorrelated_logs.append({"itemIndex": item_index, "path": "/contexts/trace",
                                           "outputPointer": output_pointer})

    def report(self):
        by_id = defaultdict(list)
        supplied_span_ids = set()
        for span, resource, location in self.spans:
            by_id[(span["traceId"], span["spanId"])].append((span, resource, location))
            supplied_span_ids.add(span["spanId"])

        duplicates, conflicting = [], set()
        for (trace_id, span_id), occurrences in by_id.items():
            if len(occurrences) < 2:
                continue
            first_span, first_resource, _ = occurrences[0]
            identical = all(span == first_span and resource == first_resource
                            for span, resource, _ in occurrences[1:])
            if not identical:
                conflicting.add((trace_id, span_id))
            duplicates.append({"kind": "identical" if identical else "conflicting",
                               "traceId": trace_id, "spanId": span_id,
                               "occurrences": [location for _, _, location in occurrences]})

        parents = []
        for span, _, location in self.spans:
            relationship = {**location, "traceId": span["traceId"], "spanId": span["spanId"]}
            if "parentSpanId" not in span:
                status = "not_supplied"
            else:
                parent_id = span["parentSpanId"]
                relationship["parentSpanId"] = parent_id
                parent_key = (span["traceId"], parent_id)
                if parent_key in conflicting:
                    status = "ambiguous"
                elif parent_key in by_id:
                    status = "resolved"
                elif parent_id in supplied_span_ids:
                    # This is evidence of ID reuse, not permission to attach to
                    # that trace. A same-trace external parent may still exist.
                    status = "other_trace_only"
                else:
                    status = "missing"
            relationship["status"] = status
            parents.append(relationship)

        # A conflicting definition never supplies a guessed edge or target.
        # Identical duplicates do supply one unambiguous definition for checking.
        edges = {}
        for key, occurrences in by_id.items():
            if key in conflicting:
                continue
            span = occurrences[0][0]
            parent_key = (key[0], span.get("parentSpanId"))
            if parent_key in by_id and parent_key not in conflicting:
                edges[key] = parent_key
        cycles = _cycles(edges)
        duplicate_counts = Counter(duplicate["kind"] for duplicate in duplicates)
        parent_counts = Counter(parent["status"] for parent in parents)
        return {
            "summary": {"traceIds": len({key[0] for key in by_id}),
                        "spanOccurrences": len(self.spans), "spanIdentities": len(by_id),
                        "identicalDuplicateGroups": duplicate_counts["identical"],
                        "conflictingDuplicateGroups": duplicate_counts["conflicting"],
                        "parents": {state: parent_counts[state] for state in PARENT_STATES},
                        "cycles": len(cycles), "uncorrelatedLogs": len(self.uncorrelated_logs)},
            "duplicates": duplicates, "parents": parents, "cycles": cycles,
            "uncorrelatedLogs": self.uncorrelated_logs,
        }


def _cycles(edges):
    """Iterative walk of a functional graph; one canonical entry per cycle."""
    done, cycles = set(), []
    for start in edges:
        path, positions = [], {}
        current = start
        while current in edges and current not in done and current not in positions:
            positions[current] = len(path)
            path.append(current)
            current = edges[current]
        if current in positions:
            cycle = path[positions[current]:]
            # Rotate rather than sort: retain the actual child-to-parent order.
            first = cycle.index(min(cycle))
            cycle = cycle[first:] + cycle[:first]
            cycles.append({"traceId": cycle[0][0], "spanIds": [key[1] for key in cycle]})
        done.update(path)
    return sorted(cycles, key=lambda cycle: (cycle["traceId"], cycle["spanIds"]))
