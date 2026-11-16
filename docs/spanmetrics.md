# otelwrench spanmetrics

Build reproducible Prometheus request/error/duration (RED) fixtures from a bounded
OTLP JSON trace capture, either a local file or explicit JSONL stdin. Use it to see
exactly which `spanmetrics_*` series a set of saved traces produces, and to compare
two dimension selections on the same capture (and gate the series increase in CI)
before changing a Collector spanmetrics configuration. It runs offline, without a
Collector, exporter, database, server or credentials. This is an original, limited
interoperability implementation; the Collector's spanmetrics connector is the full
online alternative.

## Quick start

Install once with `pip install otelwrench`, then run from a clone of the repository
so the sample captures under `examples/spanmetrics/` are available:

```sh
otelwrench spanmetrics examples/spanmetrics/traces.json --buckets 0.1,0.5,1 --output out.prom
otelwrench spanmetrics compare examples/spanmetrics/dimensions.json \
  --dimensions-file examples/spanmetrics/dimension-pair.json \
  --max-series-increase 24 --output comparison.json --dry-run
otelwrench spanmetrics validate-dimensions --dimensions-file examples/spanmetrics/dimension-pair.json
```

Conversion writes `out.prom` and `out.prom.report.json`, and prints the same JSON
report to stdout. Use `--report FILE` to choose another report path. Existing
files require `--force`; destination directories must already exist. The command
uses only Python's standard library at runtime and never touches the network.
`python -m otelwrench spanmetrics` is equivalent. Run `otelwrench spanmetrics --help`,
`otelwrench spanmetrics compare --help` or `otelwrench spanmetrics validate-dimensions --help`
for every option.

## Reproducible examples

All files in `examples/spanmetrics/` are synthetic and contain no production telemetry.
`traces.json` is an OTLP `resourceSpans` → `scopeSpans` → `spans` envelope with
a resource, instrumentation scope, schema URL, attributes, status and an event.
It contains a 0.1-second parent and a 0.3-second child, both named `GET /shop` in
service `web`. The child has status code 2 (ERROR); the parent's unset status
is not an error. Both count once, regardless of parentage.

With bounds `0.1,0.5,1`, the two status partitions contain:

| status_code | calls | errors | le=0.1 | le=0.5 | le=1 | le=+Inf | count | sum (seconds) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 0.1 |
| 2 | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 0.3 |

The report totals are **2 unique spans, 1 error, 0.4 seconds, 2 groups, 16 time
series**. Summing the bucket counts over both status partitions gives 1,2,2,2.
The implementation compares exact integer nanoseconds to decimal boundaries;
a duration equal to a bound belongs in that bucket. Histogram buckets are cumulative.

Select only the extra attributes that should become dimensions:

```sh
otelwrench spanmetrics examples/spanmetrics/traces.json \
  --dimension resource:deployment.environment.name=environment \
  --dimension span:http.request.method=method \
  --buckets 0.1,0.5,1 --output out.prom --force
```

The resulting custom labels are `environment="s:synthetic"` and `method="s:GET"`.

## Multiple requests in JSONL

Select JSONL explicitly; the tool never guesses from the file extension:

```sh
otelwrench spanmetrics examples/spanmetrics/traces.jsonl --input-format jsonl \
  --buckets 0.1,0.5,1 --output out.prom
```

`examples/spanmetrics/traces.jsonl` splits `traces.json` into two lines, one supported OTLP
`ExportTraceServiceRequest` per line, retaining each span's full resource/scope
context. It produces **byte-identical Prometheus output**, including all 16 series,
and the same report totals: 2 unique spans, 1 error, 0.4 seconds and 2 groups.
The report's raw input SHA-256, input format and CLI paths can differ. Repeating
either line counts an exact duplicate across requests without changing metrics.
Conflicting span, resource, scope or schema metadata across lines rejects the
whole conversion under the same full-record policy as a single request.

Every line must be a complete UTF-8 JSON request; blank lines and pretty-printed
multiline requests are errors. LF and CRLF are accepted, and the final newline is
optional. An empty file is rejected; use a line containing `{"resourceSpans":[]}`
for an empty capture. `--max-line-bytes` defaults to 1 MiB of UTF-8 bytes per line,
excluding LF/CRLF; it is checked before decoding JSON. The total input byte cap
includes line terminators. `--max-spans` counts records **across all requests,
including duplicates and invalid spans**; `--max-series` caps aggregate output
across all requests. No cap resets at a line boundary.

**Failure example:** add `--max-spans 1` to the command above. It exits 1 and
writes a rejected report, with no new metrics. A malformed interior line or
exceeded line/byte/span cap stops processing and sets `record_counts_complete=false`;
prefix totals are diagnostic only. Diagnostics identify the failing line. Span
errors, conflicting duplicates and series overflow reject all metrics while
continuing record checks. With `--force`, old metrics remain unchanged on any
rejection. Publication starts only after validation of the entire capture;
metrics and report retain the individual atomic-file guarantees described below.

Both conversion and `compare` accept `--input-format jsonl` and the same limits.
The library equivalent is `convert(data, Config(input_format="jsonl",
max_line_bytes=1048576))` from `otelwrench.spanmetrics.core`; comparison configurations must share these settings.
Input bytes remain in memory under the total byte cap; JSONL decodes one request
at a time and retains global deduplication and aggregation state. This is bounded
fixture ingestion, not an unbounded streaming collector.

### Read JSONL from stdin

Select stdin explicitly with positional `-` or `--input -`, and always include
`--input-format jsonl`. Both conversion and `compare` accept either spelling:

```sh
otelwrench spanmetrics --input - --input-format jsonl \
  --buckets 0.1,0.5,1 --output out.prom < examples/spanmetrics/traces.jsonl

otelwrench spanmetrics compare - --input-format jsonl \
  --dimensions-file examples/spanmetrics/dimension-pair.json --output comparison.json < examples/spanmetrics/traces.jsonl
```

The first command produces the same metrics as the file example: **2 unique spans,
1 error, 0.4 seconds, 2 groups and 16 series**. The report has `input="-"` and hashes
all raw input bytes, including line terminators. There is no implicit stdin mode;
provide exactly one positional source or `--input SOURCE`. File paths also work
with `--input`; `./-` reads a literal file named `-`. Stdin with the default
`json` format, conflicting sources or a
missing source exits **2** without publication.

