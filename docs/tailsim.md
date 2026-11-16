# otelwrench tailsim

Tail-sampling policies are easy to get subtly wrong: a `drop` veto that also discards
error traces, a latency bound that is off by one millisecond, or a decision wait that
fires before the interesting span arrives. `otelwrench tailsim` replays a deterministic
subset of OpenTelemetry Collector tail-sampling policies against a saved, finite OTLP
trace corpus, offline, and explains every keep/drop decision. Use it before deploying
a policy change to see which traces change, how many spans and error traces are
retained, and how many serialized JSON bytes are kept.

It evaluates complete traces, or simulates a bounded arrival schedule against an
expected-span reference corpus. The Collector already provides runtime sampling
decisions and policy metrics; `otelwrench tailsim` evaluates a bounded static capture.
It is not a full Collector emulator or a production cost/throughput predictor.

## Quick start

Install with `pip install otelwrench` (Python 3.11+; PyYAML is installed as a core
dependency). The commands below use the synthetic samples under
[`examples/tailsim/`](../examples/tailsim/), so run them from a clone of the repository.

```sh
# Explain each complete-trace decision for one policy configuration
otelwrench tailsim replay examples/tailsim/traces.json --config examples/tailsim/policy.yaml

# Compare retention between a baseline and a candidate configuration
otelwrench tailsim compare examples/tailsim/traces.json \
  --baseline examples/tailsim/baseline.yaml --candidate examples/tailsim/policy.yaml

# Simulate a fixed decision wait over a bounded arrival schedule
otelwrench tailsim replay-arrivals examples/tailsim/arrival-order/traces.json \
  --arrivals examples/tailsim/arrival-order/arrivals.json \
  --config examples/tailsim/arrival-order/policy.yaml --decision-wait-ms 100
```

Subcommands:

| Subcommand | Purpose |
| --- | --- |
| `replay CORPUS --config FILE` | Explain each complete-trace decision |
| `compare CORPUS --baseline FILE --candidate FILE` | Compare finite-corpus retention for two configurations |
| `replay-arrivals CORPUS --config FILE --arrivals FILE --decision-wait-ms N` | Simulate a bounded first-arrival decision-wait model |
| `compare-arrivals CORPUS --config FILE --arrivals FILE --baseline-wait-ms N --candidate-wait-ms N` | Compare two waits on one arrival schedule |
| `verify-replay`, `verify-compare`, `verify-arrivals`, `verify-compare-arrivals` | Check a saved report's input fingerprints against supplied files |
| `verify-replay-evidence`, `verify-compare-evidence`, `verify-arrivals-evidence`, `verify-compare-arrivals-evidence` | Regenerate a saved report locally and compare every field |

The verification commands are documented in [Verifying saved reports](tailsim/verification.md).
Run `otelwrench tailsim <subcommand> --help` for the flags; `otelwrench tailsim --version`
prints `otelwrench tailsim 0.1.0`.

## Running and exit codes

The tool never uses the network, credentials, subprocesses, or a server.
Replay prints one JSON report to stdout. Invalid or unsupported input returns
exit code 2 with an error on stderr (prefixed `otelwrench tailsim: `) and **no report
on stdout**. No input files are changed. There is no executable dry-run configuration:
YAML is data only, loaded safely; custom tags, aliases, and duplicate mapping keys are
rejected. Use ordinary shell redirection if you want to save a report (the shell may
create/truncate the target even if validation fails).

The supplied synthetic example contains 10 ms OK and 600 ms ERROR traces.
`examples/tailsim/policy.yaml` samples ERROR **or** latency >500 ms. Actual result:
1/2 traces and 1/2 spans retained, 1/1 error traces retained. The slow trace
explains both `errors` and `slow`. Canonical serialized JSON shrinks from 738
to 465 bytes; this is a finite-fixture observation, not a production forecast.

## Supported corpus

