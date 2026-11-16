# otelwrench tracediff

Changed trace IDs, span order or wire format make two trace captures look completely
different even when the system behaved the same. `otelwrench tracediff` compares two
saved OTLP or Jaeger trace corpora offline: it normalizes spans into
`(service, operation, kind)` groups and parent-operation edges, then reports latency,
error and topology changes and gates them with a CI budget. Use it to check a
load-test or staging capture against a baseline before you ship; Jaeger's UI
comparison is interactive, this command gives a local, machine-readable verdict.

## Quick start

Install with `pip install otelwrench` (Python 3.11+, no extra dependencies). The
commands use the synthetic captures under [`examples/tracediff/`](../examples/tracediff/),
so run them from a clone of the repository.

```sh
# OTLP baseline vs. an equivalent Jaeger capture with new IDs and order: PASS, exit 0
otelwrench tracediff examples/tracediff/baseline.json examples/tracediff/compatible.json \
  --config examples/tracediff/budget.json

# Same comparison as a JSON report
otelwrench tracediff examples/tracediff/baseline.json examples/tracediff/compatible.json --format json

# A deliberate regression (checkout 100 -> 150 ms plus a new audit child): FAIL, exit 1
otelwrench tracediff examples/tracediff/baseline.json examples/tracediff/regression.json \
  --config examples/tracediff/budget.json --format json
```

The compatible corpora contain 20 traces / 40 spans each: `checkout` (shop, server,
100 ms, 10% errors) calls `select` (database, client, 40 ms). They differ in IDs,
ordering and wire format, so the expected result is PASS, no structural changes and 20
occurrences of the cross-service edge. The command only reads local files and writes
its report to stdout; it never executes capture content.

```
otelwrench tracediff BASELINE.json CANDIDATE.json [--config POLICY.json] [--format text|json]
    [--missing-parent-limit N] [--upstream-warning-limit N] [--group-limit N] [--edge-limit N]
    [--comparison-limit N] [--observation-limit N] [--operation-diff-limit N]
    [--witnesses [--witness-path PATH.json] [--witness-source-limit N]
                 [--witness-unresolved-limit N] [--witness-parent-limit N]]
```

`--help` and `--version` are available. Inputs must be regular UTF-8 JSON files of at
most 64 MiB each; stdin, URLs and compressed files are not read.

## Exit codes and output

| Exit | Meaning |
| --- | --- |
| 0 | Pass |
| 1 | A configured latency, structure or observation gate failed (the full report is still printed) |
| 2 | Malformed or unsupported input, invalid configuration, usage or file error |

Default output is text. With `--format json` a successful comparison (including exit 1)
goes to stdout and an exit-2 error is a single JSON object on stderr:
`{"error":{"code":"INPUT_ERROR","message":"..."}}`. In text mode errors are printed as
`otelwrench tracediff: <message>`.

The first `--format` option decides the error encoding, even when it follows the
invalid option: `--witnesses --witness-source-limit 0 --format json` exits 2 with an
empty stdout and an `INPUT_ERROR` object on stderr. Values are never scanned for
option names, so `--config "policy --format json"` does not switch to JSON.

## Budget

```json
{ "absoluteP95Ms": 20, "relativeP95Percent": 20, "minSamples": 20, "failOnStructure": false }
```

These are the defaults ([`examples/tracediff/budget.json`](../examples/tracediff/budget.json));
any field may be overridden, unknown keys are rejected. A group common to both corpora
fails only when both sides have at least `minSamples` spans and the candidate p95
exceeds the baseline p95 by **strictly more than both** thresholds. 100 -> 150 ms fails
the default budget (50 ms and 50%). Threshold decisions use exact integer ratios against
the decimal form of the policy number, so `0.000003` ms means exactly 3 ns.

A zero baseline followed by a positive p95 is an infinite relative increase
(`relativeP95Percent: null`, `relativeState: "infinite"`); the absolute threshold and
sample minimum still apply. Zero to zero is 0%. Low-sample groups stay in the report
with `reason: "insufficient-samples"`.

Added/removed groups and added/removed edges are always reported; with
`failOnStructure: true` they also fail the gate. Changed edge counts are reported but
never fail it.

### Optional count and error-fraction gates

```json
{
  "countGate": { "maxChangePercent": 20, "minSamples": 20 },
  "errorFractionGate": { "maxIncreasePercent": 50, "minSamples": 20 }
}
```

`countGate` fails when `abs(candidate - baseline) / baseline * 100` is strictly greater
than its budget, in either direction. `errorFractionGate` fails for a relative
*increase* of `errorCount / count` strictly greater than its budget: 10% -> 20% errors
is a 100% relative increase. Each gate has its own `minSamples` (default 20), independent
of the latency minimum. Enabled gates add one row per union group under
`observationGates.count` / `observationGates.errorFraction`, and their failure counts
under `gate.countGateFailures` / `gate.errorFractionGateFailures`.

