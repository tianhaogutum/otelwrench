# otelwrench promdiff

Catch breaking metric changes before a release reaches Prometheus.
`otelwrench promdiff` compares two saved Prometheus 0.0.4 text snapshots (for
example, `/metrics` from the current release and from a candidate build) and
reports removed families, changed types and sample label-key schemas, metadata
changes, and observed series growth as an offline CI contract. Ordinary sample
values and timestamps never appear in the contract and never cause a schema
violation.

The command only reads its inputs and writes its report to stdout/stderr. No API
keys, network endpoints, live scrapes, services, or production data are needed.
There is no shell execution, input rewriting, or separate `--dry-run` mode. Its
only third-party dependency, `prometheus-client` (validated with 0.25.0), is a
core dependency of otelwrench.

## Quick start

```sh
pip install otelwrench
git clone https://github.com/tianhaogutum/otelwrench && cd otelwrench

otelwrench promdiff examples/promdiff/before.prom examples/promdiff/compatible.prom --format json
```

Usage summary (`otelwrench promdiff --help` lists every flag):

```text
otelwrench promdiff BASELINE.prom CANDIDATE.prom
    [--format text|json] [--config POLICY.json] [--config-overlay PATH ...]
    [--allowlist EXPECTED.json] [--rename-map RENAMES.json]
    [--include-raw-changes] [--validate-composites] [--version]
```

`otelwrench promdiff --version` prints `otelwrench promdiff 0.1.0`. Text reports
are the default.

Real examples (all bundled data is synthetic):

```sh
# Value-only differences: exit 0, status pass, two families / three series.
otelwrench promdiff examples/promdiff/before.prom examples/promdiff/compatible.prom

# Adds region to queue_depth's label keys: exit 1.
otelwrench promdiff examples/promdiff/before.prom examples/promdiff/breaking.prom

# Reviewed, exact schema exception: exit 0 with two series churn warnings.
otelwrench promdiff examples/promdiff/before.prom examples/promdiff/breaking.prom \
  --allowlist examples/promdiff/expected-changes.json --format json

# Apply a stricter missing-series policy and 10% growth budget.
otelwrench promdiff examples/promdiff/before.prom examples/promdiff/compatible.prom \
  --config examples/promdiff/policy.json

# Counter, classic histogram, summary and escaped labels: exit 0.
otelwrench promdiff examples/promdiff/classic.prom examples/promdiff/classic.prom

# Also validate numerical consistency of classic composites: exit 0.
otelwrench promdiff examples/promdiff/classic.prom examples/promdiff/classic.prom --validate-composites

# Invalid escape: exit 2, diagnostic names the input line.
otelwrench promdiff examples/promdiff/before.prom examples/promdiff/malformed.prom --format json
```

The breaking example's relevant change is:

```json
{
  "kind": "label_schema_changed",
  "family": "queue_depth",
  "before": [{"name": "queue_depth", "label_keys": ["queue"]}],
  "after": [{"name": "queue_depth", "label_keys": ["queue", "region"]}],
  "severity": "fail",
  "allowed": false
}
```

## Contract and series semantics

Each family retains its **wire name**, type, decoded HELP (null if absent),
unit, and sorted sample-name/label-key contracts. Counter `requests_total`
remains `requests_total`, even though the Python client's internal representation
normalizes counters. A classic histogram's `_bucket`, `_sum`, `_count` samples
belong to its declared parent family; the same applies to summary quantiles,
`_sum`, and `_count`. Suffixes alone are never guessed into composite families:
without TYPE, samples are untyped. Python client `_created` **gauge** families
are retained separately, as actually declared in its 0.0.4 output.

0.0.4 carries no unit metadata: `unit` is always null. The tool does not infer
units from names; OpenMetrics `# UNIT` is rejected. No unit data is discarded
from an accepted input.

An observed series is `(wire sample name, sorted decoded label key/value pairs)`;
values and timestamps are excluded. Every bucket/quantile/component is a real
observed series for the growth budget, but never an unrelated metric family.
Label order and, by default, sample order within a family do not affect the
report. An explicit empty-valued label remains a present label key; contracts describe exactly what
was exported, rather than applying Prometheus ingestion normalization.

`new` and `missing` are set differences between the two snapshots, both globally
and per family. Label **values** changing causes observed series churn, not a
schema violation. A missing series in a still-observed family is a warning by
default, not proof an instrument was removed from production. A family disappears
only if both its samples and declarations disappear. Metadata-only families are
preserved; if their sample schema is no longer observed, that empty schema is
reported as a schema change (conservative default: fail). Changes to a histogram
bucket boundary or a summary quantile value are series churn, not label-key
schema changes; this tool does not analyze their semantic suitability.