One UTF-8 OTLP/HTTP JSON `ExportTraceServiceRequest` object containing
`resourceSpans` → `scopeSpans` → `spans`. Merge captured requests into that
structure before use. Binary protobuf, gzip, JSONL, arrays of requests,
legacy `instrumentationLibrarySpans`, and non-OTLP shorthand are unsupported
and fail explicitly. The input must fit in memory.

- Spans require nonzero 32-hex-digit `traceId`, nonzero 16-hex-digit `spanId`,
  and unsigned 64-bit `startTimeUnixNano` / `endTimeUnixNano` as integers or
  decimal strings. IDs group case-insensitively. Reversed intervals fail.
  Missing/empty `parentSpanId` is accepted; nonempty parents/links must have
  valid nonzero IDs. Remote parents need not appear in the capture.
- Status codes are OTLP integers: 0 UNSET, 1 OK, 2 ERROR. An omitted status/code
  means UNSET. Unknown enums and symbolic enum strings fail. At least one ERROR
  span makes the trace an error, independently of its root's status.
- Duration is **max(end) − min(start)** over all spans with the same trace ID,
  across resources and scopes. Zero timestamps/zero-length spans are accepted.
- Resource, scope, span, event, and link metadata/attributes are validated and
  preserved for byte measurement. Standard AnyValue scalars, arrays, and
  key-value lists are supported; nested attributes are limited to 64 levels.
  Nonfinite doubles, null values, duplicate attribute keys, unknown fields,
  nonstandard/malformed base64 bytes (only the standard alphabet is accepted), and all repeated span identities (even identical
  duplicates) fail. No silent deduplication occurs.

Completeness is an **assumption supplied by the caller**; no file can prove
that all spans were captured. Source `dropped*Count` fields are retained, but
missing source attributes/events/spans cannot be recovered or modeled.

## Supported configuration and semantics

The configuration must contain exactly `processors.tail_sampling.policies`.
A full Collector configuration must first be reduced to that subtree. Unknown
fields anywhere, other processor names, runtime settings (`decision_wait`,
`num_traces`, caches, sampling strategies, `sample_on_first_match`, etc.),
and unsupported policies fail before report output. Nothing is ignored.

```yaml
processors:
  tail_sampling:
    policies:
      - name: errors
        type: status_code
        status_code: {status_codes: [ERROR]}
      - name: slow
        type: latency
        latency: {threshold_ms: 500}
```

| Policy | Accepted configuration | Match condition |
| --- | --- | --- |
| `always_sample` | No body, or `always_sample: {}` | Every trace |
| `status_code` | `status_code: {status_codes: [OK, ERROR, UNSET]}` | Any span has any listed status |
| `latency` | `latency: {threshold_ms: 500, upper_threshold_ms: 1000}` | `floor(duration_ns / 1_000_000) > threshold_ms`; nonzero upper bound is inclusive |
| `string_attribute` | `string_attribute: {key: service.name, values: [api]}` | Exact, case-sensitive string match in any resource or span |
| `and` | `and: {and_sub_policy: [<policies>]}` | All children match |
| `not` | `not: {not_sub_policy: <one policy>}` | Opposite of child match |
| `drop` | `drop: {drop_sub_policy: [<policies>]}` | All children match, producing an explicit drop veto |

Every policy needs a nonempty name and type. Names must be unique among
siblings. Lists of policies/status codes/attribute values must be nonempty.
Thresholds are nonnegative signed-64-bit integers; a nonzero upper threshold
must exceed the lower threshold. Omitted/zero upper means unbounded. Matching
strings are never coerced from numeric/bool values. A literal `*` matches only
`*`; no glob/regex matching. Resource and span values both participate even
when the same key has different values. Scope/event/link attributes do not
participate in string matching.

Top-level policies may use all seven types. `and` / `drop` children may be
leaf policies or `not`; `not` wraps a leaf only, following the Collector
configuration shape. Arbitrary recursive nesting fails. `invert_match`, all
regex settings (even `enabled_regex_matching: false`), probabilistic,
composite, rate/byte limiting, OTTL, and every other type/option fail.