| Reason | Decision |
| --- | --- |
| `new-group`, `removed-group` | Unpaired: skipped, `relativeState: "unpaired"`, null change |
| `insufficient-samples` | Below this gate's minimum on either side: skipped |
| `zero-baseline-increase` | Error fraction 0 -> positive: fails, `relativeState: "infinite"` |
| `zero-to-zero` | Both error fractions zero: passes |
| `at-threshold` | Exactly at a budget: passes |
| `threshold-exceeded` | Strictly above: fails |
| `within-budget` | Below the budget, or an error-fraction improvement: passes |

```sh
# 20 -> 30 spans per operation, checkout errors 2/20 -> 6/30: latency-only policy passes
otelwrench tracediff examples/tracediff/baseline.json examples/tracediff/observation-candidate.json
# With the opt-in gates: FAIL, exit 1 (countGateFailures 2, errorFractionGateFailures 1)
otelwrench tracediff examples/tracediff/baseline.json examples/tracediff/observation-candidate.json \
  --config examples/tracediff/observation-budget.json --format json
```

These are observational corpus differences, not causal regressions. They contribute to
`gate.failed` (and its alias `regression`); `comparisons` and `latencyRegressions` keep
their latency-only meaning.

**Absolute allowances.** `countGate.maxChangeCount` (a nonnegative safe integer) and
`errorFractionGate.maxIncreasePercentagePoints` (a finite nonnegative number, in
percentage points) make a gate fail only when **both** its relative and its absolute
budget are exceeded. Rows then also carry `changeCount` / `changePercentagePoints`,
`absoluteExceeded` and `relativeExceeded`.

```sh
# 20 -> 30 spans passes with maxChangeCount 10 even at a 0% relative budget
otelwrench tracediff examples/tracediff/baseline.json examples/tracediff/observation-candidate.json \
  --config examples/tracediff/count-absolute-budget.json --format json
# checkout 10% -> 20% errors (+10 pp) passes with maxIncreasePercentagePoints 10
otelwrench tracediff examples/tracediff/baseline.json examples/tracediff/observation-candidate.json \
  --config examples/tracediff/error-fraction-budget.json --format json
```

## Structural source witnesses

`--witnesses` adds `structuralWitnesses` to the report: observed subtrees grouped by
`{parentPath: [{service, operation, kind}, ...], group: {service, operation, kind}}`,
each with source references (`traceId`, `spanId`, JSON `sourcePath`) for both sides.

```sh
otelwrench tracediff examples/tracediff/witness-baseline.json examples/tracediff/witness-candidate.json \
  --witnesses --format json
```

Here one OTLP `checkout` calls one `database/query` while the Jaeger capture calls it
twice: exit 0, aggregate query-edge count 1 -> 2, `checkout` is
`unique-structural-candidates`, `query` is `ambiguous` with one baseline and two
candidate references. Rows report `presence` (`both`, `baseline-only`,
`candidate-only`), `pairing` (`unique-structural-candidates`, `unpaired`, `ambiguous`)
and `ambiguityReasons` (`duplicate-path`, `ambiguous-parent-path`), plus per-path
`edgeChanges`. Top-level `edgeChanges` lists the source relations behind every
aggregate added, removed and count-changed edge. Spans with a missing parent, several
CHILD_OF parents, or an unresolved ancestor are listed under `unresolved.baseline` /
`unresolved.candidate` with reasons `missing-parent`, `multiple-parents` and
`unresolved-ancestor`.

Witnesses are candidate sets only: they never pair individual spans and make no claim
of causal alignment or workload equivalence. Expansion is limited per capture to
100,000 root-to-span path components and 8,388,608 serialized path-key characters
(UTF-16 code units); exceeding either is an exit-2 input error. Omit `--witnesses` for
aggregate comparison of such captures.

`--witness-path PATH.json` expands only one subtree. The selector is a nonempty JSON
array of exact groups from the captured root to the selected node, for example
[`examples/tracediff/witness-query-path.json`](../examples/tracediff/witness-query-path.json).
Aggregate diffs, gates, edge evidence and unresolved diagnostics still cover the whole
corpus; an unknown path is an input error.

## Output caps

Large corpora produce long lists. Each cap below takes a positive safe integer, limits
one output list in canonical order and adds a notice plus exact
`{total, listed, omitted, truncated}` totals. Gates, diffs and statistics always use the
complete corpus; the caps are output limits, not memory bounds.