## Optional classic composite validation

`--validate-composites` validates **both** inputs before compatibility comparison.
The library equivalent is `parse_snapshot(text, source, validate_composites=True)`.
It adds these checks to the always-on composite structure checks:

- For each histogram base-label group, `le` boundaries must be strictly
  increasing in **wire order**, including a required terminal `le="+Inf"`.
  Numerically equal spellings such as `1` and `1.0` are duplicate boundaries.
  Groups may be interleaved; each group's bucket subsequence is checked separately.
- Histogram bucket counts and histogram/summary `_count` values must be finite,
  nonnegative integers. Cumulative bucket counts must never decrease, and the
  `+Inf` bucket must equal the same group's `_count`.
- Summary quantile labels must lie in the inclusive range `[0,1]`, using their
  exact decimal values. Labels such as `1.00000000000000001` and `-1e-9999`
  are rejected even though binary float screening rounds them to an endpoint.
  Endpoints, signed zero and in-range scientific/precise decimals are valid.
- Summary quantile labels must be numerically unique within each base-label
  group: `0.5`, `0.50` and `5e-1` are equivalent, as are `0` and `-0.0`.
  Quantiles need no particular order; the same quantile in different groups is
  valid. A summary with only `_sum` and `_count` remains valid.
- Every observed histogram/summary group still requires explicit `_sum` and
  `_count` samples. Missing components get the family's name and the first line
  of the incomplete group; other errors identify the offending sample line and,
  where relevant, the related bucket/count/quantile line. Raw label values are not included.

For example, this synthetic histogram has an impossible cumulative count:

```text
# TYPE h histogram
h_bucket{le="1"} 3
h_bucket{le="+Inf"} 2
h_sum 1.5
h_count 2
```

```sh
otelwrench promdiff examples/promdiff/classic.prom examples/promdiff/inconsistent-histogram.prom \
  --validate-composites --format json
```

This exits **2**, leaves stdout empty, and writes a JSON error to stderr containing
`examples/promdiff/inconsistent-histogram.prom:3: h: cumulative bucket count decreases (previous bucket at line 2)`.
Changing the first bucket count to `1` makes this histogram valid. Comparing the
corrected fixture with itself exits 0 with status `pass` and no changes.

The bundled `examples/promdiff/inconsistent-summary.prom` contains `quantile="0.5"` at
line 2 and `quantile="0.50"` at line 3 in the same summary group:

```sh
otelwrench promdiff examples/promdiff/inconsistent-summary.prom examples/promdiff/inconsistent-summary.prom \
  --validate-composites --format json
```

This exits **2**, leaves stdout empty, and writes a JSON error to stderr containing
`examples/promdiff/inconsistent-summary.prom:3: s: numerically duplicate quantile (previous quantile at line 2)`.
Removing line 3 makes the summary valid; comparing it with itself exits 0 with
status `pass` and no changes. Without `--validate-composites`, the original
fixture compared with itself also exits 0: the default preserves distinct wire
label spellings as separate observed series. Validation never rewrites them.

Checks compare exact wire decimals, without binary float rounding; exponents
outside Python Decimal's representable range are input errors in this mode.
Decimal conversion uses a private error context: a library caller disabling
the `decimal.InvalidOperation` trap still gets the same `InputError` for
unsupported bucket boundaries, quantile labels and count exponents. Conversion
does not change the caller's Decimal settings or flags, and does not round
wire values to the caller's precision or exponent limits.
For summary quantile labels, this representability check precedes binary float
range screening: both `1e99999999999999999999` and `1e-99999999999999999999`
at line 2 produce `source:2: s: numeric exponent outside supported decimal range`
with `--validate-composites`. Comparing either invalid fixture with itself, or
using it as either input, exits **2** with empty stdout; `--format json` writes
an error object to stderr and `--format text` writes an `otelwrench promdiff:` diagnostic.
The diagnostic is the same for both signs of exponent. Without the option, positive overflow
still gets `invalid quantile value`, and negative underflow may parse as zero.
For example, comparing a summary containing `s{quantile="1.00000000000000001"} 1`
at line 2 with itself using `--validate-composites --format json` exits **2**,
leaves stdout empty, and writes a JSON error to stderr containing
`source:2: s: invalid quantile value (exact label outside [0,1])`, where `source`
is the input path. The same applies to `quantile="-1e-9999"`. Without the option,
the existing binary float range screening remains unchanged; these two fixtures
are accepted and their self comparisons exit 0. Validation retains the original
wire labels and series identities.
Values are discarded after validation and remain absent from contracts/reports.
No missing bucket, quantile, observation, or label group is synthesized. A
metadata-only family has no observations to validate; summaries need no quantile
samples. `_sum` and quantile sample values (including NaN), quantile ordering,
nonnegative ordinary counters, and cross-snapshot numerical trends are outside
these checks. This option does not broaden the supported exposition format.

