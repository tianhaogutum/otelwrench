# SPDX-License-Identifier: Apache-2.0
"""Synthetic capture builders and CLI helpers shared by the tracediff tests."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import math
from dataclasses import dataclass
from pathlib import Path

from otelwrench.tracediff.cli import main
from otelwrench.tracediff.jsontext import MAX_SAFE_INTEGER, UNDEFINED, js_round, stringify

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "tracediff"
BASE_ARGS = [str(EXAMPLES / "baseline.json"), str(EXAMPLES / "compatible.json")]
PROG = "otelwrench tracediff"
MAX_SAFE = MAX_SAFE_INTEGER
clone = copy.deepcopy


@dataclass
class Result:
    status: int
    stdout: str
    stderr: str


def run(args, cwd=None) -> Result:
    """Run the CLI in-process and capture its UTF-8 stdout/stderr."""
    out, err = io.BytesIO(), io.BytesIO()
    out_text = io.TextIOWrapper(out, encoding="utf-8", newline="")
    err_text = io.TextIOWrapper(err, encoding="utf-8", newline="")
    with contextlib.ExitStack() as stack:
        if cwd is not None:
            stack.enter_context(contextlib.chdir(cwd))
        stack.enter_context(contextlib.redirect_stdout(out_text))
        stack.enter_context(contextlib.redirect_stderr(err_text))
        status = main([str(a) for a in args])
    out_text.flush()
    err_text.flush()
    return Result(status, out.getvalue().decode("utf-8"), err.getvalue().decode("utf-8"))


def write_json(path, value) -> Path:
    """``writeFileSync(path, JSON.stringify(value))``."""
    Path(path).write_text(stringify(value), encoding="utf-8")
    return Path(path)


def summary(total: int, n: int) -> dict:
    return {"total": total, "listed": min(total, n), "omitted": max(0, total - n), "truncated": total > n}


def json_error(stderr: str) -> dict:
    return json.loads(stderr)["error"]


def reverse_keys(value):
    """Recursively reverse object key order (``JSON.stringify`` replacer used by the original tests)."""
    if isinstance(value, dict):
        return {k: reverse_keys(v) for k, v in reversed(list(value.items()))}
    if isinstance(value, list):
        return [reverse_keys(v) for v in value]
    return value


# --- fixtures.js -----------------------------------------------------------

def hex_(n, width: int = 16) -> str:
    return format(int(n), "x").rjust(width, "0")


def otlp_span(trace=1, span=1, parent=None, name="checkout", kind=2, ms=100, error=False,
              start=1700000000000000000) -> dict:
    return {"traceId": hex_(trace, 32), "spanId": hex_(span), **({"parentSpanId": hex_(parent)} if parent else {}),
            "name": name, "kind": kind, "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(start + js_round(ms * 1e6)), "status": {"code": 2 if error else 1}}


def otlp(spans=None, service="shop") -> dict:
    spans = [otlp_span()] if spans is None else spans
    return {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                               "scopeSpans": [{"spans": spans}]}]}


def _num(value):
    return int(value) if isinstance(value, float) and value.is_integer() else value


def jaeger_span(trace=1, span=1, parent=None, name="checkout", kind="server", ms=100, error=False,
                processID="p1") -> dict:
    return {"traceID": hex_(trace, 32), "spanID": hex_(span), "operationName": name, "processID": processID,
            "startTime": 1700000000000000, "duration": _num(ms * 1000),
            "references": [{"refType": "CHILD_OF", "traceID": hex_(trace, 32), "spanID": hex_(parent)}] if parent else [],
            "tags": [{"key": "span.kind", "type": "string", "value": kind},
                     {"key": "error", "type": "bool", "value": error}], "logs": [], "warnings": None}


def jaeger(spans=None, service="shop") -> dict:
    spans = [jaeger_span()] if spans is None else spans
    traces: dict = {}
    for s in spans:
        if s["traceID"] not in traces:
            traces[s["traceID"]] = {"traceID": s["traceID"], "spans": [],
                                    "processes": {"p1": {"serviceName": service, "tags": []}}, "warnings": None}
        traces[s["traceID"]]["spans"].append(s)
    return {"data": list(traces.values()), "total": len(traces), "limit": 0, "offset": 0, "errors": None}


QUERY_PATH = [{"service": "shop", "operation": op, "kind": "server"} for op in ("checkout", "query")]
OTLP_KIND = {"internal": 1, "server": 2, "client": 3}


# --- groups.js -------------------------------------------------------------

def group_corpus(n, format="otlp", duplicate=True, offsetMs=0, extra=0, error=False, rename=False) -> dict:
    """Exactly n distinct groups; repeated spans are distinct identities, one optional exact duplicate."""
    resources, spans, processes = [], [], {}
    for i in range(n):
        service = "shop" if i < 2 else ["a|b", "a", "Ω"][i % 3]
        if i == 0:
            name = "checkout"
        elif i == 1:
            name = "query"
        else:
            name = f"{'op|x' if i % 2 else 'op'}-{i}{'-new' if rename and i == n - 1 else ''}"
        kind = "server" if i < 2 else ["server", "client", "internal"][i % 3]
        group_spans = []
        for j in range(2 + i % 3 + (extra if i == n - 1 else 0)):
            params = {"span": i * 10 + j + 1, **({"parent": 11 if i == 2 else 1} if i else {}), "name": name,
                      "kind": kind, "ms": (j + 1) * 10 + offsetMs, "error": error or j == 1}
            if format == "otlp":
                group_spans.append(otlp_span(**{**params, "kind": OTLP_KIND[kind]}))
            else:
                s = jaeger_span(**params, processID=f"p{i}")
                s["warnings"] = ["repeat", "repeat"]
                if i > 2 and i % 17 == 0:
                    s["references"] = [{"refType": "CHILD_OF", "traceID": hex_(1, 32), "spanID": hex_(999999)}]
                elif i > 2 and i % 19 == 0:
                    s["references"].append({"refType": "CHILD_OF", "traceID": hex_(1, 32), "spanID": hex_(2)})
                group_spans.append(s)
        if duplicate and i == 0:
            group_spans.append(clone(group_spans[0]))
        resources.extend(otlp(group_spans, service)["resourceSpans"])
        spans.extend(group_spans)
        processes[f"p{i}"] = {"serviceName": service, "tags": []}
    if format == "otlp":
        return {"resourceSpans": resources}
    capture = jaeger(spans)
    if capture["data"]:
        capture["data"][0]["processes"] = processes
    return capture


# --- edges.js --------------------------------------------------------------

def edge_corpus(n, format="otlp", duplicate=True, extra=0, offsetMs=0, error=False, rename=False,
                diagnostics=False) -> dict:
    """Exactly n distinct aggregate edge rows, including a valid root-only n=0."""
    make = otlp_span if format == "otlp" else jaeger_span
    roots = [make(span=span) for span in (1, 2)]
    resources = list(otlp(roots)["resourceSpans"])
    spans = list(roots)
    processes = {"p1": {"serviceName": "shop", "tags": []}}
    for i in range(n):
        service = "shop" if i == 0 else ["a|b", "a", "Ω"][i % 3]
        name = "query" if i == 0 else f"op|{i}{'-new' if rename and i == n - 1 else ''}"
        kind = "server" if i == 0 else ["server", "client", "internal"][i % 3]
        children = []
        for j in range(2 + i % 3 + (extra if i == n - 1 else 0)):
            params = {"span": 10 + i * 10 + j, "parent": 1 + j % 2, "name": name,
                      "kind": OTLP_KIND[kind] if format == "otlp" else kind,
                      "ms": 10 + (offsetMs if i == n - 1 else 0), "error": i == n - 1 and error}
            s = otlp_span(**params) if format == "otlp" else jaeger_span(**params, processID=f"p{i + 2}")
            if format == "jaeger" and diagnostics:
                s["warnings"] = ["repeat", "repeat"]
                if i > 0 and i % 17 == 0:
                    s["references"].append({"refType": "CHILD_OF", "traceID": hex_(1, 32), "spanID": hex_(999999)})
                if i > 0 and i % 19 == 0:
                    s["references"].append({"refType": "CHILD_OF", "traceID": hex_(1, 32), "spanID": hex_(2 - j % 2)})
            children.append(s)
        resources.extend(otlp(children, service)["resourceSpans"])
        spans.extend(children)
        processes[f"p{i + 2}"] = {"serviceName": service, "tags": []}
    if duplicate:
        resources[0]["scopeSpans"][0]["spans"].append(clone(roots[0]))
        spans.append(clone(roots[0]))
    if format == "otlp":
        return {"resourceSpans": resources}
    capture = jaeger(spans)
    capture["data"][0]["processes"] = processes
    return capture


# --- witness-unresolved.js -------------------------------------------------

def unresolved_corpus(n, duplicate=False, ms=100, error=False) -> dict:
    """Unresolved entries outside the selected query path; the valid query is repeated."""
    spans = [jaeger_span(), jaeger_span(span=2, parent=1, name="query"), jaeger_span(span=3, parent=1, name="query"),
             jaeger_span(span=4, parent=2, name="leaf")]
    for i in range(n):
        s = jaeger_span(span=100 + i, parent=98 + i if i % 3 == 2 else 9999, name="unresolved", ms=ms, error=error)
        if i % 3 == 1:
            s["references"] = [{"refType": "CHILD_OF", "traceID": hex_(1, 32), "spanID": hex_(x)} for x in (3, 1)]
        spans.append(s)
    if duplicate and n:
        spans.append(clone(spans[4]))
    return jaeger(spans)


# --- missing-parents.js ----------------------------------------------------

def missing_parent_corpus(n, duplicate=False, ms=100, error=False) -> dict:
    """Each child refers across traces to an absent ID that exists in its own trace."""
    spans = unresolved_corpus(0)["data"][0]["spans"]
    for i in range(n):
        s = jaeger_span(span=100 + i, name="missing", ms=ms, error=error)
        s["references"] = [{"refType": "CHILD_OF", "traceID": hex_(i % 2 + 2, 32), "spanID": hex_(1)}]
        spans.append(s)
    if duplicate and n:
        spans.append(clone(spans[4]))
    return jaeger(spans)


# --- upstream-warnings.js --------------------------------------------------

def warning_corpus(n, duplicate=False, unresolved=6, ms=100, error=False) -> dict:
    """Exactly n source-warning occurrences, including those on an exact duplicate."""
    capture = unresolved_corpus(unresolved, ms=ms, error=error)
    extra = jaeger_span(trace=2)
    capture["data"].append({**clone(capture["data"][0]), "traceID": extra["traceID"], "spans": [extra]})
    trace = capture["data"][0]
    if duplicate and n >= 2:
        trace["spans"][1]["warnings"] = ["repeat"]
    targets = [trace, trace["spans"][0], capture["data"][1], extra]
    for i in range(n - (2 if duplicate and n >= 2 else 0)):
        target = targets[i % len(targets)]
        if target.get("warnings") is None:
            target["warnings"] = []
        target["warnings"].append("repeat" if i % 3 else "z warning" if i % 2 else "a warning")
    if duplicate:
        trace["spans"].append(clone(trace["spans"][1]))
    return capture


# --- witness-parents.js ----------------------------------------------------

def parent_corpus(n=200, duplicate=False, ms=100, error=False) -> dict:
    """n distinct parents, including identical span IDs in two different traces."""
    spans = unresolved_corpus(0)["data"][0]["spans"]
    parents = [jaeger_span(trace=i % 2 + 1, span=1000 + i // 2, name="parent") for i in range(n)]
    spans.extend(parents)
    multi = jaeger_span(span=100, name="unresolved", ms=ms, error=error)
    multi["references"] = [{"refType": "CHILD_OF", "traceID": s["traceID"], "spanID": s["spanID"]}
                           for s in reversed(parents)]
    # n=0 intentionally has no unresolved entry: zero-parent spans are roots.
    if n:
        spans.extend([multi, jaeger_span(span=101, parent=9999, name="unresolved", ms=ms, error=error),
                      jaeger_span(span=102, parent=100, name="unresolved", ms=ms, error=error)])
    if duplicate and n:
        spans.append(clone(multi))
    return jaeger(spans)


# --- operation-diffs.js ----------------------------------------------------

def operation_corpus(n, prefix="added", common=3, format="otlp") -> dict:
    """Prefix only the exclusive groups; keep checkout/query and other common groups intact."""
    capture = group_corpus(n + common, format=format)
    if format == "otlp":
        for r in capture["resourceSpans"][common:]:
            for s in r["scopeSpans"][0]["spans"]:
                s["name"] = f"{prefix}:{s['name']}"
    else:
        for s in capture["data"][0]["spans"]:
            if int(s["processID"][1:]) >= common:
                s["operationName"] = f"{prefix}:{s['operationName']}"
    return capture


def deep_chain(length=500) -> dict:
    return otlp([otlp_span(span=i + 1, **({"parent": i} if i else {})) for i in range(length)])


# --- shared cap-test matrices ----------------------------------------------

INVALID_API_VALUES = [None, UNDEFINED, 0, -1, 1.5, math.inf, -math.inf, math.nan, MAX_SAFE_INTEGER + 1, "2", True, {},
                      []]
BAD_CLI_VALUES = ["0", "-1", "1.5", "null", "true", '"2"', "[]", "{}", "NaN", "Infinity", "-Infinity", "1e999",
                  "9007199254740992", "wat", "", " "]


def bad_cap_args(option, values, prefix=()):
    """Invalid, missing and duplicate values for one cap option."""
    return [[*prefix, option, v] for v in values] + [[*prefix, option], [*prefix, option, "2", option, "2"]]


def cli_error_matrix(cli, option, bad_args) -> None:
    """Every bad argument list exits 2 in default/text/JSON format, before or after the bad option."""
    for args in bad_args:
        for fmt in ([], ["--format", "text"], ["--format", "json"]):
            for before in (True, False):
                r = cli([*fmt, *args] if before else [*args, *fmt])
                assert r.status == 2 and r.stdout == "", (args, fmt, before)
                if fmt[1:] == ["json"]:
                    error = json.loads(r.stderr)["error"]
                    assert error["code"] == "INPUT_ERROR" and option in error["message"], (args, error)
                else:
                    assert r.stderr.startswith(f"{PROG}: ") and option in r.stderr, (args, r.stderr)


def format_interplay(cli, option) -> None:
    """The first --format decides the error encoding even when the cap error comes first."""
    for args, as_json in ((["--format", "json", option, "0", "--format", "text"], True),
                          (["--format", "text", option, "0", "--format", "json"], False),
                          (["--format", "bad", option, "0", "--format", "json"], False)):
        r = cli(args)
        assert r.status == 2 and r.stdout == ""
        if as_json:
            assert json.loads(r.stderr)["error"]["code"] == "INPUT_ERROR"
        else:
            assert r.stderr.startswith(f"{PROG}:")


def bad_otlp_variants(make):
    """Conflicting duplicate, self-cycle and invalid-kind variants of the last OTLP resource."""
    for kind in ("conflict", "cycle", "invalid"):
        bad = make()
        spans = bad["resourceSpans"][-1]["scopeSpans"][0]["spans"]
        if kind == "conflict":
            spans.append({**spans[0], "name": "conflict"})
        if kind == "cycle":
            spans[0]["parentSpanId"] = spans[0]["spanId"]
        if kind == "invalid":
            spans[0]["kind"] = -1
        yield bad


# --- comparison/observation ordering fixtures ------------------------------

TUPLES = [("a|b", "c", 2), ("a", "b|c", 2), ("a", "b|c", 3), ("a", "b|d", 2), ("Ω", '"\\\n', 2)]


def unpaired(data, name):
    """Copy of a capture with one extra group that exists only on this side."""
    c = clone(data)
    if "resourceSpans" in c:
        c["resourceSpans"].extend(otlp([otlp_span(span=99999, name=name)])["resourceSpans"])
    else:
        c["data"][0]["spans"].append(jaeger_span(span=99999, name=name, processID="p0"))
    return c


def tuple_capture():
    return {"resourceSpans": [r for i, (service, name, kind) in enumerate(TUPLES)
                              for r in otlp([otlp_span(span=1 + i * 10 + j, name=name, kind=kind)
                                             for j in range(5 - i)], service)["resourceSpans"]]}