All top-level policies are evaluated so explanations include every match.
A matched **drop wins**, otherwise any sample vote keeps the entire trace;
otherwise it is dropped. A false `not` match is an ordinary non-sample vote,
not a global veto. Example: `always_sample` plus `not(always_sample)` still
samples. Explanation children show intermediate votes and reasons; top-level
`matched_policy_names` includes explicit drop matches as well as sample votes.

The [upstream latency implementation](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/tailsamplingprocessor/internal/sampling/latency.go)
uses integer-millisecond truncation and a strict lower bound. `otelwrench tailsim` applies
those boundaries to the **final complete-trace duration**, even with an upper
bound. It does not reproduce the runtime's span-by-span early matches. This
intentional static rule makes decisions invariant to input permutation.

## Reports and byte accounting

Static `replay` and `compare` reports include `fingerprints` with `version: 1`
and `algorithm: sha256`. Digests are 64 lowercase hexadecimal characters:

- `corpus_source_sha256` hashes the exact input UTF-8 bytes, including whitespace,
  key/array order, ID case, and line endings. It identifies the source file,
  not just its decisions or the filtered byte-accounting representation.
- Replay's `effective_config_sha256` hashes the normalized supported configuration.
  Compare labels the two hashes `baseline_effective_config_sha256` and
  `candidate_effective_config_sha256`; each embedded replay has its own matching
  `effective_config_sha256` and the same corpus hash. Swapping configurations
  swaps their labels, even if their decisions happen to be identical.

For fingerprint version 1, normalize the validated configuration back into the
`processors.tail_sampling.policies` shape shown above. Every policy includes its
type-specific body, including `always_sample: {}`. Insert
`upper_threshold_ms: 0` when omitted. Sort and deduplicate `status_codes` and
string-attribute `values` (exact Unicode strings, without case folding).
Recursively normalize children using the same rules. Preserve policy names,
top-level order, and child order because they are part of report explanations.
Hash compact, sorted-key UTF-8 JSON of that full configuration, with literal
Unicode, no ASCII escaping, and no trailing newline. YAML comments, formatting,
mapping order, equivalent defaults, and set-value order/duplicates therefore
do not affect the config hash. Names, policy/child order, and parameter changes
do. This is an identity of normalized report inputs, not proof that different
hashes imply different sampling decisions.

Only fully validated inputs produce a report; normalization never ignores an
unsupported option or repairs invalid data. Paths are not hashed or included.
Keep the corpus, configuration files, and otelwrench version with saved reports:
hashes verify inputs but cannot recover them or prove corpus completeness.
These are additive fields in report schema 1. Arrival replay additionally identifies
its normalized schedule and effective wait model as specified below.

Save a comparison with its input identities:

```sh
otelwrench tailsim compare examples/tailsim/traces.json \
  --baseline examples/tailsim/baseline.yaml --candidate examples/tailsim/policy.yaml > comparison.json
python -c 'import json; print(json.load(open("comparison.json"))["fingerprints"])'
```

Replay reports include every trace (sorted by normalized trace ID), duration,
observed statuses, error flag, span count, kept span count, final sample/drop,
matched policy names, and the complete policy explanation tree. The summary
counts all/kept traces and spans, error retention `{total, kept, ratio}`, and
serialized bytes. With no error traces, `ratio` is `null`, never NaN.

Compare embeds both replay reports plus all changed traces, newly sampled /
dropped counts, lost/gained error trace IDs, span delta, and byte delta
(**candidate minus baseline**). Decisions are compared, not just policy names.
Each `comparison.changed_traces` row is self-contained: `baseline_reason` /
`candidate_reason` explain the final decisions, and `baseline_policies` /
`candidate_policies` contain the exact evaluated explanation trees. These include
all matches **and nonmatches**, preserving names, types, votes, reasons, and
children in configuration order. This explains a no-match drop as well as a
drop veto over a sample vote without joining the embedded replay reports.
Traces with unchanged decisions are omitted even if their reasons differ.