## Policy and growth budget

`--config` accepts a strict JSON object containing either or both of
`policy` and `max_series_growth_percent`. Omitted policy classes keep defaults:

| Change class | Default | Meaning of before / after |
| --- | --- | --- |
| `family_added` | warn | null / complete contract |
| `family_removed` | fail | complete contract / null |
| `type_changed` | fail | old / new type |
| `help_changed` | warn | old / new decoded HELP, including null |
| `label_schema_changed` | fail | old / new sorted sample schema lists |
| `series_added` | warn | 0 / new observed identity count in a common family |
| `series_missing` | warn | missing observed identity count / 0 in a common family |
| `series_growth` | fail | baseline / candidate total observed series |

Only `warn` and `fail` are accepted. Unknown keys, duplicate JSON keys,
non-finite numbers and malformed configuration are errors; there is no silent
fallback. All changes remain visible even when allowed or downgraded.

Repeat `--config-overlay PATH` to compose local strict JSON configuration files:

```sh
otelwrench promdiff before.prom after.prom --config base.json \
  --config-overlay overlay.json --config-overlay team.json \
  --format json --include-raw-changes
```

The base is `Config()` defaults unless `--config` is supplied. The base file
always loads first, regardless of its flag position; overlays then apply in
their command-line order. Each overlay uses the same `policy` /
`max_series_growth_percent` schema. Later supplied policy entries or budgets
replace earlier values; omitted entries and budgets persist, including exact
numeric evidence. `{}` and `{"policy":{}}` retain all current values. Overlays
also work without `--config`. Each invocation starts with independent policy
state; no files are written.

For example, let `base.json` contain
`{"max_series_growth_percent":20,"policy":{"family_removed":"warn"}}`,
`overlay.json` contain
`{"max_series_growth_percent":0.099999999999999999,"policy":{"series_growth":"warn"}}`
and `team.json` contain `{}`. For 1000 -> 1001 series in the same gauge family,
this exits **0**, status `warn`, retains `family_removed: "warn"`, and reports
`growth_limit_exceeded: true`,
`max_growth_percent_exact: "0.099999999999999999"`, and `series_growth` /
`series_added` in both changes and raw changes. A final overlay supplying
`{"max_series_growth_percent":0.1}` instead removes the growth event and exact
evidence. Supplied precise/scientific/extreme-underflow budgets use the same
exact validation as `--config` and the library file overlay API.

Every base/overlay file has its own **64 MiB** cap, enforced before strict
UTF-8 and JSON decoding, with at most cap + 1 bytes read. The first invalid
base or overlay stops processing before comparison: exit **2**, empty stdout,
and the existing JSON/text diagnostic on stderr. Later overlays cannot repair
an invalid earlier file. Read/UTF-8/oversize/JSON errors name the failing path;
schema errors retain their field labels. Missing files, malformed/duplicate
JSON, unknown fields/classes, invalid severities and exact out-of-range budgets
are errors. The cap bounds individual reads, not aggregate bytes, execution time
or total memory. Effective configuration controls policy, projected allowlist
matching and raw growth evidence. Omitting overlays preserves report schema and
output bytes.

The default maximum growth is **20%**, with an inclusive threshold. Let B and C
be baseline and candidate total observed series. For B > 0, the gate fails when
`100 * max(0, C - B) > limit * B`. This is **net positive growth**, not the
number of new identities; replacing every label value with the same cardinality
does not exceed it. The displayed `net_growth_percent` is clamped to zero for
shrinkage and rounded half-even to six decimal places; the decision uses unrounded
exact rational arithmetic. Limits must be finite JSON numbers in `0..1000000`;
the CLI checks this range before float rounding can hide an out-of-range value.
For example, `-1e-9999` and `1000000.00000000001` are configuration errors
(exit 2, empty stdout, JSON error on stderr with `--format json`). Both endpoints,
signed zero and in-range scientific notation are accepted. The CLI uses the exact
numeric value of the raw JSON limit token for the budget decision: for 1000 ->
1001 series, `0.1` (or `1e-1`) accepts the inclusive 0.1% boundary (exit 0, warn),
while `0.099999999999999999` fails (exit 1, `series_growth` plus `series_added`).
Positive values too tiny for a float remain valid; any positive observed growth
exceeds them. Zero growth never exceeds a nonnegative limit.