Reads use chunks of at most 64 KiB and consume at most `--max-input-bytes + 1`
bytes. The bounded capture is buffered before validation; line/span/series limits
apply across all requests exactly as for files. Success waits for EOF. An input
SHA-256 is reported only after EOF within the byte cap, including on later JSONL
validation rejection. Byte overflow stops reading immediately, exits **1** with
`record_counts_complete=false`, omits `input_sha256`, and emits no new metrics.
For example, adding `--max-input-bytes 10` to the first command rejects it. An
empty stdin, malformed/blank line, invalid UTF-8, line/span cap, conflicting
duplicate or series overflow also exits **1** and rejects all metrics. `--force`
preserves old metrics on rejection. Comparison suppresses both sides' cardinality
and attribution on any rejection. Dry runs write nothing. Input read failures
exit **2** without publication; publication I/O retains the individual atomic-file
guarantees below. Output preflight runs before stdin consumption;
regular files redirected through stdin are protected against output/report aliases.
Stdin is not closed by the tool. A producer must finish/close its stream; there is
no timeout or unbounded collection mode.

### Require exact capture bytes before conversion

Conversion accepts an optional expected SHA-256 from a separately reviewed
capture. For the unchanged synthetic JSON example:

```sh
otelwrench spanmetrics examples/spanmetrics/traces.json \
  --expect-input-sha256 f796ed38eb7bb026a4322629d5119ef4455647e0115610c39f643d870c7abe85 \
  --output out.prom

otelwrench spanmetrics --input - --input-format jsonl \
  --expect-input-sha256 5337cefc4dd8d8ee514ff21968b8fd3cb9a25c1cea922a0b9562509215afcf2e \
  --output out.prom --dry-run < examples/spanmetrics/traces.jsonl
```

`--expect-input-sha256 HEX` requires exactly **64 lowercase ASCII hexadecimal
digits** (`0-9`, `a-f`), without a prefix or whitespace. Spell the gate option in
full; `--expect-input-sha256=HEX` also works. Invalid, incomplete or abbreviated
gate options exit **2** with empty stdout before reading a pair or capture.
Existing option abbreviations and defaults remain available. JSON/JSONL files
accept positional input or `--input FILE`; stdin still requires explicit JSONL.

The gate hashes the same bounded bytes loaded once by the existing reader,
without reopening the capture. It compares only after full consumption within
the total byte cap, before parsing or aggregating the capture. A mismatch exits
**2** with empty stdout and `otelwrench spanmetrics: input SHA-256 mismatch: expected HEX, got HEX`
on stderr. Inputs, metrics and explicit/default reports stay unchanged, including
under `--force` and `--dry-run`. Whitespace, object ordering, UTF-8 encoding and
JSONL line endings affect the digest even when decoded requests are equivalent.

A match adds no fields and retains existing metric/manifest bytes and publication
behavior. Matching malformed captures or line/span/series-limit failures still
exit **1**, publishing a diagnostic report and preserving old metrics under
`--force`. Total-byte overflow retains its existing exit-1 diagnostic path:
**no digest is calculated or compared for the truncated prefix**, no input hash
is reported, and no new metrics are emitted. This also applies if HEX happens
to match that prefix. Dry runs write nothing. A matching stdin capture requires
producer EOF; no timeout is added.

The gate works with dimension flags or either explicitly selected reviewed pair
side. Complete-pair validation and an optional `--expect-dimension-sha256` gate
run before capture consumption; destination alias preflight remains in place.
`--dimension-provenance` stays separately opt-in. Expected capture digests check
loaded byte identity, without authenticating, archiving or locking the capture
against concurrent/later changes. `compare` also accepts the gate as described
below; capture-free `validate-dimensions` does not.

## Compare dimension configurations on one capture

The `compare` command reads one capture once from a file or explicit JSONL stdin
and validates the same bytes under two explicit dimension configurations. It writes a single JSON report
and prints that report to stdout. It emits no Prometheus file.

```sh
otelwrench spanmetrics compare examples/spanmetrics/dimensions.json \
  --baseline-dimension span:method=method \
  --candidate-dimension span:method=method \
  --candidate-dimension resource:zone=zone \
  --candidate-dimension span:customer=customer \
  --buckets 0.1,0.5,1 --output comparison.json
```

Each side always includes the four mandatory labels. Repeat its dimension option
to select custom attributes; omitting the option means no custom dimensions.
The candidate need not extend the baseline: removals, replacements and label
renames are supported. Histogram bounds and all limits are shared. `--max-series`
applies separately to each configuration. `--dry-run` prints without writing;
`--force` replaces an existing regular JSON output. Path/alias protection and exit
codes match conversion. Use `otelwrench spanmetrics compare --help` for all options. The library API is
`otelwrench.spanmetrics.comparison.compare(data, baseline_config, candidate_config)`.

The local synthetic `dimensions.json` has six unique 0.1-second spans and one
exact duplicate. All mandatory labels match (`web`, `request`, kind 2, status 0).
Its **independently hand-counted expected results** are 2 baseline groups / 16
series and 5 candidate groups / 40 series: **candidate minus baseline = 24**.
Three finite bounds mean 8 series per group. The joint observed combinations are:

| resource:zone | span:method | span:customer (typed) | Unique spans |
| --- | --- | --- | ---: |
| `s:east` | `s:GET` | `s:1` | 1 |
| `s:east` | `s:GET` | `i:1` | 1 |
| `s:west` | `s:GET` | `missing` | 2 |
| `s:east` | `s:POST` | `s:a\|b` | 1 |
| `s:west` | `s:POST` | `s:missing` | 1 |

The GET partition splits from 1 to 3 groups (+16 series); POST splits from 1 to
2 (+8 series). Two spans in the same west/GET/missing combination remain one
group. Missing, literal `"missing"`, string `"1"`, and integer `1` have distinct
identities. The duplicate contributes no group or span count.

The JSON has `report_type="dimension_comparison"`, `comparison_complete=true`,
`unique_records=6`, per-side `configurations.*.groups` and `.series`, and
`series_delta=24`. Its sorted `partitions` retain mandatory labels and shared
attributes by **source/key**, even when labels are renamed. Each partition lists
the baseline/candidate group counts, series delta, and `attribute_combinations`.
Every combination includes structured `source_attributes` entries, full
`baseline_labels` and `candidate_labels`, and its unique-span count. No delimiter
concatenation defines identity. These observed joint combinations explain the
split; they do not assign independent causal effects to correlated attributes.