Every static replay and comparison report includes `assumptions`: completeness is
required from the caller and is not verified; evaluation uses the complete
captured trace; `online_timing_or_cache_equivalence` and
`production_savings_estimated` are both `false`. The scope of byte accounting
also travels with the report. These are additive fields in report schema 1.

`serialized_total_bytes` and `serialized_kept_bytes` measure compact,
sorted-key UTF-8 JSON OTLP requests with no trailing newline, after filtering
the original resource/scope groups by selected trace ID. All supported fields
in nonempty groups remain unchanged; empty groups are removed. JSON whitespace
and key order are canonicalized. A fully dropped corpus is still the 20-byte
`{"resourceSpans":[]}` envelope. Shared context is counted once per original
group, so bytes are not additive per trace. `source_file_bytes` separately
reports the input UTF-8 size (including whitespace). These are **serialized
JSON bytes**, not protobuf, compression, transport, backend storage, or cost.
Grouping and textual number representation can affect bytes without affecting
decisions. The CLI produces a report rather than transformed telemetry.

## Check every supported policy on one corpus

```sh
otelwrench tailsim compare examples/tailsim/decision-matrix/traces.json \
  --baseline examples/tailsim/decision-matrix/baseline.yaml \
  --candidate examples/tailsim/decision-matrix/candidate.yaml
```

This local synthetic OTLP fixture has eight complete traces and nine spans.
Trace 2 has an OK root and an ERROR span in another resource group; its final
duration is 600 ms. Resource `service.name` and span `url.path` attributes
exercise both supported matching locations. All baseline decisions are
`sample` via `always_sample` policy `all`. The candidate matrix below gives
each top-level match (1/0) and the resulting decision:

| Trace suffix | errors | slow | api | non-health-errors (`and`) | never (`not`) | health-veto (`drop`) | Candidate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 0 | 0 | 1 | 0 | 0 | 0 | sample |
| 2 | 1 | 1 | 1 | 1 | 0 | 0 | sample |
| 3 | 0 | 1 | 0 | 0 | 0 | 1 | drop |
| 4 | 0 | 0 | 0 | 0 | 0 | 0 | drop |
| 5 | 1 | 0 | 0 | 0 | 0 | 0 | sample |
| 6 | 0 | 0 | 0 | 0 | 0 | 0 | drop |
| 7 | 0 | 1 | 0 | 0 | 0 | 0 | sample |
| 8 | 0 | 0 | 0 | 0 | 0 | 0 | drop |

Expected changed trace suffixes are **3, 4, 6, 8**. Trace 3 carries
`candidate_reason: drop_policy_precedence`, matching names
`[slow, health-veto]`, and both exact explanations: the latency evidence is
`duration floor(ms)=600; require >500 and <=700`; the veto's children show
`/health` matched and ERROR did not match. The other changed traces carry
`no_sample_policy_match` and preserve every failed policy explanation. Trace 6
is 500.999999 ms, trace 7 is 700.999999 ms, and trace 8 is 701 ms, exposing
integer truncation and both latency bounds. A false `never` vote does not veto
the sampled traces. Reversing the two configurations newly samples the same
four traces with their explanations reversed.

[`expected.json`](../examples/tailsim/decision-matrix/expected.json) is a hand-truthed
before/after oracle with every trace's decisions, top-level and child votes,
and exact leaf evidence strings. The regression suite expands its fixed tree
shape and checks the CLI output without deriving expected votes from the implementation.
An invalid candidate (for example `threshold_ms: -1`) must return 2 with
empty stdout, even when the baseline is valid. These are full-corpus static
fixtures, with no online timing/cache equivalence or production savings claim.

## Replay a bounded arrival schedule