Report version **1** retains `observed_series.max_growth_percent` as the finite
Python int/float display, which can round the configured threshold. When its
shortest decimal representation differs numerically from the CLI token, the
report adds optional `observed_series.max_growth_percent_exact`: a string
containing that original JSON numeric token. In the failing example above both
`max_growth_percent` and `net_growth_percent` display as `0.1`, but
`max_growth_percent_exact` is `"0.099999999999999999"` and
`growth_limit_exceeded` is true. Consumers reconstructing the budget must prefer
this exact string when present. Text output adds `Exact growth budget: <token>%`
in the same cases. Ordinary thresholds retain their existing output bytes;
no token field is added for numerically equivalent spellings such as `1e-1` or
signed zero. This is additive evidence in version 1, with no event-schema change.
Allowlist and rename-map JSON parsing is unchanged.

Library callers can load the same strict, exact JSON configuration from text:

```python
from otelwrench.promdiff.compare import Config, compare
from otelwrench.promdiff.parser import InputError, parse_snapshot

config = Config.from_json('{"max_series_growth_percent":0.099999999999999999}')
report = compare(parse_snapshot(baseline_text), parse_snapshot(candidate_text),
                 config, include_raw_changes=True)
```

For 1000 -> 1001 series this reports `status: "fail"`,
`growth_limit_exceeded: true`, numeric growth and budget displays of `0.1`, and
`max_growth_percent_exact: "0.099999999999999999"`. Both `changes` and
`raw_changes` contain `series_growth` followed by `series_added`.
`Config.from_json(text, *, source="config")` accepts a **string** and returns a
`Config`; it performs no file I/O or text size limiting. Invalid JSON, non-string
input, duplicate keys at any depth, nonfinite numbers, overflow, unknown fields,
invalid policy/type and exact out-of-range limits raise `InputError`. Decode
diagnostics use the optional `source` label; existing config schema diagnostics
retain their field names. The CLI uses this same loader after its existing
UTF-8/64 MiB file read. JSON scientific notation, signed zero, inclusive endpoints
and extreme positive underflow follow the growth/report contract above.

`Config.from_file(path, *, max_bytes=64 * 1024 * 1024) -> Config` loads a
standalone configuration from a local strict UTF-8 JSON file. It accepts a
`str` or `pathlib.Path`, shares the bounded reader and argument rules of
`with_overrides_file()` below, and calls `from_json()` with the path as source.
Absent fields use defaults; partial policies merge defaults. Each load returns
an independent configuration and policy dictionary.

```python
from pathlib import Path

# policy.json contains:
# {"max_series_growth_percent":0.099999999999999999,
#  "policy":{"family_removed":"warn","series_growth":"warn"}}
config = Config.from_file(Path("policy.json"))
report = compare(parse_snapshot(baseline_text), parse_snapshot(candidate_text),
                 config, include_raw_changes=True)
exported = config.to_json()  # Caller can save this for CLI --config.
assert Config.from_json(exported).to_json() == exported
```

For 1000 -> 1001 series this yields `status: "warn"`,
`growth_limit_exceeded: true`, `max_growth_percent_exact: "0.099999999999999999"`
and `series_growth` / `series_added` events. Full policies, precise/scientific
budgets, signed zero and extreme positive underflow have the same exact
semantics and stable export/reload as text loading.

Validation checks `max_bytes` first (plain integer in `0..sys.maxsize - 1`),
then path type/NUL, bounded binary read, strict UTF-8, JSON decoding and schema.
It reads at most `max_bytes + 1` bytes, rejects oversize before UTF-8 decoding,
and performs no writes or newline/BOM normalization. Invalid arguments, I/O,
UTF-8, oversize and all `from_json()` failures raise `InputError`. Read/decode
diagnostics name the path; schema diagnostics retain field names. For example,
`{"policy":{}}` with `max_bytes=8` fails with a path-labeled 8 byte limit error.
The default is **64 MiB**; zero allows only empty input, which fails JSON
decoding. Success and failure preserve caller Decimal settings, traps and flags.
The cap bounds bytes read, not total memory or execution time; no file-type
restriction or timeout is imposed. Text APIs, overlay APIs and CLI diagnostics,
limits and report schemas retain their existing contracts.