**Failure example:** add `--max-series 16` to the command. The candidate exceeds
the cap, so exit is 1, `status="rejected"`, `comparison_complete=false`, both
configurations' group/series counts and the delta are `null`, and `partitions=[]`.
The same suppression applies to any malformed span, conflicting duplicate,
selected complex attribute, malformed envelope or byte/span cap in either side.
Per-side `validation` retains bounded diagnostics and diagnostic record counts;
`record_counts_complete` there describes record classification, not a usable
comparison. Never interpret accepted-record diagnostics as complete cardinality.
With `--force`, a rejected report replaces the old comparison JSON. Invalid
options, paths or I/O failures exit 2 and do not promise a report.

Counts and attribute values are **capture-local**. They do not estimate production
traffic, unseen combinations, sampling correction or cost. The report includes
selected attribute values, so handle it with the same care as the source capture.
Processing and attribution are in memory, bounded by the configured byte/span/
series limits; up to one joint combination per unique captured span may appear.

### Omit observed values from a comparison report

`compare --summary-only` produces a deterministic count summary using the same
complete validation and aggregation as the default comparison:

```sh
otelwrench spanmetrics compare examples/spanmetrics/dimensions.json \
  --dimensions-file examples/spanmetrics/dimension-pair.json --summary-only \
  --max-series-increase 24 --output summary.json --dry-run
# Exit 0: baseline 2 groups / 16 series, candidate 5 / 40, delta 24.
```

This is a Boolean switch with **exact spelling** and no value. Prefixes such as
`--summary` and forms such as `--summary-only=true` exit **2**, before reading a
pair or capture. Conversion and `validate-dimensions` do not accept it. Legacy
options and their existing abbreviations retain their behavior.

The summary removes the `partitions` key entirely, including for empty or rejected
captures. It adds `summary_only=true`, replaces `attribution` with
`{"status":"omitted","reason":"summary_only"}`, and adds
`summary_omissions=["partitions","validation_diagnostic_messages"]`.
No observed mandatory/custom label values or source attribute combinations are
copied into replacement metadata. Diagnostic messages can quote capture-controlled
text, so each per-side `validation.diagnostics` becomes `[]`; its former length
is added to `diagnostics_omitted`, retaining the exact total diagnostic count.
Use the default report when detailed rejection messages are needed.

All other fields remain: resolved dimension **definitions and names**, per-side
validation statuses, record counts and completeness, groups/series, final delta,
input digest, optional pair provenance and budget decision. Rejection still
suppresses both sides' cardinality and delta; omitted attribution does not imply
successful validation. Budget status/exits, digest-gate precedence, JSON/JSONL
files and explicit JSONL stdin, dry-run, force and alias protection are unchanged.
Without the switch, report bytes and exit/publication behavior stay unchanged.

This is an output omission mode, not a smaller-memory parser or aggregator:
validation and attribution still run in memory under the same limits. It does
not anonymize caller-supplied paths, dimension source/key/label definitions or
hashes, authenticate inputs, or estimate unseen traffic. The library comparison
API continues to return its full report. No dimension coverage summary is added.

### Set a capture-local series-increase budget

`compare --max-series-increase N` optionally checks the **final complete**
`series_delta` (candidate minus baseline). `N` must contain ASCII decimal digits
and be in **0..9223372036854775807**; zero and leading zeros are accepted.
Signs, whitespace, fractions, exponents, underscores and Unicode digits are
rejected. The option requires its exact spelling; existing `--max-series`
abbreviations keep their meaning. Invalid/incomplete/abbreviated budget options
exit **2** with empty stdout before pair or capture reads.

```sh
otelwrench spanmetrics compare examples/spanmetrics/dimensions.json \
  --dimensions-file examples/spanmetrics/dimension-pair.json \
  --max-series-increase 24 --output comparison.json --dry-run
# Exit 0: actual series_delta=24, inclusive budget met.
# Use 23 instead: exit 1, actual counts and attribution remain available.
```

Only with the option, the report adds:

```json
"series_increase_budget": {
  "max_series_increase": 24,
  "status": "within_budget"
}
```

The budget status is `within_budget` with exit **0** when `series_delta <= N`,
including zero/negative deltas; `exceeded` with exit **1** when `series_delta > N`;
or `unavailable` with exit **1** on a rejected/incomplete capture. On exceedance,
`status="ok"` still denotes valid capture comparison and
`comparison_complete=true`; real counts and partitions remain unchanged.
Stderr explicitly reports budget exceedance. On rejection, `status="rejected"`,
null counts/delta, empty partitions and existing unavailable-count diagnostics
remain; no budget evaluation is claimed from diagnostic prefix counts.

This is separate from `--max-series`, which caps each side and rejects an
incomplete comparison. A budget does not raise that cap. Flags or reviewed pairs,
JSON/JSONL files or explicit JSONL stdin, provenance and digest gates work together.
Pair validation/gates and capture loading/gates keep their precedence; digest
mismatches exit **2** without JSON/publication, and byte overflow retains existing
exit-1 rejection without hashing a prefix. Complete comparison is required
before evaluating the budget, including after the final JSONL line.

An exceeded-budget report is written like any complete comparison. Existing
output requires `--force`; `--dry-run` prints without writing, even on exceedance.
Alias protection and individually atomic publication remain unchanged. Without
the option, JSON bytes, exit codes and publication behavior remain unchanged.
The library and capture-free inspection APIs do not accept this CLI-only budget.
The decision describes this capture only, with no traffic, cost or unseen-series
prediction.

### Require exact capture bytes before comparison

`compare --expect-input-sha256 HEX` optionally gates the exact fully loaded
capture bytes before either configuration is compared or aggregated:

```sh
otelwrench spanmetrics compare examples/spanmetrics/dimensions.json \
  --dimensions-file examples/spanmetrics/dimension-pair.json \
  --expect-input-sha256 7b616616b3f477c51d3d4c6616280be17a70f0598f8d5ce581f8c23b123da1ac \
  --output comparison.json --dry-run

otelwrench spanmetrics compare --input - --input-format jsonl \
  --expect-input-sha256 5337cefc4dd8d8ee514ff21968b8fd3cb9a25c1cea922a0b9562509215afcf2e \
  --output comparison.json --dry-run < examples/spanmetrics/traces.jsonl
```