```sh
otelwrench tailsim replay-arrivals examples/tailsim/arrival-order/traces.json \
  --arrivals examples/tailsim/arrival-order/arrivals.json \
  --config examples/tailsim/arrival-order/policy.yaml --decision-wait-ms 100
```

This is the **`first_arrival_fixed_wait_v1` synthetic model**, not Collector
runtime equivalence. Use the same supported policy-only YAML as static replay.
Collector `decision_wait`, `decision_cache`, `num_traces`,
`sample_on_first_match`, root timing, and all other runtime settings still fail
explicitly, even when set to false/zero/empty. The positive integer CLI wait
belongs only to this model; no Collector defaults or options are inferred.

The reference OTLP corpus defines every expected `(trace ID, span ID)` and its
original resource/scope context. A separate local JSON schedule supplies arrival
times, independently of OTLP start/end timestamps. For example, one trace with
an OK span and an ERROR span in the reference can have this schedule:

```json
{
  "schema_version": 1,
  "observation_end_unix_nano": "250000000",
  "arrivals": [
    {"at_unix_nano": "0", "trace_id": "00000000000000000000000000000001", "span_id": "0000000000000001"},
    {"at_unix_nano": "50000000", "trace_id": "00000000000000000000000000000001", "span_id": "0000000000000002"}
  ]
}
```

Times are unsigned 64-bit nanoseconds (integers or decimal strings) on one
caller-chosen clock. They may start at zero. The entire corpus and schedule
must fit in memory. Schedule array order is irrelevant: each trace is replayed
in timestamp order, with normalized IDs and sorted output. Equal-time arrivals
are simultaneous; their file order cannot affect a decision.

The model has these exact rules:

1. The first received span starts one timer at `arrival + wait`. An empty
   schedule starts no timers. A later span never resets a timer.
2. Evaluate all policies once, at that deadline, using only spans arriving
   **strictly before** it. Timers win ties against arrivals at the deadline.
   Latency uses the final duration of this subset, with the existing static
   integer-ms boundaries; there are no incremental early policy matches.
3. Freeze that decision. Discard **every** late span, including late spans of
   sampled traces. There is no cache, reopening, forwarding after a decision,
   eviction, capacity/throughput model, periodic tick, or root acceleration.
4. The observation cutoff is inclusive. An arrival at the cutoff is observed;
   a timer at the cutoff fires. Deadlines beyond it remain `pending`. Traces
   with no arrivals are `unobserved`. Neither receives a fabricated sample/drop
   decision or an EOF flush, even if all expected spans have already arrived.

The public five-trace/eight-span fixture, using an ERROR policy and a 100 ms
wait, has this hand-truthed outcome at the 250 ms cutoff:

| Trace suffix | Scheduled arrivals | State / decision | Full-trace reference | Evidence |
| --- | --- | --- | --- | --- |
| 1 | OK at 0 ms, ERROR at 50 ms | decided / sample at 100 ms | sample | Both spans on time |
| 2 | OK at 0 ms, ERROR at 150 ms | decided / drop at 100 ms | sample | ERROR is late and unseen at decision |
| 3 | OK at 0 ms; ERROR absent | decided / drop at 100 ms | sample | ERROR is missing from the schedule |
| 4 | ERROR at 200 ms | pending, deadline 300 ms | sample | Cutoff does not force a decision |
| 5 | No arrivals | unobserved | drop | Its one OK span is missing |

Expected summary: 3 decided, 1 pending, 1 unobserved; 1 late span and 2 missing
spans; 2 incomplete decisions; 1 sampled trace and 2 kept spans. Only decided
traces **2 and 3** differ from full-trace decisions. The hand-authored oracle is
[`examples/tailsim/arrival-order/expected.json`](../examples/tailsim/arrival-order/expected.json).