`Config.to_json() -> str` exports the effective configuration as compact JSON
with lexicographically sorted object keys, no insignificant whitespace or trailing
newline, and the full policy (including merged defaults). A partial policy on a
manually constructed `Config` is merged just as in `from_dict()`; export does not
mutate the instance or its policy. Invalid constructed or mutated policy/limit
values raise `InputError` using the existing config validation.

```python
exported = config.to_json()
restored = Config.from_json(exported)
assert compare(parse_snapshot(baseline_text), parse_snapshot(candidate_text),
               restored, include_raw_changes=True) == report
```

The growth limit is a JSON **number**, never a quoted string. If the loaded
limit requires exact report evidence, export retains its original numeric token
(including scientific or extreme positive-underflow spelling). Other int/float
limits use Python's JSON spelling, retaining int versus float and float signed
zero. Equivalent spellings such as `1e-1` may become `0.1`; export promises exact
budget semantics and required report evidence, rather than original text layout.
Repeated export and export/reload/export are byte-stable, independent of policy
insertion order. This API performs no file I/O, adds no report fields and preserves
the caller's Decimal settings, traps and flags on success and failure.

`Config.with_overrides(data) -> Config` returns a validated independent copy.
`data` uses the `from_dict()` schema: only `policy` and
`max_series_growth_percent` are accepted. Partial policy entries overlay the
current effective policy, including defaults for a manually constructed partial
policy. Omitted fields and an empty policy retain current values; `{}` still
returns a fresh instance and policy dictionary. Successive overrides build on
the previous result without mutating or aliasing either input policy.

```python
config = Config.from_json('{"max_series_growth_percent":0.099999999999999999,'
                          '"policy":{"family_removed":"warn"}}')
derived = config.with_overrides({"policy": {"series_growth": "warn"}})
assert config.policy["series_growth"] == "fail"
assert derived.policy["family_removed"] == "warn"
report = compare(parse_snapshot(baseline_text), parse_snapshot(candidate_text),
                 derived, include_raw_changes=True)
```

For 1000 -> 1001 series this produces `status: "warn"` while retaining
`growth_limit_exceeded: true`, `max_growth_percent_exact: "0.099999999999999999"`
and the `series_growth` / `series_added` events. An omitted limit retains its
exact budget and evidence, including scientific and extreme-underflow tokens.
A supplied limit **replaces** it using `from_dict()` Python int/float semantics,
including inclusive endpoints and signed zero; an already rounded float cannot
retain the old token's precision or evidence. Both the base state and overrides
must validate: non-object overrides, unknown fields/classes, invalid severities,
non-numeric/nonfinite/out-of-range limits and invalid constructed or mutated
base state raise `InputError`. Overrides cannot repair an invalid base. Failure
leaves both inputs unchanged. Export/reload preserves the derived budget, policy
and reports; overriding preserves the caller's Decimal settings, traps and flags.

`Config.with_overrides_json(text, *, source="config overrides") -> Config`
accepts strict JSON **text** for the same independent overlay operation, with
exact JSON replacement budgets. It shares `from_json()` decoding and
`with_overrides()` validation/merging rules; it does no file I/O or text size
limiting. Callers must bound untrusted text before passing it to this API.

```python
base = Config.from_json('{"max_series_growth_percent":20,'
                        '"policy":{"family_removed":"warn"}}')
derived = base.with_overrides_json(
    '{"max_series_growth_percent":0.099999999999999999,'
    '"policy":{"series_growth":"warn"}}')
report = compare(parse_snapshot(baseline_text), parse_snapshot(candidate_text),
                 derived, include_raw_changes=True)
assert base.max_series_growth_percent == 20
assert base.policy["series_growth"] == "fail"
assert derived.policy["family_removed"] == "warn"
```

For 1000 -> 1001 series this yields `status: "warn"`,
`growth_limit_exceeded: true`, `max_growth_percent_exact: "0.099999999999999999"`
and `series_growth` / `series_added` events. Supplied numeric limits replace the
base budget with `from_json()` exact semantics, including precise/scientific
numbers, extreme positive underflow, signed zero and inclusive endpoints.
Omitted values retain the base budget and exact evidence. Empty/successive
text overlays keep independent policies; exact export/reload remains stable.

Non-string, malformed/recursive/duplicate/nonfinite JSON, unknown fields/classes,
invalid severities/types and exact out-of-range numeric tokens raise `InputError`
without mutation or changes to the caller's Decimal settings, traps or flags.
Decoding happens first and diagnostics use `source`; after decoding, existing
schema diagnostics and base validation follow `with_overrides()`. A constructed
or mutated invalid base cannot be repaired by text overlays. Dictionary override
semantics, CLI schemas, projection, allowlists and raw reports are unchanged.

