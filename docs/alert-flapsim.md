# otelwrench alert-flapsim

Alerts that flap (fire, resolve, fire again) page people for noise, while long
`for` / `keep_firing_for` timers delay real pages. `otelwrench alert-flapsim`
replays alert states offline over a saved Prometheus `query_range` matrix, so you
can see every evaluation, transition and episode for each exact label set and
compare explicit threshold and timer choices before changing a rule. Use it when
tuning an alert against a captured incident or a noisy week, with no server,
network access or live Prometheus.

All inputs are local JSON files; output is deterministic JSON (or CSV) on stdout.
The sample inputs live in [`examples/alert-flapsim/`](../examples/alert-flapsim/);
run the commands below from a clone of the repository.

## Quick start

```sh
# One rule over one capture: timeline, transitions, episodes, summary, checkpoint.
otelwrench alert-flapsim replay examples/alert-flapsim/query-range.json \
  --config examples/alert-flapsim/alert.json

# Two explicit rules over the same capture, with candidate-minus-baseline deltas.
otelwrench alert-flapsim compare examples/alert-flapsim/jitter-query-range.json \
  --baseline examples/alert-flapsim/baseline.json \
  --candidate examples/alert-flapsim/candidate.json --format csv

# A bounded Cartesian grid of for/keep_firing_for values against a baseline.
otelwrench alert-flapsim grid examples/alert-flapsim/grid-query-range.json \
  --baseline examples/alert-flapsim/grid-baseline.json \
  --grid examples/alert-flapsim/timer-grid.json
```

`otelwrench alert-flapsim --help` prints the full interface; `otelwrench
alert-flapsim <command> --help` prints one command's usage.

| Command | Purpose |
| --- | --- |
| `replay MATRIX.json --config ALERT.json [--checkpoint-in STATE.json]` | Replay one rule; the report includes a final checkpoint. |
| `checkpoint MATRIX.json --config ALERT.json [--checkpoint-in STATE.json]` | Same replay, emitting only the final checkpoint. |
| `compare MATRIX.json --baseline BASE.json --candidate CANDIDATE.json [--format json\|csv]` | Two rules on one capture and grid. |
| `grid MATRIX.json --baseline BASE.json --grid TIMERS.json [--format json\|csv]` | Explicit timer grid; no ranking or optimum. |
| `join REPORT1.json REPORT2.json [...]` | Verify 2..256 contiguous checkpoint-chained reports and replay the whole capture. |
| `compare-reports BASELINE.json CANDIDATE.json [--format json\|csv]` | Compare two saved whole-capture replay reports. |
| `compare-report-set BASELINE.json CANDIDATE1.json [...] [--format json\|csv] [--detail full\|summary] [--names NAMES.json] [--output PATH]` | One baseline and 1..255 saved candidates. |
| `compare-report-set --manifest SET.json [...]` | Same, with report paths listed in a manifest. |

Every failure exits 1 with one `otelwrench alert-flapsim: ...` line on stderr and
an empty stdout (never a partial report or CSV header). Success exits 0.

## Inputs

**Matrix**: a saved Prometheus HTTP API `query_range` success response with
`resultType: "matrix"`. Sample values are Prometheus numeric strings (`"2"`,
`"NaN"`, `"+Inf"`, `"-Inf"`); timestamps are Unix seconds with exact millisecond
precision. `warnings`/`infos` are retained in reports.

**Config** (`--config`, `--baseline`, `--candidate`):

```json
{
  "evaluationInterval": "60s",
  "threshold": { "op": ">", "value": 1 },
  "for": "60s",
  "keep_firing_for": "120s",
  "missingData": "false",
  "start": 0,
  "end": 360
}
```

All five non-range fields are required. `missingData` is the string `"false"` or
`"error"`. `start`/`end` are paired Unix seconds; omitting both infers the
earliest/latest samples (reported as `rangeSource: "sample-extrema"`). Durations
use exactly one `ms`/`s`/`m`/`h`/`d` unit with exact millisecond precision
(`"0s"`, `"500ms"`, `"1.5m"`); only the interval must be positive. Thresholds are
finite JSON numbers; operators are `>`, `>=`, `<`, `<=`, `==`, `!=`. There is no
PromQL, regex, interpolation or lookback.