Each row contains `decision_snapshot` (the full explanation tree for the
on-time subset, or null), `full_trace` (the same policies applied to all reference
spans), and `decision_differs_from_full_trace` (null until decided). It lists
`on_time_span_ids`, `late_span_ids`, `missing_span_ids` (not scheduled by the
cutoff), and `unseen_at_decision_span_ids` (late plus missing, null until decided).
For pending traces, the on-time list contains spans already received before
their future deadline; no policy result is emitted. `kept_spans` counts only
on-time spans of decided/sample traces; late and pending spans never count.
The changed-decision summary excludes unresolved traces and does not imply
that sampled traces retained all their reference spans.

**Missing and incomplete are relative to the reference inventory only.** A
parent ID absent from that inventory is not inferred missing; real capture
loss cannot be detected. Full-trace reference decisions are counterfactual
when spans never arrived. Reports attach these assumptions and explicitly set
`online_timing_or_cache_equivalence: false` and
`production_savings_estimated: false`. Arrival mode makes no byte/cost estimate.
This bounded model does not claim agreement with any Collector version.

Unknown/duplicate span arrivals, unsupported/duplicate fields, invalid IDs or
timestamps, arrivals beyond the cutoff, zero/negative/fractional waits, and
unsigned-64-bit deadline overflow all fail with exit 2, stderr explanation,
and **empty stdout**. To reproduce a failure, add `decision_wait: 100ms` under
`processors.tail_sampling` in the example policy, or change an arrival to
`"250000001"` (beyond the example cutoff). No partial report is emitted.

Arrival replay reports include `fingerprints` with `version: 1`, `algorithm:
sha256`, `corpus_source_sha256`, `effective_config_sha256`,
`arrival_schedule_sha256`, and `effective_arrival_model_sha256`. The corpus and
policy identities use **exactly** the static replay contracts above, including
the original reference corpus bytes (never the deadline-filtered snapshot).

For `arrival_schedule_sha256`, construct this full preimage from the validated
schedule: `{"schema_version":1,"observation_end_unix_nano":<integer cutoff>,
"arrivals":[{"at_unix_nano":<integer time>,"trace_id":<lowercase ID>,
"span_id":<lowercase ID>},...]}`. Sort rows by the numeric `at_unix_nano`, then
lowercase `trace_id`, then lowercase `span_id`. Include all scheduled rows,
even late or pending arrivals, and the cutoff even when it changes no decisions.
Integer/decimal-string time encodings (including leading zeros), ID case,
whitespace, mapping order and arrival-array order normalize identically.
Changing a timestamp, cutoff or scheduled identity changes this identity.

For `effective_arrival_model_sha256`, the entire preimage is
`{"name":"first_arrival_fixed_wait_v1","decision_wait_ms":<validated integer>}`.
A changed wait changes this hash even if all decisions stay equal. The cutoff
belongs only to the schedule hash. Both preimages use compact, sorted-key,
literal-Unicode UTF-8 JSON without a trailing newline, exactly as for the
normalized policy hash. No paths, explanation text or derived deadlines enter
these preimages. Only fully validated inputs yield a report; malformed schedules,
unknown/duplicate spans, invalid waits and overflow still return 2 with empty
stdout. Fingerprints do not change deadline/cutoff/late/pending semantics.
Retain corpus/config/schedule files, wait and otelwrench version: hashes cannot
recover inputs, prove capture completeness or establish Collector equivalence.

## Compare two waits on one arrival schedule

```sh
otelwrench tailsim compare-arrivals examples/tailsim/arrival-order/traces.json \
  --arrivals examples/tailsim/arrival-order/arrivals.json \
  --config examples/tailsim/arrival-order/policy.yaml \
  --baseline-wait-ms 100 --candidate-wait-ms 200
```

`compare-arrivals` takes exactly one reference corpus, version-1 schedule and
supported policy-only config, shared by both waits. Each wait is a required
positive integer in milliseconds, at most `18446744073709`; equal waits are
valid. The corpus and schedule are validated once, and both runs use the same
validated span inventory and arrivals. The synthetic timing, cutoff, late-span
and missing-span rules above apply unchanged. Longer waits need not improve
retention: more spans can trigger a drop veto, or a deadline can pass the cutoff.