`Config.with_overrides_file(path, *, max_bytes=64 * 1024 * 1024) -> Config`
applies the same exact JSON overlay from a local UTF-8 file. Accept a `str` or
`pathlib.Path`; the default byte cap is the CLI's **64 MiB** boundary. It shares
the CLI's bounded reader and calls `with_overrides_json()` with the path as its
source. It opens only for binary reading, reads at most `max_bytes + 1` bytes,
rejects oversize input before decoding, and decodes strict UTF-8 without stripping
a BOM or normalizing newlines. It performs no file writes.

```python
from pathlib import Path

# overrides.json contains the exact JSON overlay in the preceding example.
derived = base.with_overrides_file(Path("overrides.json"), max_bytes=1024)
assert derived.policy["family_removed"] == "warn"
assert base.max_series_growth_percent == 20
exported = derived.to_json()  # Optional export; CLI can use --config-overlay directly.
```

`max_bytes` must be a plain integer in `0..sys.maxsize - 1`; booleans, negative
limits and other types raise `InputError` before opening the file. Zero permits
only an empty byte stream, which subsequently fails JSON decoding. Non-`str`/`Path`
paths and embedded NUL characters also raise `InputError` before opening.
Missing/unreadable files, directories, invalid UTF-8 and oversize files raise
`InputError` naming the path; oversize diagnostics include the byte limit. For
example, `{"policy":{}}` with `max_bytes=8` exceeds the 8 byte limit.

Arguments and file read/decode are checked before JSON decoding and base/schema
validation. JSON decoding diagnostics name the path; base/schema diagnostics
retain the existing field names. Invalid bases remain unrepairable, and failures
leave base state and caller Decimal settings/traps/flags unchanged. Omitted values
and exact evidence persist, supplied precise/scientific/underflow budgets replace
exactly, and empty/successive overlays retain independent policies and stable
export/reload. This cap bounds bytes read, not execution time or total memory;
platform/resource limits may still prevent extremely large reads. Text APIs
continue to leave size limiting to callers. CLI limits and diagnostics are unchanged.

`Config` and `Config.from_dict()` still accept Python int/float limits, with their
decimal string representation defining the threshold. Use `from_json()` before
a general JSON decoder rounds a numeric token: `from_dict(json.loads(text))`
cannot recover that original precision. CLI parsing and library `compare()`
produce the same growth decision and display regardless of the caller's Decimal
precision, rounding or traps, and leaves its context settings and flags unchanged.
For example, 3 -> 4 series at a 33% limit fails with `net_growth_percent: 33.333333`,
including inside `decimal.localcontext()` with precision 1.

Zero baseline is explicit: 0 -> 0 has 0% growth; 0 -> any positive number exceeds
every finite limit, reports `net_growth_percent: null` and
`zero_baseline_growth: true`. To bootstrap intentionally, allow the exact
`series_growth` event or set that class to warn. Added/removed families also
contribute to the global count; their identities are included in the totals
without duplicate `series_added`/`series_missing` events.

## Exact expected changes

`--allowlist` takes a JSON **array**, with no glob, regex, prefix, or partial
match support. Each object must contain exactly `kind`, `family`, `before`,
`after`; copy those four fields from a reviewed JSON report. For example,
`examples/promdiff/expected-changes.json` allows only the exact queue-depth schema change
shown above. It does not allow an unrelated type/removal change or future
additional label keys. A growth exception has `family: null`, and exact baseline
and candidate counts. Series churn exceptions match counts, not specific label
values, because the event contract is an observed count.

Allowed events retain their severity/evidence and have `allowed: true`; only
unallowed failures cause exit 1. Duplicate, unused/stale or partially matching
allowlist entries are errors (exit 2). This catches changed expectations instead
of silently retaining stale exceptions. No exceptions are enabled by default.

## Explicit rename declarations

`--rename-map` accepts a strict JSON object with optional `metrics` and `labels`
objects. Names map **from baseline to candidate**, exactly and simultaneously;
there is no automatic rename inference. For example, the bundled snapshots are:

```text
# baseline
# TYPE old_requests_total counter
old_requests_total{verb="get"} 1

# candidate
# TYPE requests_total counter
requests_total{method="get"} 2
```

Declare their intended correspondence in `examples/promdiff/renames.json`:

```json
{
  "metrics": {"old_requests_total": "requests_total"},
  "labels": {"old_requests_total": {"verb": "method"}}
}
```