The same **64 lowercase ASCII hexadecimal digits**, full option spelling,
`--expect-input-sha256=HEX` form and exact-byte rules documented for conversion
apply. Invalid, incomplete or abbreviated gates exit **2** with empty stdout
before pair/capture reads. Files accept positional input or `--input FILE`;
stdin requires explicit JSONL and producer EOF. Existing defaults and legacy
option abbreviations remain available.

With dimension flags or a reviewed complete pair, the gate compares the digest
of the same single bounded capture load, without reopening. Complete-pair
validation and an optional `--expect-dimension-sha256` gate still precede capture
loading. An in-cap mismatch exits **2** with empty stdout and
`otelwrench spanmetrics: input SHA-256 mismatch: expected HEX, got HEX` on stderr,
before comparison/aggregation/publication. Inputs, pair and output stay
unchanged, including `--force` and `--dry-run`; output alias preflight remains
in place. Formatting and JSONL line-ending changes can mismatch even when
decoded requests are equivalent.

A match adds no fields and retains the existing comparison JSON/publication
behavior. Pair provenance remains separately opt-in, with no `selected_side`.
Matching malformed captures or line/span/series-limit failures still exit **1**
with diagnostic reports, both sides' group/series counts unavailable, no delta
or attribution. Total-byte overflow keeps its exit-1 diagnostic report without
hashing or comparing any prefix, even if HEX matches the returned cap+1 bytes;
no input hash is reported. Dry runs write nothing. This checks loaded byte
identity, without authenticating, archiving or locking the capture against
concurrent/later changes. Inspection and library APIs are unchanged.


### Reuse a reviewed dimension pair

Save both selections in a UTF-8 JSON file and pass it to `compare`:

```sh
otelwrench spanmetrics compare examples/spanmetrics/dimensions.json \
  --dimensions-file examples/spanmetrics/dimension-pair.json --output comparison.json
```

`examples/spanmetrics/dimension-pair.json` contains:

```json
{
  "schema_version": 1,
  "baseline": [{"source": "span", "key": "method", "label": "method"}],
  "candidate": [
    {"source": "span", "key": "method", "label": "method"},
    {"source": "resource", "key": "zone", "label": "zone"},
    {"source": "span", "key": "customer", "label": "customer"}
  ]
}
```

With the same capture, bounds, limits and output path, this produces the **same
JSON report** as the flags above: 2/5 baseline/candidate groups, 16/40 series and
`series_delta=24`. Dimension order is normalized by label. Both arrays are required;
use `[]` for built-in labels only. Labels may be reused across the two independent
sides, and the same key in resource and span is a different source/key identity.

The file must be at most **65,536 bytes** and contain exactly `schema_version`
(JSON integer `1`), `baseline` and `candidate`. Each array has at most 32 entries;
each entry contains exactly the string fields `source`, `key` and `label`.
Sources must be `resource` or `span`; keys must be nonblank and cannot contain
`=`. The existing label syntax, reserved-name, built-in `service.name`, duplicate
source/key and label-collision rules apply independently to each side. Strings
must contain valid Unicode. Unknown/missing fields, wrong types, duplicate JSON
object keys, nonstandard numeric constants, malformed JSON and unsupported
versions are errors. Files contain selections only; bounds, limits, input format
and capture/output paths stay explicit CLI options. No includes or scripts run.

`--dimensions-file` cannot be combined with either `--baseline-dimension` or
`--candidate-dimension`; no merging or overrides occur. Invalid configurations
exit **2**, print a diagnostic to stderr and write no report, including under
`--force`. Path/I/O errors also exit 2 with the general publication guarantees
described below. A comparison output cannot alias the configuration file by path,
symlink or hardlink. `--dry-run` validates the configuration but writes nothing.
A valid configuration with a rejected capture retains the existing exit-1 report
contract.

### Record the reviewed pair used for comparison

Opt in to exact-byte provenance for both comparison configurations:

```sh
otelwrench spanmetrics compare examples/spanmetrics/dimensions.json \
  --dimensions-file examples/spanmetrics/dimension-pair.json --dimension-provenance \
  --output comparison.json
```

The saved JSON and stdout add `dimension_pair_provenance` containing
`schema_version=1` and `sha256` (64 lowercase hex digits). This digest covers the
**exact complete pair-file bytes loaded and validated**, including whitespace
and both selections. It comes from the same bounded read without reopening the
file or repeating schema validation. Comparison always uses both arrays; this
field has no `selected_side`. Resolved dimensions remain sorted by label.
Formatting or entry-order changes can change the digest without changing
dimensions, counts or attribution. No pair path or contents are embedded.

The field also appears on `--dry-run` stdout and exit-1 rejected-capture reports,
including capture byte-cap rejection. Any rejection still suppresses both sides'
group/series counts, the unique count and delta, and all attribution. Explicit
JSONL file/stdin inputs and shared bounds/limits retain their existing behavior.
The reviewed pair stays unchanged; output aliases by path, symlink or hardlink
are rejected before capture consumption, including with `--force`.

`--dimension-provenance` requires `--dimensions-file`. Flag-only selections,
mixing the file with either side's dimension flags, incomplete options or an
invalid pair on either side exit **2** before capture consumption or publication.
`--dimensions-side` remains unsupported by `compare`. Without the opt-in,
comparison JSON stays byte-equivalent to expanded dimension flags with the same
capture, settings and paths. The digest identifies loaded bytes, not authenticity,
an archive or the file's later contents; no concurrent-mutation lock is taken.

### Require the reviewed pair before comparison

Gate comparison on the exact complete-pair bytes from an earlier review:

```sh
otelwrench spanmetrics compare examples/spanmetrics/dimensions.json \
  --dimensions-file examples/spanmetrics/dimension-pair.json \
  --expect-dimension-sha256 2f4bb65208dbd9e8baca3bd3d2e534c76eedde7001659d1be0dc06480623cceb \
  --output comparison.json --dry-run
```

`--expect-dimension-sha256 HEX` requires `--dimensions-file` and exactly **64
lowercase hexadecimal digits** (`0-9`, `a-f`), with no prefix or whitespace.
The gate option must be spelled in full; `--expect-dimension-sha256=HEX` also
works. Flag-only selections, mixing the file with either dimension flag, and
invalid, incomplete or abbreviated gate options exit **2** with empty stdout
before reading the pair or capture. Both selections then pass the existing
strict complete-pair validation before the gate compares against the digest
from that same single bounded load, without reopening or revalidating the file.
Invalid files keep their existing diagnostics even when their bytes hash to HEX.