**Grid** (`--grid`): `{"for": [...], "keep_firing_for": [...], "maxEvaluations": N}`.
Arrays hold 1..256 durations without normalized duplicates; the product is at
most 256 combinations; `maxEvaluations` (1..1,000,000) must cover
`(combinations + 1) x evaluations x max(1, series)` including the baseline run.

Input files must be regular local files of at most 64 MiB, strict UTF-8, valid
JSON without duplicate object keys and nested at most 100 levels.

## Replay semantics

Each exact label set (all labels, including `__name__`) starts `inactive` unless a
checkpoint supplies its state. True enters `pending`; reaching `for` fires.
False resets pending immediately. While firing, the **first false evaluation**
starts the hold; continued false resolves at or after `keep_firing_for`, and a
true evaluation clears the hold without repeating `for`. Timers that are not a
multiple of the interval take effect at the next evaluation. Missing grid samples
are false (or an error under `missingData: "error"`); a present `NaN` is numeric
NaN, not missing. Firing duration is observed exposure in `[start, end)`; open
episodes at capture end are right-censored (`end: null, censored: true`).
Aggregate durations sum series-seconds, using exact integer milliseconds
internally. See [the precise semantics](alert-flapsim/SEMANTICS.md) and the
[seeded jitter experiment](alert-flapsim/EXPERIMENT.md).

## Checkpoints and join

```sh
otelwrench alert-flapsim checkpoint examples/alert-flapsim/checkpoint-window-1.json \
  --config examples/alert-flapsim/checkpoint-alert-1.json > state.json
otelwrench alert-flapsim replay examples/alert-flapsim/checkpoint-window-2.json \
  --config examples/alert-flapsim/checkpoint-alert-2.json --checkpoint-in state.json
```

A checkpoint (`kind: "alert-flapsim/checkpoint"`, `schemaVersion: 1`) records the
state after `evaluatedThrough`. The next window needs explicit `start`/`end` with
`start = evaluatedThrough + evaluationInterval`, identical settings and identical
exact label sets; gaps, overlaps and shifted grids fail. `join` accepts 2..256
saved replay reports from one checkpoint chain, re-verifies every derived field by
replaying the saved raw samples, and emits one regular `command: "replay"` report
for the combined capture (at most 1,000,000 combined series-evaluations).

## Saved-report comparisons

`compare-reports` and `compare-report-set` take complete saved replay reports
(including `join` output) that start at their checkpoint origin and share the
exact raw capture: identical windows, interval, label sets and raw sample strings
(`"2"` differs from `"2.0"`; missing differs from `"NaN"`). Each report is
regenerated and verified before any output. All reports together may cost at most
1,000,000 series-evaluations. Verification checks internal consistency, not
authenticity.

`compare-report-set` options follow the report paths, in any order, once each:

- `--detail summary` emits compact JSON (`command: "replay-summary"` per report,
  no expanded timelines) with a shared `captureIdentity` and a per-report
  `ruleIdentity`. Default `full` is the complete report set.
- `--format csv` with `--detail full` emits the fixed CSV schema below; with
  `--detail summary` it emits the separate summary CSV
  (`csvSchema: alert-flapsim-report-set-summary-v1`, 50 columns).
- `--names NAMES.json` (requires `--detail summary`) adds display names:
  `{"schemaVersion":1,"baseline":"prod","candidates":["for 5m","for 10m"]}`, one
  name per candidate in input order, each 1..256 UTF-8 bytes. Named CSV is
  `schemaVersion` 2 / `alert-flapsim-report-set-summary-v2` with a `displayName`
  column after `candidateIndex`.
- `--manifest SET.json` replaces positional paths:
  `{"schemaVersion":1,"baseline":"baseline.json","candidates":["c1.json","c2.json"]}`.
  Relative paths resolve against the canonical manifest file's directory.