The JSON report has `schema_version: 1`,
`mode: arrival_order_offline_comparison`, and `baseline` / `candidate` containing
complete arrival replay reports (identical to running `replay-arrivals` with
the corresponding wait). `comparison` contains:

- `both_decided_trace_count`, `changed_decision_count`, and `changed_decisions`:
  compare sample/drop **only where both sides are decided**. Each changed row
  has `trace_id` and complete `baseline` / `candidate` trace rows, including
  snapshots, every policy explanation, deadlines and on-time/late/missing IDs.
- `newly_sampled_traces` and `newly_dropped_traces`: counts of drop→sample and
  sample→drop flips within that same both-decided population. A pending→sample
  transition is a state change, not a newly sampled finalized decision.
- `state_change_count` and `state_changes`: every trace whose state differs,
  with the same complete before/after row shape. With one shared schedule,
  unobserved traces remain unobserved; decided↔pending are the possible changes.
- `unresolved_trace_count` and `unresolved_traces`: every trace unresolved on
  either side, including unchanged pending/pending and unobserved/unobserved
  pairs. Each row has `trace_id`, `baseline_state`, `candidate_state`,
  `baseline_decision` and `candidate_decision`; undecided decisions are `null`.
- `kept_span_delta`: candidate minus baseline finalized kept spans, across all
  traces. This can change through state resolution or span-subset differences
  even with no sample/drop flips. It is not a savings or byte estimate.

All lists sort by normalized trace ID. Equal waits yield identical embedded
reports, zero flips/state changes/delta, and still list unresolved traces.
Reversing waits swaps evidence, reverses flips/transitions and negates the span
delta. In the public 100→200 ms example, only trace 2 flips drop→sample; traces
4 (pending/pending) and 5 (unobserved/unobserved) remain unresolved. There are
no state changes and the kept-span delta is +2. At 100→50 ms, trace 1 instead
flips sample→drop (its ERROR arrives exactly at the 50 ms deadline), trace 4
changes pending→decided/sample at the inclusive 250 ms cutoff, and trace 5
remains unobserved. The delta is −1, with exactly one finalized flip.

Invalid corpus/config/schedule or **either** invalid wait/deadline returns 2,
an explanation on stderr and empty stdout, even if the other wait succeeds.
Missing files/required arguments, unknown fields, duplicate arrivals and every
unsupported Collector setting fail too. No partial baseline report is printed.
Empty corpora/schedules are valid. Both the top-level and embedded reports
carry the synthetic-model assumptions: no Collector timing/cache equivalence,
no verified capture completeness, no production savings and no byte estimate.
Each embedded arrival replay includes its own fingerprint block, identical to
the corresponding single-wait report. The comparison wrapper also includes
`fingerprints` with exactly these fields:

- `version: 1` and `algorithm: sha256`.
- `corpus_source_sha256`, `effective_config_sha256`, and
  `arrival_schedule_sha256`: the shared identities, equal to both embedded
  reports. Corpus/config hashes also agree with static replay on the same files.
- `baseline_effective_arrival_model_sha256` and
  `candidate_effective_arrival_model_sha256`: each side's embedded
  `effective_arrival_model_sha256`, labelled by its wait argument.

These fields reuse the exact corpus/config/schedule/model preimages specified
above; the wrapper adds no new preimage or hash of a report. In particular, the
schedule includes its version, integer cutoff and all normalized sorted rows,
and each model hashes only its name and validated integer wait. Equal waits
yield equal labelled model hashes; reversing waits swaps them, even when
decisions stay equal. Equivalent schedule encodings preserve the shared hash.
Changed schedule times/cutoff/identities change that hash independently of the
waits. No unlabelled `effective_arrival_model_sha256` appears at the top level.
All provenance is additive: invalid inputs or overflow on either side still
fail before any report is printed. Retain corpus/config/schedule files, waits
and otelwrench version.


