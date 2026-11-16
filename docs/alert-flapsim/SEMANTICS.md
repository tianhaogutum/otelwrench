# Supported replay semantics

This is an original implementation of a narrow offline model. It operates on
numeric values already returned by a captured query, then applies one explicit
threshold comparison. It does not parse or evaluate that query's PromQL. For
example, an existing `bool` comparison may return zeros and ones, but **presence
alone does not activate this model**: the configured numeric comparison still
applies to each value. Prometheus alert rules ordinarily activate on expression
vector presence; importing a filtered expression requires choosing a comparison
appropriate to those captured values.

The behavioral reference is the [Prometheus alerting rules documentation](https://github.com/prometheus/prometheus/blob/main/docs/configuration/alerting_rules.md),
as retrieved on 2026-11-11 (SHA-256
`92085dedf36ae2a9f16aa008931379815397862a9aee835f888ce087a741b7b5`).
It documents continuous pending activation for `for` and retention of firing
with `keep_firing_for`. The explicit discrete timer boundaries below are this
tool's supported contract, pinned by boundary tests. They do not claim parity
with every Prometheus version, rule option, restart or evaluation failure mode.
No upstream rule-evaluation source code is copied.

Without a checkpoint, for every known exact label set, initialize `state=inactive`, `pendingSince=null`
and `holdSince=null`. Process all evaluation timestamps in increasing order.
Each transition and timeline state describes the state **after** that evaluation.

| Previous state | Condition | Action |
| --- | --- | --- |
| inactive | false | Stay inactive. |
| inactive | true | Set pendingSince=t, enter pending; fire immediately if for=0. |
| pending | true | Fire when t-pendingSince >= for; otherwise stay pending. |
| pending | false | Enter inactive and clear pendingSince, regardless of hold. |
| firing | true | Stay firing, clear holdSince; do not repeat the for wait. |
| firing | false | Set holdSince=t if unset. Stay firing while t-holdSince < hold; otherwise resolve and clear both timers. |

With 60-second evaluations, firing at t=0 followed by false at t=60, 120, 180
and a 120-second hold resolves at **t=180**. The hold clock starts at the first
false evaluation (t=60). If true resumes at t=180, that evaluation instead clears
the timer and continues the existing episode. A later false starts a fresh hold.
A duration that is not a multiple of the interval reaches its boundary at the
first subsequent evaluation. There is no intermediate evaluation at its nominal
expiry time.

Every supplied sample must be on the inclusive `start + i*evaluationInterval`
grid, with `end-start` an exact interval multiple. Intervals need not equal the
distance between successive supplied samples: multiples are gaps, not implicit
resampling. No sample is interpolated, carried forward, cropped or ignored.
Omitted `start/end` infer the shared extrema across all supplied series and are
reported as `rangeSource: "sample-extrema"`. Inference cannot discover absent
leading/trailing evaluations; provide explicit bounds from the saved request to
model them. Different series may cover different parts of the shared range.
An empty matrix with explicit bounds contains zero known series. An explicitly
present empty series has missing samples at every evaluation.

A missing sample is always condition=false under `missingData: "false"`, even
for `!=`; it is never numeric zero. Under `"error"`, any missing grid point in
any known series fails the entire command, with no partial output. This policy
also governs disappearance at the end of an explicitly bounded capture.

Float strings are parsed as IEEE-754 binary64. Their original strings remain in
the timeline, including large values and negative zero. `NaN` is present numeric
NaN, so `!=` is true and other operators false. It is **not** missing data under
either policy. `+Inf`, `Inf`, and `-Inf` compare numerically. JSON cannot preserve
a stale-NaN payload distinct from ordinary NaN; staleness here is only absence
at a grid point. A literal `stale` or `StaleNaN` value is rejected. This tool
cannot reconstruct scraping, upstream lookback, or the cause of missing data.

Canonical identity is a JSON array of label-name/value pairs sorted by UTF-16
code-unit order, independent of locale, source key order and series order. All
label names are nonempty strings and all label values are strings; no label-name
syntax validation or rewriting is imposed. Every supplied label, including
`__name__`, participates in identity. Duplicate canonical sets fail instead of
merging, even if their samples differ. Unlike Prometheus alert label creation,
metric names are retained and no rule labels, external labels or templates apply.

`timeline` contains every evaluated point, raw sample string or null, condition,
state, timer starts and reason. `transitions` only includes state changes; with
zero `for`, inactive goes directly to firing in the transition log. A firing
episode ends on resolution, or has `end: null, censored: true` at capture end.
There is no synthetic recovery or extra evaluation past the end.

`firingDurationSeconds` integrates post-evaluation firing state over the
half-open capture interval `[start,end)`. A firing state at t accounts for time
until the next grid point. The last evaluation accounts for zero additional
time. Duration includes hold time when the condition is false. Aggregate
duration is a sum of series-seconds, which can exceed elapsed wall-clock time.
`heldEvaluations` counts false evaluations that remain firing, including the
last one if held; it is a count, not a duration. `pendingSince` retains the
original activation start while firing, and resets on resolution.

`firstFiring` is an absolute Unix timestamp or null if no episode starts. The
aggregate is the earliest first firing across all series, not an average. A
compare report includes full baseline and candidate replays and per-series
deltas. All numeric deltas mean candidate minus baseline. First-firing delay is
null when either side never fires; aggregate delay compares earliest overall
firings, which may belong to different label sets. Compare requires identical
windows and intervals. Different threshold and missing-data policies are allowed,
but either side's validation error fails the whole comparison.

Limits are explicit: regular UTF-8 JSON files up to 64 MiB each, JSON nesting at
most 100 levels, timestamps round-trippable at safe integer milliseconds,
nonnegative single-unit ms/s/m/h/d duration strings of at most 64 characters
(positive interval), and at most 1,000,000 expanded series-evaluations per replay.
Integer-millisecond aggregate exposure must also fit a safe integer. Invalid
JSON, duplicate JSON keys (including escaped spellings), unsorted/duplicate
timestamps, off-grid/out-of-range samples, native histograms, unsupported result
types, unknown fields, PromQL and regex operators all fail explicitly. No partial
report is emitted. These bounds are validation limits, not throughput guarantees.
The implementation holds the parsed capture and result in memory.

## Checkpoints

`otelwrench.flapsim.replay(matrix, config, checkpoint)` emits a `checkpoint` object in each report.
The CLI `checkpoint MATRIX --config CONFIG [--checkpoint-in STATE]` emits only
that object. `replay ... --checkpoint-in STATE` resumes it. Files use the same
strict UTF-8, duplicate-key and size validation as captures and configuration.
Compare emits the final checkpoints of its two cold replays but does not accept
an initial checkpoint.

Checkpoint schema version 1 has `kind: "alert-flapsim/checkpoint"`, `config`
(the five required alert settings, durations normalized to integer-ms strings),
`originStart`, `evaluatedThrough` (Unix seconds), and `series`. Each series entry
has `labels`, `state`, `pendingSince`, `holdSince` and `episodeStart`; timers are
Unix seconds or null. State is after `evaluatedThrough`, not before it. Neither
timelines nor historical summaries are stored. The first origin is retained
across successive checkpoints.

Import requires explicit new `start/end`. The first new evaluation must be
exactly one evaluation interval after `evaluatedThrough`; overlap, skipped
evaluations and shifted grid phases fail. The normalized evaluation interval,
threshold operator/value, for duration, hold duration and missing-data policy
must all match. Equivalent duration spellings are accepted. Origin, end and
timers must have exact millisecond precision on the same grid; the total chain
span must fit safe integer milliseconds. Series and label key order may differ,
but the complete canonical label-set collection must match exactly. No new
series is silently initialized, and no omitted series is silently forgotten.
To represent an absent known series, explicitly include its labels with empty
`values`; the configured missing-data policy then applies at every evaluation.

State validation rejects unknown fields/versions and duplicate identities.
Inactive state requires all timers null. Pending requires only `pendingSince`,
with elapsed time strictly less than `for`. Firing requires pending and episode
starts, with firing at the first grid evaluation where `for` elapsed. An optional
hold must start after firing began and have elapsed time strictly less than
`keep_firing_for`. All timers must lie within the known capture. These checks
validate internal consistency, not historical authenticity: a manually supplied
plausible checkpoint is an explicit assertion, not proof of prior samples.
No pre-origin state, upstream lookback, Prometheus restart recovery or live rule
state is reconstructed. Cold replay still assumes inactive and states explicitly
that pre-capture state is unknown.

Concatenating successive windows' timelines and transition logs gives the exact
single-window result. Each evaluation occurs once, including the first resumed
one; a checkpoint never re-evaluates its final sample. Pending activation and
hold timers retain their original starts. Zero-length (one evaluation) windows
and any number of contiguous resumptions follow the same rule.

Summaries remain local to `[start,end)`; they are **not cumulative**. Inherited
firing episodes retain their known original `start`, have `continued: true`,
and clip `durationSeconds` to this window. `firstFiring` includes an inherited
episode's known start; `firingEpisodes` includes that continued episode even if
it resolves on the first new evaluation (zero local duration). Historical closed
episodes and earlier first firings are not carried in checkpoints. Counts and
durations of separate reports should not be summed to reconstruct a full report:
continued episodes would be counted twice, and the interval between the first
window's end and the next window's start is outside both local `[start,end)`
exposures. Use the concatenated timeline over the combined range to calculate
whole-capture metrics. No extra exposure is added to a censored first report.

## Joining saved reports

`otelwrench.flapsim.join([report1, report2, ...])` and CLI `otelwrench alert-flapsim join REPORT1 REPORT2 [REPORT3 ...]`
accept 2–256 complete schema-version-1 replay reports in chronological order.
The first must start at its checkpoint's `originStart`; a resumed suffix alone
cannot recover historical closed episodes and fails. Subsequent reports must
have explicit config bounds and begin exactly one evaluation interval after
the preceding end. All normalized rule settings, complete canonical label sets
and checkpoint origins must match. Reversed order, gaps, overlapping/shared
endpoints, duplicate windows, independent cold restarts and missing history
fail. Join does not sort windows or fill absent reports.

Input reports retain the full current replay schema, including assumptions,
raw timeline samples, derived fields and final checkpoint. Each timeline must
have exactly one point at every inclusive window evaluation in order. The
implementation reconstructs the local matrix from raw strings (`null` means
missing), replays each window from the preceding verified checkpoint, and
checks the entire saved report against that result. Conditions, sample kinds,
state, timers, reasons, IDs, transitions, episodes, local/aggregate summaries
and checkpoint schema/state must agree. Unknown/missing fields and malformed
values fail. Object/label-key order and series/checkpoint-series order may
differ. First-window inferred bounds are allowed only when replaying its
samples actually yields those extrema. A later inferred window fails.

After validation, concatenate the raw timelines on the shared grid and replay
from the origin. This is equivalent to the concatenated verified states and
timer history, including pending and hold timers that cross any boundary.
No local counts, episodes or durations are summed. The interval from a prior
window's end to the next window's start belongs to the combined `[start,end)`
capture: its exposure follows the post-evaluation state at the prior end.
It is counted once, including when the next evaluation resolves the alert.
One-evaluation windows have zero local exposure but can contribute the full
boundary interval when joined. Resolved episodes retain their true full
endpoints, continued episodes merge, and only the final open episode is
censored. First firing includes all supplied history. The final evaluation
adds no exposure. Missing/NaN/held counts count each evaluation once.

Output uses the ordinary replay schema with `command: "replay"`, explicit
combined bounds (`rangeSource: "config"`), canonical series order and the final
checkpoint. It can be saved and joined with subsequent reports as an origin
prefix. Source warnings/infos are concatenated in supplied report order,
including duplicates. Assumptions describe the cold replay from the known
origin; pre-origin state remains unknown. No extra provenance fields are added.

At most 1,000,000 combined series-evaluations are accepted, charging each empty
matrix window for its evaluation count. The combined span and aggregate
millisecond exposure must fit safe integers. These bounds are checked before
alert replay; CLI files retain the usual 64 MiB/UTF-8/JSON limits. All inputs
and reconstructed samples/replays remain in memory; no runtime or memory
performance guarantee is implied. Failures emit no partial CLI stdout and do
not modify input files. This validates internal semantic continuity, not
historical authenticity or live Prometheus state. Plausible coordinated edits
to a report's samples and derived fields are not detectable. Standalone
checkpoints and compact grid/compare wrappers are not replay reports.

## Bounded timer grid

`otelwrench.flapsim.grid(matrix, baseline_config, timer_grid)` and CLI
`otelwrench alert-flapsim grid MATRIX --baseline BASE --grid TIMERS` compare explicit `for` and
`keep_firing_for` arrays over the Cartesian product. The baseline is a normal
complete replay configuration. Only its two timers change; each candidate uses
the same threshold, interval, window, exact labels and missing-data policy.
Every run starts inactive. Grid accepts no initial checkpoint and does not
infer pre-capture state.

The grid object requires exactly `for`, `keep_firing_for` and `maxEvaluations`.
Each axis is nonempty and contains valid single-unit duration strings with no
normalized duplicates, including equivalent spellings such as `1m` and `60s`.
The product is at most 256 combinations. Candidate order follows the input,
with `for` outermost and `keep_firing_for` innermost; no sorting, ranking or
selection occurs. The baseline is evaluated separately even when its timer pair
also appears as a candidate.

`maxEvaluations` is a required integer in [1,1000000]. Before running any alert
state, the implementation validates the baseline capture/grid and checks
`(combinations + 1) * window.evaluations * max(1, known series)` against the
budget. The baseline consumes the extra run, missing grid points consume work,
and an empty matrix is still charged for its window. All ordinary replay range,
sample and safe integer exposure limits also apply. The report's `budget`
records requested cost and limits. These are validation limits, not a memory
size, runtime bound or throughput claim; parsed samples and results are held
in memory. Any invalid grid, excessive cost or missing-data error fails the
entire command with no partial stdout. There is no truncation or sampling of
combinations or evaluations.

Reports contain compact baseline/candidate configurations, summaries and exact
per-label transitions/episodes; expanded timelines and checkpoints are omitted.
`transitionCount` counts all state changes, including pending activation/reset.
An immediate inactive-to-firing change with zero `for` counts once, as in replay.
Per-series and aggregate deltas compare each candidate to the explicit baseline.
First-fire delay is the difference in observed first firing timestamps, null
if either side never fires. Aggregate first firing is the earliest across
series, so aggregate delay may compare different label sets. Firing duration
remains observed series-seconds in [start,end), and every open firing episode
is labeled `end: null, censored: true`. Final pending state is reported but is
not a firing episode. Warnings/infos are retained once at report level.

The shipped eight-evaluation synthetic fixture's four independently calculated
transition sequences and episode endpoints are in `tests/flapsim/support/__init__.py` (`EXPECTED_GRID`).
Repository tests and the installed-package check assert all combinations,
censoring, missing policy and exact budget accounting. Results are descriptive
tradeoffs within the supplied finite capture: no statistical inference,
production-performance claim or universal optimum is provided.