- `--output PATH` writes the export atomically: an exclusive
  `.alert-flapsim-<uuid>.tmp` file (mode `0600` subject to umask) in the target
  directory is renamed over the target after the whole set validates. The parent
  must exist; symlinks, directories, FIFOs and any alias of an input report,
  manifest or names file are rejected. On failure an existing target keeps its
  bytes and an absent target stays absent. There is no `-` stdout alias (use
  `./-` for a file named `-`).

## CSV output

`compare`, `grid`, `compare-reports` and `compare-report-set --detail full` share
one fixed CSV schema (version 1): a header, then baseline rows, then candidates in
order; each run emits an aggregate row followed by one row per canonical label set.
Records are CRLF-terminated, cells containing `,`, `"`, CR or LF are quoted with
doubled quotes, and unavailable values are the literal `null`.

```text
schemaVersion,role,candidateIndex,scope,seriesId,baselineForMs,baselineKeepFiringForMs,forMs,keepFiringForMs,
evaluationIntervalMs,thresholdJson,missingData,windowStart,windowEnd,rangeSource,seriesCount,firstFiring,
firingDurationSeconds,firingEpisodes,resolutions,openAtEnd,pendingAtEnd,transitionCount,missingSamples,nanSamples,
heldEvaluations,firstFiringDelaySeconds,deltaFiringDurationSeconds,deltaFiringEpisodes,deltaResolutions,
deltaOpenAtEnd,deltaPendingAtEnd,deltaTransitionCount
```

(one header line; wrapped here for readability). `seriesId` is the canonical JSON
array of `[label, value]` pairs sorted by UTF-16 code units, so label text never
becomes a bare spreadsheet cell. Timer columns are exact integer milliseconds.

## Identity encodings

Both digests are SHA-256 over the UTF-8 bytes of an ECMAScript `JSON.stringify`
array (no whitespace, no trailing newline):

```text
["alert-flapsim-capture-v1", startMs, endMs, intervalMs, evaluations, [[labelPairs, rawGrid], ...]]
["alert-flapsim-rule-v1", intervalMs, forMs, holdMs, op, threshold, missingData]
```

The integer header fields and durations are decimal strings; `threshold` is a JSON
number in ECMAScript formatting (`-0` serializes as `0`). The capture identity
covers exact bounds, interval, labels and raw sample strings (`null` for missing);
the rule identity covers only the rule settings. For example, a 60 ms interval,
120 ms `for`, 180 ms hold, `>= 1.5` and `"false"` hash exactly
`["alert-flapsim-rule-v1","60","120","180",">=",1.5,"false"]`.

## Python API

```python
from otelwrench import flapsim

report = flapsim.replay(matrix, config)                # dicts parsed from JSON
resumed = flapsim.replay(next_matrix, next_config, report["checkpoint"])
text = flapsim.to_json(report, 2)                      # byte-identical to the CLI output
```

`replay`, `join`, `compare`, `compare_csv`, `compare_reports`,
`compare_reports_csv`, `compare_report_set(baseline, candidates, {"detail": "summary"})`,
`compare_report_set_csv`, `compare_report_set_summary_csv`, `grid`, `grid_csv`,
`parse_matrix` and `parse_json` mirror the commands. Errors raise
`flapsim.FlapsimError` with the CLI diagnostic text. Use `parse_json` (numbers as
IEEE-754 doubles, duplicate keys rejected) and `to_json` (ECMAScript number
formatting and key order) to keep saved reports byte-compatible.

## Limits and non-goals

No PromQL engine, Alertmanager grouping/inhibition/silencing, live state
restoration, statistical extrapolation, notifications or deployment. Numeric
comparison is configured here, unlike an alert rule's expression-vector presence
semantics. Expanded timelines are capped at 1,000,000 series-evaluations per
replay; timestamps and durations must fit safe-integer milliseconds. Results
describe only the supplied finite capture; no candidate is ranked or selected.
It does not replace `promtool test rules`.