A match retains the existing comparison JSON and publication behavior, including
JSON/JSONL files, explicit JSONL stdin, `--dry-run`, and exit-1 rejected-capture
reports with suppressed counts. The gate adds no fields; provenance remains a
separate opt-in with no `selected_side`. A mismatch exits **2** with empty stdout
and `otelwrench spanmetrics: dimension configuration SHA-256 mismatch: expected HEX, got HEX`
on stderr, before capture/stdin reads or publication. Existing destinations and
inputs stay unchanged, including under `--force`; path/symlink/hardlink alias
protection still applies when the gate matches.

Whitespace or entry-order changes can mismatch even with identical canonical
dimensions. Supply the expected digest from a trusted review separately: this
checks loaded byte identity, without authenticating or archiving bytes or locking
against concurrent/later file changes. Selected-side conversion accepts the same
gate as described below.

### Inspect a reviewed pair without a capture

Validate both selections before running comparison or conversion:

```sh
otelwrench spanmetrics validate-dimensions \
  --dimensions-file examples/spanmetrics/dimension-pair.json
```

Exit **0** prints deterministic JSON with `schema_version=1`,
`report_type="dimension_configuration"`, `status="ok"` and `configurations`.
Each `configurations.baseline` / `.candidate` contains `dimensions` (the custom
`source`, `key`, `label` entries sorted by label) and `custom_dimension_count`.
The example has counts **1** and **3**. Empty arrays have count 0; the four
mandatory metric labels are excluded. Resource and span keys remain distinct.
This is configuration validation; it cannot predict capture-local series counts
or guarantee that a capture will pass validation.

The command uses the same complete-pair schema, 65,536-byte cap and per-side
dimension rules as comparison and conversion. A missing, nonregular, malformed
or over-cap file, or invalid selection on either side, exits **2** with empty
stdout and a diagnostic on stderr. It reads no capture or stdin and writes no
files; the reviewed file stays unchanged. Only `--dimensions-file FILE`, optional
`--dimension-provenance`, optional `--expect-dimension-sha256 HEX` and `--help`
are accepted, without option abbreviations.
Capture, conversion, output,
side, force and dry-run options are errors. As for other pair consumers,
`--dimensions-file -` refers to a literal local file named `-`, not stdin.

To identify the exact reviewed bytes for both inspected selections, opt in:

```sh
otelwrench spanmetrics validate-dimensions \
  --dimensions-file examples/spanmetrics/dimension-pair.json --dimension-provenance
```

The deterministic JSON adds only `dimension_pair_provenance` containing
`schema_version=1` and `sha256` (64 lowercase hex digits). The digest covers the
**exact complete pair-file bytes loaded and validated**, including whitespace
and both selections, from the existing single bounded read and schema validation.
The file is not reopened; there is no `selected_side` because both arrays are
inspected. Canonical dimensions and counts, including empty selections, stay the
same. Formatting or entry-order changes may change the digest without changing
those selections. Without the opt-in, inspection JSON stays byte-identical.

`--dimension-provenance` still requires `--dimensions-file FILE`; incomplete
options and the invalid options/files described above exit **2** with empty
stdout, without capture/stdin reads or file writes. No pair path or raw contents
are embedded. The digest identifies loaded bytes, not authenticity, an archive
or the file's later contents; no concurrent-mutation lock is taken.

To require the exact bytes from an earlier review, supply the expected digest:

```sh
otelwrench spanmetrics validate-dimensions \
  --dimensions-file examples/spanmetrics/dimension-pair.json \
  --expect-dimension-sha256 2f4bb65208dbd9e8baca3bd3d2e534c76eedde7001659d1be0dc06480623cceb
```

`HEX` must be exactly **64 lowercase hexadecimal digits** (`0-9`, `a-f`),
without a prefix or whitespace. Invalid or incomplete options fail before the
pair file is read. Both selections must pass the existing strict validation
before the expected digest is compared with the digest from that single bounded
complete-pair load; the file is not reopened or revalidated.

A match exits **0** with the existing byte-identical inspection JSON. The gate
adds no fields; `--dimension-provenance` remains a separate opt-in, and neither
option infers a selected side. A mismatch exits **2** with empty stdout and
`otelwrench spanmetrics: dimension configuration SHA-256 mismatch: expected HEX, got HEX`
on stderr. Invalid files retain their existing diagnostics even if their bytes
match the supplied digest. No capture/stdin is read and no files are written,
including on failure. Formatting or selection-order changes can cause a mismatch
even when the canonical dimensions are unchanged. This checks loaded byte identity;
it does not authenticate the expected digest or lock the file against later changes.
The expected-digest option is also available on `compare` and selected-side conversion.

### Convert with one reviewed side

Select a side explicitly to use the same pair for metric conversion:

```sh
otelwrench spanmetrics examples/spanmetrics/dimensions.json \
  --dimensions-file examples/spanmetrics/dimension-pair.json --dimensions-side candidate \
  --buckets 0.1,0.5,1 --output candidate.prom
```

This produces **5 groups and 40 series**, with 6 unique spans, 1 duplicate,
0 errors and 0.6 seconds of total duration. Selecting `baseline` produces
**2 groups and 16 series**. The Prometheus bytes and manifest match conversion
with that side expanded as repeated `--dimension SOURCE:KEY=LABEL` flags when
all other options and paths match. By default the manifest retains resolved
dimensions without pair-file provenance. The reviewed file stays unchanged.

`--dimensions-file` and `--dimensions-side baseline|candidate` are required
together for conversion. There is no default side; an unknown side, a side
without a file, a file without a side, or mixing the file with `--dimension`
exits **2** without changing either destination, even with `--force`.
The whole pair is strictly validated, including the unselected side, using
the schema and byte limit above. An empty selected array means built-in labels
only. `compare` always uses both sides and does not accept `--dimensions-side`.

JSON and explicit JSONL file/stdin captures accept the same shared options:

```sh
otelwrench spanmetrics --input - --input-format jsonl \
  --dimensions-file examples/spanmetrics/dimension-pair.json --dimensions-side baseline \
  --output baseline.prom < examples/spanmetrics/traces.jsonl
```

Both the Prometheus destination and the explicit or default report destination
are protected against configuration-file aliases by path, symlink or hardlink,
including under `--force`. Invalid configurations preserve existing metrics and
reports. Dry runs validate the full pair and capture without writing. A rejected
capture exits **1**, writes the diagnostic report and preserves old metrics under
`--force`; it never publishes a valid prefix. Byte/line/span/series limits,
capture-wide deduplication and full-consumption input hashes remain unchanged.