```sh
otelwrench promdiff examples/promdiff/rename-before.prom examples/promdiff/rename-after.prom \
  --rename-map examples/promdiff/renames.json --format json
```

This exits **0**, status `pass`, `changes: []`, and new/missing series counts both
zero. Omitting the map retains the existing remove/add result (exit 1). Unlisted
families and label keys keep their normal behavior. The library equivalent is
`compare(baseline, candidate, rename_map={...})`.

`metrics` maps **family wire names**. A declared histogram/summary family rename
carries its owned sample components (`_bucket`, `_sum`, `_count`, or summary
quantiles) with it. Counter names use the exact wire spelling, including `_total`
when present. Independently exported `_created` gauge families need their own
explicit entries; suffixes on untyped metrics are never interpreted as components.
`labels` is scoped by the **original baseline family name**, even when that family
is also renamed. Each scoped map applies to all samples in that family; label
values remain unchanged. Swaps/chains are simultaneous, not recursive substitutions.

All maps must be one-to-one within their scope, including unmapped baseline
names/keys. For example, `a -> b` collides if baseline `b` remains unmapped, and
`verb -> method` collides if both label keys already exist in that baseline
family. Composite parents reserve their component suffix names even without
samples: renaming a separate metric into a histogram's `_count` name is an error.
Mapped families are also checked against the candidate's reserved namespace.
Structural `le`/`quantile` labels cannot be renamed or targeted for a histogram/
summary on either side of the mapped family. Ordinary gauge labels with these
spellings are permitted; `__` label names are always reserved.

Many-to-one maps, collisions, identity entries (`a -> a`), unknown fields,
invalid/duplicate JSON keys, absent baseline source families, and unobserved
source label keys exit **2**, with empty stdout and a diagnostic on stderr
(JSON with `--format json`). A missing candidate target still reports removal;
a declaration does not manufacture observations. For example, replacing the
map's `"method"` target with `"__name__"` fails before any report is printed.

Declarations express the user's intended equivalence; they do not prove that
existing consumers support the new names. With a map, JSON adds
`declared_renames` separately from observed `changes`, and
`contracts.comparison_baseline` shows the projected contract used for comparison.
`contracts.baseline` and `contracts.candidate` retain the original wire contracts.
Text output prints `DECLARED metric ...` / `DECLARED label ...` lines. Observed
schema/type/HELP differences and identity churn still produce normal events;
growth still uses the unchanged observed counts. Events, per-family statistics,
and exact allowlist entries use candidate naming after baseline projection.
Declarations are neither allowlist exceptions nor policy events. Without a map,
the existing report shape and comparison behavior are unchanged.

### Optional wire-change evidence

Add `--include-raw-changes` to include the actual wire changes alongside the
projected comparison in the same artifact:

```sh
otelwrench promdiff examples/promdiff/rename-before.prom examples/promdiff/rename-after.prom \
  --rename-map examples/promdiff/renames.json --include-raw-changes --format json
```

This still exits **0**, status `pass`, with `changes: []`. It also adds
`raw_changes`: a sorted list containing `family_removed` for `old_requests_total`
and `family_added` for `requests_total`, with the complete original contracts in
their respective `before`/`after` fields. A label-only declaration instead shows
the original `label_schema_changed`, `series_added`, and `series_missing` events.
For example, declaring `queue`'s `old -> new` label-key rename yields:

```json
{
  "kind": "label_schema_changed",
  "family": "queue",
  "before": [{"name": "queue", "label_keys": ["old"]}],
  "after": [{"name": "queue", "label_keys": ["new"]}]
}
```

The library equivalent is `compare(baseline, candidate, rename_map={...},
include_raw_changes=True)`. Each raw event has exactly `kind`, `family`,
`before`, and `after`; there are no `severity` or `allowed` fields. These are
**informational observations** of the original snapshots, without projection or
allowlist application. They include all normal event classes, including unrelated
changes and `series_growth` under the configured growth threshold, sorted by
original family name (snapshot-wide events first), kind, and canonical event JSON.
They do not identify which individual declaration covers each event.

Only projected `changes` control policy, allowlist matching, summary, status,
and exit code. A raw-only allowlist entry is still an unused-entry error (exit 2).
Invalid input/maps and collisions still fail before any report is printed.
The option also works without a rename map, or with an empty map; raw events
then match the normal observations before policy/allowlist decoration. An empty
wire diff produces `raw_changes: []`. Omitting the option leaves the report
shape and text output unchanged. Text output adds a `RAW CHANGES (wire,
informational):` section with `RAW ...` event lines, or `(none)`.