## Verifying saved reports

`verify-replay`, `verify-compare`, `verify-arrivals` and `verify-compare-arrivals` check a
saved report's declared input fingerprints; `verify-replay-evidence`,
`verify-compare-evidence`, `verify-arrivals-evidence` and `verify-compare-arrivals-evidence`
regenerate the report from the supplied files and compare every field, optionally listing
mismatch paths with `--diagnostic-limit N`. See [Verifying saved reports](tailsim/verification.md).

## Reproduce the retention experiment

```sh
python benchmarks/tailsim/run.py
```

Run from the repository root. This generates 100 synthetic complete traces (200 spans),
invokes the CLI for an always-sample baseline and two candidate configurations, and
independently verifies every result using standard-library arithmetic and JSON filtering.
The deterministic fixture, observed results, and interpretation are checked in under
[`benchmarks/tailsim/`](../benchmarks/tailsim/README.md). The health-drop candidate
intentionally exposes an error-retention regression. `examples/tailsim/drop.yaml` and
`examples/tailsim/and-not.yaml` also provide runnable composition examples.

## Limitations

No decision cache, trace eviction, root acceleration, throughput, statistical
hashing, or Collector feature gates. Timing/late/missing spans are supported
only by the explicitly bounded arrival model above. No production gain is claimed.
Synthetic captures and small-corpus experiments measure retention only.

## Test coverage

The suite under `tests/tailsim/` covers hand-truthed decisions, equality boundaries,
multispan aggregation, drop precedence, and/not, input permutation, independent
end-to-end byte accounting, self-contained before/after evidence for all seven supported
policy types, report assumptions, malformed/unsupported inputs, and CLI failure with
empty stdout. Arrival regressions cover the fixed oracle, deadline/cutoff ties,
late discard after sampling, pending/unobserved traces, cross-resource subset
policies, permutation/case invariance, malformed schedules, and explicit
runtime-setting rejection. Paired-wait regressions cover finalized flips versus
state changes, unchanged unresolved pairs, equal/reversed waits, deadline ties,
cutoff resolution, drop vetoes with longer waits, span deltas without decision
flips, empty inputs, input permutation and atomic failures on either side.

Fingerprint regressions independently check SHA-256 preimages, equivalent
YAML/defaults/sets, exact UTF-8/CRLF corpus identity, name/order/parameter changes,
comparison direction, saved reports, and validation failures with no partial report.
Arrival provenance tests additionally verify full normalized schedules (including
cutoff, late and pending rows), all tie permutations, equivalent time encodings/ID
case, model identity independent of decisions, static identity reuse, saved CLI output
and uint64 boundaries.

Saved-report verification regressions cover independent hash checks, isolated and
combined mismatches (all 7 static-comparison and all 13 arrival-comparison
wrapper/embedded digests), normalized config/schedule encodings, labelled config and
wait direction, malformed metadata on either side, deadline overflow, opaque evidence
for the provenance-only commands and atomic input failures. Evidence regressions compare
every top-level field against local regeneration, with independent finite oracles for
sampled/dropped/pending/unobserved traces, strict JSON types and array order, nonfinite
overflow, and changed supplied inputs. Optional diagnostics tests cover value-free
paths, missing/extra keys and array items, strict types, slash/tilde/empty/Unicode and
escaped-surrogate member names, numeric array index order, deterministic mapping order,
exact fit versus truncation at limits 1 and 1000, unchanged default bytes, and atomic
rejection of invalid, duplicate or abbreviated diagnostic options. Saved evidence
cannot select the supplied replay policy.

`tests/tailsim/` honours `TAILSIM_TEST_CLI` to run selected CLI tests against an
alternative executable.

## License

Apache-2.0. Upstream references and attribution are listed in [THIRD_PARTY.md](../THIRD_PARTY.md).