### Require the reviewed pair before conversion

Gate selected-side conversion on the exact complete-pair bytes from a review:

```sh
otelwrench spanmetrics examples/spanmetrics/dimensions.json \
  --dimensions-file examples/spanmetrics/dimension-pair.json --dimensions-side candidate \
  --expect-dimension-sha256 2f4bb65208dbd9e8baca3bd3d2e534c76eedde7001659d1be0dc06480623cceb \
  --output candidate.prom
```

`--expect-dimension-sha256 HEX` requires a pair file and an explicit
`--dimensions-side baseline|candidate`. HEX must be exactly **64 lowercase
hexadecimal digits** (`0-9`, `a-f`), without a prefix or whitespace. Spell the
gate option in full; `--expect-dimension-sha256=HEX` also works. Flag-only
selections, mixed `--dimension` flags, missing file/side and invalid, incomplete
or abbreviated gate options exit **2** with empty stdout before reading the
pair or capture. Existing options retain their abbreviation behavior.

Both selections, including the unselected side, must pass strict complete-pair
validation before the gate checks the digest from that same single bounded
load, without reopening or revalidating. Invalid pairs keep their existing
diagnostics even when their bytes match HEX. A mismatch exits **2** with empty
stdout and `otelwrench spanmetrics: dimension configuration SHA-256 mismatch: expected HEX, got HEX`
on stderr before capture/stdin consumption or publication. Inputs, metrics and
explicit/default reports stay unchanged, including under `--force` and
`--dry-run`. Matching gates retain both destinations' alias protections.

A match adds no fields and keeps existing metric bytes and manifest/publication
behavior for JSON/JSONL files, explicit JSONL stdin, dry runs and exit-1 rejected
captures. `--dimension-provenance` remains a separate opt-in with an explicit
`selected_side`. The digest covers **both arrays and exact formatting**, so
whitespace or entry-order changes can mismatch despite identical resolved
selections. Supply the expected digest from a trusted review separately: the
gate checks loaded byte identity; it does not authenticate or archive bytes or
lock against concurrent/later changes. Comparison and inspection are unchanged.

### Record the reviewed pair used for conversion

Opt in to exact-byte provenance when converting with a reviewed side:

```sh
otelwrench spanmetrics examples/spanmetrics/dimensions.json \
  --dimensions-file examples/spanmetrics/dimension-pair.json --dimensions-side candidate \
  --dimension-provenance --output candidate.prom
```

The manifest and stdout include `dimension_pair_provenance` with
`schema_version=1`, `selected_side="candidate"` and `sha256` (64 lowercase hex
digits). The digest covers the **exact pair-file bytes loaded and validated**,
including whitespace and both selections. The tool hashes that same bounded
read without reopening the file. Formatting or entry-order changes can change
the digest even when resolved dimensions and metrics stay identical. No path
or file contents are embedded in the provenance field.

The field also appears on `--dry-run` stdout and exit-1 rejected-capture
manifests, including capture byte-cap rejections. Rejection still publishes no
new metrics. `--dimension-provenance` requires `--dimensions-file` and an explicit
valid `--dimensions-side`; incomplete combinations, mixed dimension flags and
invalid pairs exit **2** before capture consumption or publication. Both sides
remain strictly validated and both destination alias checks apply under
`--force`. `compare` accepts the option for both selections as described above;
`validate-dimensions` accepts the option for capture-free inspection.

Without the option, manifests retain byte equivalence with dimension flags.
Provenance identifies the loaded bytes; it does not archive or sign them, lock
the file against changes, or prove its later contents match. Preserve the pair,
capture and shared conversion settings when exact reruns are needed.

## Conversion failures and duplicates

Duplicates and failures can be inspected without writing any files:

```sh
otelwrench spanmetrics examples/spanmetrics/duplicates.json --output out.prom --dry-run
# Exit 0: input_records=3, unique_records=2, duplicate_records=1, rejected_records=0

otelwrench spanmetrics examples/spanmetrics/conflict.json --output out.prom --dry-run
# Exit 1: input_records=3, unique_records=2, duplicate_records=0, rejected_records=1
```

`--dry-run` reads, validates and aggregates the capture, prints the manifest and
writes nothing, even if outputs already exist. No input content is executed.
There are no subprocess hooks, online endpoints, or configuration scripts.

## Metrics and dimensions

Each unique span increments `spanmetrics_calls_total`. Status code 2 also
increments `spanmetrics_errors_total`; other statuses emit an error value of 0.
`spanmetrics_duration_seconds` emits finite `_bucket` samples, the implicit
`le="+Inf"` bucket, `_count` and `_sum`. No observation timestamps, exemplars or
`_created` series are emitted. Zero-duration spans are valid.

All three metrics use these mandatory labels:

| OTLP field | Prometheus label | Values |
| --- | --- | --- |
| resource attribute `service.name` | `service_name` | Nonempty string |
| span `name` | `span_name` | Nonempty string |
| span `kind` | `span_kind` | Integer rendered as string, 0–5 |
| span `status.code` | `status_code` | Integer rendered as string, 0–2 |

Kind 0 is UNSPECIFIED, 1 INTERNAL, 2 SERVER, 3 CLIENT, 4 PRODUCER, 5 CONSUMER.
Status 0 is UNSET, 1 OK, 2 ERROR. Missing kind/status/code defaults to 0.
Missing/blank service and span names are rejected; no service name is inferred.

Custom dimensions are a repeatable explicit allowlist:
`--dimension resource:KEY=LABEL` or `--dimension span:KEY=LABEL`. The sources are
separate; there is no resource/span fallback. Keys must be nonempty and cannot
contain `=`. Labels must match `[a-zA-Z_][a-zA-Z0-9_]*`; built-in labels, `le`,
`__*`, duplicate labels and repeated source/key entries are rejected. Resource
`service.name` is already built in. At most 32 custom dimensions are supported.

To avoid silently merging distinct values, custom labels have typed encodings:
`missing` for an absent attribute, `s:` for a string (including an empty string),
`b:true` / `b:false`, `i:<signed integer>`, or `d:<decimal>`. Thus a string `"1"`
and integer `1` remain different series. Array/kvlist/bytes attributes may be
validated and omitted, but selecting them as dimensions is an explicit error.
All label values escape backslash, double quote and newline for Prometheus.
Custom dimension labels and output groups are sorted for deterministic exposition.