Raw evidence contains contracts and churn **counts**, never label values,
sample values, or timestamps. It requires a second comparison over the parsed
snapshots; it does not reread or modify input files.

## Output and CI

`--format json` produces a deterministic, versioned report (`report_version: 1`)
with contracts, counts, effective policy, sorted changes and a summary. It
contains no clock time or file paths. JSON errors go to stderr with
`status: "error"`; stdout stays empty. Argument-parsing errors use argparse's
plain-text stderr diagnostics. Text reports are the default.

| Exit | Meaning |
| --- | --- |
| 0 | No unallowed fail events; status `pass` or `warn` |
| 1 | At least one unallowed fail event; status `fail` |
| 2 | Malformed/unsupported input, bad configuration, stale allowlist, I/O or CLI error |

For CI, capture stdout as an artifact and preserve the process exit code:

```sh
otelwrench promdiff baseline.prom candidate.prom --format json > report.json
```

## Supported subset and limitations

- Local, uncompressed UTF-8 files; LF line endings, final LF for nonempty files;
  maximum **64 MiB per input**, including base config, each config overlay,
  allowlist and rename map. Empty snapshots are
  valid. Snapshots and series identities are held in memory; there is no large
  scrape performance or memory bound beyond the input cap.
- Legacy ASCII metric/label names; Unicode is supported in label values and
  HELP. Quoted UTF-8 names, protobuf, gzip, exemplars, native histograms,
  OpenMetrics EOF/UNIT/CREATED directives and extra types are rejected. An
  OpenMetrics document consisting solely of the indistinguishable 0.0.4 subset
  cannot be identified by content alone; identified extensions are rejected.
- Gauge, counter, histogram, summary, untyped; optional signed int64 millisecond
  timestamps. Decimal/scientific values plus NaN and signed Inf are accepted.
  Label escapes are restricted to backslash, double quote and newline; HELP
  permits backslash/newline escapes. Unknown ordinary comments are ignored.
- One contiguous group per family, HELP/TYPE before samples, no duplicate
  metadata or duplicate observed series, consistent label keys per sample name.
  Composite components must have consistent base label keys; each observed
  group needs matching `_sum`/`_count`; histogram groups need `le="+Inf"`.
  Summaries may have no quantile samples. Ambiguous component ownership and
  inconsistent structures are errors, not partially parsed contracts.
- Numerical histogram/summary checks are opt-in via `--validate-composites`,
  as described above. The default checks structure only. Neither mode checks
  `_sum`/quantile sample values, nonnegative ordinary counters, or numerical
  trends between snapshots. Values/timestamps are never retained in an output
  snapshot or included in the compatibility contract.
- Only observed schemas are known. No rename inference, PromQL/rule analysis,
  live scrapes, remote writes, production cardinality estimates or assertion
  that two samples represent all production behavior. Reports can contain HELP
  text and label keys; raw label values are not emitted.

## Reproducible experiment and tests

```sh
python benchmarks/promdiff/run.py
# Optionally keep all generated snapshots and per-case CLI reports in a NEW dir:
python benchmarks/promdiff/run.py --output-dir /tmp/promdiff-experiment
python -m pytest tests/promdiff
```

Run these from a clone after `pip install -e ".[test]"`. The experiment generates
a baseline with 20 families / 100 series and nine known mutations. It invokes the
real CLI and independently checks exit codes, change classes, upstream parsed
sample counts, and new/missing counts against an explicit oracle. The comparison
baseline is **accept if both snapshots parse with prometheus_client**, not an
invocation or claimed emulation of promtool. Recorded results and rationale are
in `benchmarks/promdiff/results.json` and
[`benchmarks/promdiff/README.md`](../benchmarks/promdiff/README.md). This measures
compatibility detection, not runtime speedup or production impact. The tests in
`tests/promdiff/` also exercise actual client-generated counter/histogram/summary
exposition, malformed inputs, deterministic output, exact allowlists, growth
boundaries, and subprocess exits 0/1/2.

## References and attribution

Format references: [Prometheus exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/)
and [Python client parser](https://github.com/prometheus/client_python/blob/v0.25.0/prometheus_client/parser.py).
The upstream parser describes itself as permissive, so this command applies
additional grammar, duplicate and schema checks before trusting its output.
No Prometheus server, Loki, Mimir, Grafana or Sentry server source was copied.
`prometheus-client` includes Apache-2.0 and bundled BSD-2-Clause material; see
[THIRD_PARTY.md](../THIRD_PARTY.md) for attribution and license texts.