| Option | Caps | Summary field |
| --- | --- | --- |
| `--missing-parent-limit N` | `diagnostics.missingParents` per capture | `diagnostics.missingParentSummary` |
| `--upstream-warning-limit N` | `diagnostics.upstreamWarnings` per capture (occurrences before deduplication) | `diagnostics.upstreamWarningSummary` |
| `--group-limit N` | per-capture `groups` rows | `groupSummary` |
| `--edge-limit N` | per-capture `edges` rows (each `count` keeps full multiplicity) | `edgeSummary` |
| `--comparison-limit N` | paired-group `comparisons` rows | `comparisonSummary` |
| `--observation-limit N` | rows of each enabled observation gate | `observationSummary` |
| `--operation-diff-limit N` | `diff.operations.added` / `removed`, independently | `diff.operations.operationDiffSummary` |
| `--witness-source-limit N` | each side of witness source and relation lists (needs `--witnesses`) | `sourceSummary` |
| `--witness-unresolved-limit N` | unresolved witness entries per capture, with exact reason counts (needs `--witnesses`) | `unresolvedSummary` |
| `--witness-parent-limit N` | parent IDs per listed unresolved entry (needs `--witnesses`) | `parentSummary` |

The value is parsed as strict JSON: `2`, `2.0` and `2e0` are accepted; `0`, `1.5`,
`"2"`, `0x2`, `null` and `9007199254740992` are exit-2 errors. Omitting a cap keeps the
report shape exactly as without it.

## Supported input and projection

- **OTLP JSON**: one object with `resourceSpans` -> `scopeSpans` -> `spans`. Each
  resource needs a nonempty string `service.name`. IDs are nonzero hex (32 trace / 16
  span characters); an absent or empty `parentSpanId` marks a root. `kind` is an
  integer 0..5 (missing means unspecified); status code 2 means error. Start/end times
  are unsigned decimal nanoseconds (strings recommended).
- **Jaeger query JSON**: `{"data": [{"traceID", "spans", "processes"}]}`. Trace IDs
  are 16 or 32 hex characters (64-bit IDs are zero-extended), span IDs 16. `span.kind`
  string tags map to the OTLP kinds; boolean `error` tags determine errors. CHILD_OF
  references become parent edges (including across traces); FOLLOWS_FROM is listed as
  not compared. `startTime` and `duration` are unsigned integer microseconds.

Numeric timestamps must be safe integers; larger values must be decimal strings.
Durations are exact integer nanoseconds up to 9007199254740991 ns. Every percentile is
**nearest rank** over sorted durations, `x[ceil(p*n)-1]` (the median of an even sample
is the lower middle value). Output milliseconds are IEEE-754 numbers.

Exact duplicate spans (same trace and span ID) are removed only when the span and its
resource/scope or process context are identical ignoring key order; any other
difference under the same identity is a conflict (exit 2). Omitted versus explicitly
empty `scope` or `schemaUrl` counts as a difference. Cyclic span graphs are rejected;
missing parents are reported as incomplete-capture diagnostics.

This is a comparison projection, not a lossless converter. Resource attributes other
than `service.name`, scope and schema URLs, span attributes/tags other than kind and
error, events/logs, links, flags, status messages and Jaeger pagination fields are not
compared; populated ones are counted under `diagnostics.omittedFields`. Unknown fields,
duplicate JSON keys, malformed metadata, empty corpora, partial Jaeger error responses,
NDJSON, protobuf and legacy `instrumentationLibrarySpans` are rejected.

## Report and Python API

The JSON report (`schemaVersion: 1`) contains `policy`, `regression`, `gate`, optional
`observationGates` and `structuralWitnesses`, `normalization`, per-capture
`baseline`/`candidate` statistics and diagnostics, `diff.operations`, `diff.edges` and
`comparisons`. Field order and number formatting are deterministic, and identical to
the reports of the earlier standalone `tracediff` tool.

```python
from pathlib import Path

from otelwrench.tracediff import compare, parse_json, render_text

baseline = parse_json(Path("examples/tracediff/baseline.json").read_text(encoding="utf-8"))
candidate = parse_json(Path("examples/tracediff/compatible.json").read_text(encoding="utf-8"))
report = compare(baseline, candidate, {"minSamples": 20})
print(report["gate"]["failed"])  # False
print(render_text(report), end="")
```

Use `parse_json` for untrusted input so duplicate keys are rejected. `compare` raises
`InputError` for unsupported input; `normalize` and `validate_policy` are also exported.
Pass witnesses and caps as a fourth argument using the report's field names, for
example `compare(a, b, {}, {"witnesses": True, "witnessSourceLimit": 2})`; every cap
must be a positive integer.

## Reproducible experiment

[`tracediff/EXPERIMENT.md`](tracediff/EXPERIMENT.md) describes the synthetic
corpus-mutation experiment behind
[`examples/tracediff/experiment-results.json`](../examples/tracediff/experiment-results.json):
raw JSON equality flags all three candidates as changed, while the normalized comparison
passes the ID/order/format-only captures and flags exactly one latency regression and
two structural changes in the mutated one.

## Limits

Both corpora and their sorted duration arrays are held in memory; there is no streaming.
There is no live Jaeger/Tempo query, UI, critical-path analysis, workload alignment or
clock correction, and no claim of statistical significance. A passing budget can still
hide incomplete captures or insufficient samples, so check `diagnostics` and
`eligible`.