## Supported input contract

By default, exactly one UTF-8 JSON object with a **required** `resourceSpans` array
is accepted. With `--input-format jsonl`, each line must contain one such object.
An empty array is a valid empty capture. JSON duplicate object keys and nonstandard
numeric constants are errors in both formats. Protobuf, gzip, a bare span array, legacy
`instrumentationLibrarySpans`, named enums, snake_case fields, JSON null values,
unknown fields and other OTLP signals are unsupported and rejected.

The following fields are recognized; nested fields are validated even when they
will not be represented in metrics:

- ResourceSpans: `resource`, `scopeSpans`, `schemaUrl`.
- Resource: `attributes`, `droppedAttributesCount`.
- ScopeSpans: `scope`, `spans`, `schemaUrl`.
- Scope: `name`, `version`, `attributes`, `droppedAttributesCount`.
- Span: `traceId`, `spanId`, `parentSpanId`, `traceState`, `flags`, `name`, `kind`,
  `startTimeUnixNano`, `endTimeUnixNano`, `attributes`, `droppedAttributesCount`,
  `events`, `droppedEventsCount`, `links`, `droppedLinksCount`, `status`.
- Status: `code`, `message`. Event: `timeUnixNano`, `name`, `attributes`,
  `droppedAttributesCount`. Link: `traceId`, `spanId`, `traceState`, `attributes`,
  `droppedAttributesCount`, `flags`.

IDs are nonzero hex strings (32 characters for trace IDs, 16 for span IDs;
either letter case). Parent ID may be absent or empty. Timestamps are required
on spans and must be decimal uint64 integers or integer strings, without leading
zeros, fractional parts, exponents or float coercion. End must be >= start.
Flags and dropped counts are uint32 JSON integers. Attribute `intValue` is a
signed int64 integer or decimal string. Enums are JSON integers, not booleans.

Attributes are `{ "key": "...", "value": { "stringValue": "..." } }` entries.
Keys must be nonempty and unique within a list. Every AnyValue needs exactly one
of `stringValue`, `boolValue`, `intValue`, `doubleValue`, `bytesValue`, `arrayValue`
or `kvlistValue`. Bytes must be valid base64; arrays and key-value lists have a
`values` array. Empty array/kvlist objects default to empty values; empty AnyValue
objects are unsupported. Attribute nesting is limited to 16. Finite numeric
double values are supported up to magnitude 1.7976931348623157e308; nonzero values
below 1e-324 are rejected. JSON fractional number tokens are limited to 128
characters and exponent -400..400. Custom double labels preserve the parsed
JSON decimal value, without binary-float rounding or claiming full IEEE-754
normalization.

Bounds are 1–100 strictly increasing nonnegative decimal seconds; zero is allowed.
A nonzero bound must lie in [1e-30, 1e30]. Each token is at most 64 characters,
with exponent -30..30 if supplied. `+Inf` is added automatically and must not be
specified. Exact decimal bounds and integer-nanosecond sums are emitted without
binary rounding. Prometheus consumers use floating point and may round these
numbers or distinguish boundaries differently in calculations.

## Duplicates, reports and limits

Identity is the lowercased `(traceId, spanId)` pair. A repeat is deduplicated only
if the decoded span, resource, scope and their schema URLs match. Comparison is
independent of object key order, whitespace and JSON string escape spelling;
array order, explicit versus omitted fields, JSON types and parsed decimal
precision/exponent remain significant. ID case variations share identity but
conflict if their full records differ. Changes to unselected attributes,
status messages, parentage, events or metadata also conflict. This intentionally
conservative policy can reject semantically equivalent re-encodings.

Any invalid span, conflicting duplicate or series overflow rejects the whole
conversion: **no new `.prom` file is emitted**. Independent valid records are
still checked for the report. With `--force`, an older `.prom` remains unchanged
on rejection, and its report is replaced with a rejection report. Check the exit
code, `status` and `metrics_emitted` before consuming files.

The JSON manifest includes:

- Input SHA-256, output SHA-256 on success, tool/report versions and dimensions.
- `input_records`, accepted first `unique_records`, exact `duplicate_records`,
  and `rejected_records`. When `record_counts_complete=true`, these three outcome
  counts sum to `input_records`. Only accepted first records enter deduplication;
  over-cap/invalid records are rejected, including their later repeats.
- Error count, exact decimal duration sum, groups, and prospective time-series
  count for accepted unique records. Totals on failure are diagnostic, not metrics.
- Sorted omitted attribute keys by source, counts of nonempty omitted span fields
  (events/links count entries; other fields count unique spans), and explicit
  aggregation losses. Only accepted unique records contribute to these lists.
- Up to 100 diagnostics, with an exact `diagnostics_omitted` count after that.

JSON/envelope/resource/scope errors or file/span caps can prevent reliable record
classification. These report `record_counts_complete=false`; zero record counts
then do not mean an empty capture. Bad command options, unsafe paths or I/O
failures print an error and do not promise a report file.

Default limits are 64 MiB input (`--max-input-bytes`), 100,000 input records
including duplicates (`--max-spans`), and 10,000 actual output time series
(`--max-series`), plus 1 MiB per JSONL line (`--max-line-bytes`, ignored for JSON).
A group costs **number of finite bounds + 5** series: calls,
errors, finite buckets, +Inf, count and sum. All dimensions count toward this cap;
no overflow group or truncation silently merges them. Parsing is in memory and
is intended for bounded fixtures, not an untrusted-input sandbox.

Outputs are local regular files. Input/output/report aliases (including hardlinks)
and symlink destinations are refused. Each output file is atomically published
from a staged file; the pair is not a single filesystem transaction. An I/O error
or interruption can leave only one new file. A successful exit plus the matching
`prometheus_sha256` identifies a complete pair.

Exit codes: **0** successful conversion/dry-run; **1** rejected capture (JSON
report); **2** invalid command, path or I/O failure (stderr).

## Interpretation and limitations

These are counts of captured spans, including children and every span kind.
They are not necessarily incoming requests. A static capture cannot establish
traffic rate, recover dropped data or compensate for head/tail sampling. IDs,
parentage, unselected attributes, scope metadata, trace state, status messages,
events and links do not survive aggregation as metrics; the report makes these
losses explicit. The manifest does not archive the original trace.

No online collection/exporting, sampling correction, exemplars, exponential
histograms, throughput claims or full equivalence to Collector feature gates is
provided. No restricted upstream server source is copied.

## Tests

The suite in `tests/spanmetrics/` performs actual CLI subprocess runs from realistic
OTLP JSON to saved `.prom` and JSON files. The independent `prometheus_client` parser
checks all labels, per-status counts, cumulative buckets/count/sum, and the output hash.
Other tests cover nanosecond precision, uint64 boundaries, status defaults,
parentage, duplicates/conflicts, allowlist cardinality, malformed input, bounded
counts, dry runs and overwrite/alias protection.
Comparison tests independently enumerate all source combinations in the six-span
corpus and match both configurations' full sample identities against that parser.
They also cover dimension removals/replacements/renames, source and delimiter
collisions, deterministic attribution, empty captures and complete count suppression
on rejection in either configuration.
JSONL tests compare the split fixture's complete exposition and report totals to
the single request, and cover cross-line duplicates/context conflicts, malformed
interior lines, UTF-8 line boundaries, global limits, comparison attribution and
preservation of existing metrics on late rejection through the CLI.
Dimension-file tests prove report equality with equivalent flags, canonical
ordering, independent source/key identities, strict JSON/schema/type/version
validation, label/source collisions, exact configuration byte/dimension limits,
option exclusivity and output alias protection.
Capture-free validation tests exercise that same strict matrix, canonical counts,
empty selections, source identities, file limits and argument rejection; read and
publication guards verify that inspection never consumes telemetry or stdin.
Stdin tests cover both explicit source forms, exact byte/UTF-8 line limits, short
reads and EOF before publication, cap-plus-one early stop without a hash,
capture-wide duplicate/span/series enforcement, redirected input alias protection,
I/O rejection and parity with file conversion/comparison.

## Saved Jaeger export → replayable metric review

`otelwrench spanmetrics` accepts OTLP only; [`otelwrench tracepack`](tracepack.md)
owns Jaeger/Zipkin conversion. `examples/spanmetrics/tracepack-review/replay.py` is an
integration recipe that chains the two. It runs both tools as subprocesses of one
Python environment (`python -m otelwrench tracepack ...` and
`python -m otelwrench spanmetrics ...`); it never implements conversion itself and
installs nothing. The interpreter running the recipe needs `prometheus-client`,
which `pip install otelwrench` already provides.

```sh
python examples/spanmetrics/tracepack-review/replay.py \
  --side candidate --output-dir /tmp/spanmetrics-review
```

| Option | Meaning |
| --- | --- |
| `--side baseline\|candidate` | Required. The reviewed pair side used for the final selected-side conversion. |
| `--output-dir DIR` | Required. A new directory outside the repository, with an existing parent. An existing directory is refused without modifying it. |
| `--bundle FILE` | Optional. Verify a saved tracepack bundle against the shipped exact Jaeger source instead of creating one. |
| `--python INTERPRETER` | Optional. Interpreter of the environment where otelwrench is installed (default: the one running the recipe). |

The shipped **synthetic Jaeger** fixture and independently written native OTLP
control contain three distinct spans in two services. The pair uses mandatory
labels only for baseline and adds `span:customer=customer` for candidate. Bounds
are `0.1,0.5,1` seconds. The independent hand calculation is:

| service / operation | durations (seconds) | calls | errors | sum | cumulative buckets 0.1 / 0.5 / 1 / +Inf |
| --- | --- | ---: | ---: | ---: | --- |
| web / GET /shop | 0.1, 0.3 | 2 | 0 | 0.4 | 1 / 2 / 2 / 2 |
| db / SELECT | 0.5 | 1 | 1 | 0.5 | 0 / 1 / 1 / 1 |

Candidate splits the web group into customers `s:a` (0.1 seconds) and `s:b`
(0.3 seconds); db has `s:a`. Baseline is **2 groups / 16 series**, candidate
**3 groups / 24 series**, delta **8**. All three spans total **0.9 seconds**.
The runner checks every sample's labels, counts, errors, exact decimal sum and
cumulative bucket using the third-party Prometheus text parser plus raw decimal
sample tokens. Native and converted OTLP produce byte-identical `.prom` files
for the selected side and matching comparison attribution for both sides.

The subprocess flow is `tracepack bundle` → `tracepack verify --source` →
`tracepack extract` → `tracepack convert --to otlp` → `spanmetrics compare` →
selected-side `spanmetrics` conversion. Bundle members are not consumed until
tracepack's full hash/replay/exact-source verification succeeds. The original
Jaeger query's `total` metadata is intentionally lost: `conversion-loss.json` and
`review.json.loss_report` retain the complete `$.total: Jaeger query metadata dropped`
report. Losses are visible and accepted for this example, so bundle/convert do not
request strict-loss. Conversion output and its complete loss report must equal the
verified bundle members.

The review directory retains exact input/config snapshots, `bundle.json`,
`normalized.json`, both comparisons, `selected.prom` and `native.prom` plus
manifests. `review.json` records source/normalized/config/bundle/member/output
hashes, the otelwrench version both commands ran from, Python and parser versions,
exact subprocess argv/cwd/exit codes and raw stdout/stderr paths under `logs/`.
Only exit **0** with `status="verified"` and `business_oracle="passed"` identifies
a successful recipe. Failure exits **1**, preserves raw evidence and writes
`status="rejected"`; late failures can leave intermediate files, so do not infer
success from their presence. Invalid/missing arguments exit **2**.

With `--bundle FILE`, changing `normalized` or removing `lossReport.losses` fails
replay verification even after recomputing its member hash; no convert/compare/metric
process runs and no final metrics are generated on that failure.
`tests/spanmetrics/test_tracepack_recipe.py` exercises both sides, both rehashed
tamper cases and existing-output refusal. It runs automatically when
`otelwrench tracepack` is part of the installed build and is skipped otherwise. Set
`RECIPE_EVIDENCE_DIR` to retain subprocess evidence instead of temporary cleanup.

This recipe is deliberately a fixed review corpus, not an arbitrary-capture
converter or production estimator. Verification establishes reproducibility and
agreement with saved source bytes, not authenticity, signatures or a concurrent
filesystem snapshot. Counts and label comparisons describe only captured spans.

## License

Apache-2.0, part of otelwrench; attribution and format references are in
[THIRD_PARTY.md](../THIRD_PARTY.md).
