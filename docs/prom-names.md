# otelwrench prom-names

Before you route OTLP metrics into Prometheus, check what the metric and label
names will actually become. `otelwrench prom-names` converts a saved OTLP JSON
**gauge/cumulative-sum snapshot** to Prometheus text, explains every metric/label
normalization, and rejects ambiguous names or series (collisions) instead of
silently merging them. Run it in CI on a captured snapshot when you change
instrumentation, units or the translation strategy.

Two naming strategies can be run against the same file for an offline comparison.
This is a deliberately limited local inspector; it does not replace the OTel
Collector translator or the Prometheus OTLP receiver. No server, credentials,
telemetry or network access is involved; all fixtures are synthetic. The only
third-party dependency is the `prometheus-client` parser (a core dependency of
otelwrench, validated with 0.25.0).

## Quick start

```sh
pip install otelwrench
git clone https://github.com/tianhaogutum/otelwrench && cd otelwrench

otelwrench prom-names convert examples/prom-names/metrics.json \
  --strategy UnderscoreEscapingWithSuffixes --output out.prom
```

Usage summary (see `otelwrench prom-names <subcommand> --help`):

```text
otelwrench prom-names convert INPUT (--output FILE | --bundle ZIP)
    [--strategy UnderscoreEscapingWithSuffixes|UnderscoreEscapingWithoutSuffixes]
    [--mapping FILE] [--promote-resource EXACT_KEY ...] [--allow-nonfinite]
    [--convert-unit ms:s] [--convert-metric EXACT_NAME ...] [--dry-run]
otelwrench prom-names verify BUNDLE [--input INPUT]
otelwrench prom-names inspect BUNDLE [--input INPUT] [--metric EXACT_NAME ...]
```

`--strategy` defaults to `UnderscoreEscapingWithSuffixes`. Exactly one of
`--output` or `--bundle` is required; `--mapping` (default `OUTPUT.mapping.json`)
works only with `--output`.

The last command writes `out.prom` and `out.prom.mapping.json` and reports
`5` samples on stderr. The request in the fixture becomes:

```prometheus
# HELP requests_total Synthetic request count
# TYPE requests_total counter
requests_total{route="/cart"} 7
```

Check the complete expected output in `examples/prom-names/with-suffixes.prom`.
Run the second strategy independently:

```sh
otelwrench prom-names convert examples/prom-names/metrics.json \
  --strategy UnderscoreEscapingWithoutSuffixes --output plain.prom
```

Here the request is `# TYPE requests counter` followed by
`requests{route="/cart"} 7`. The complete golden is
`examples/prom-names/without-suffixes.prom`. No-suffix strategy preserves an existing
normalized `_total`; it adds no unit or counter suffix.

Explicit resource promotion and an optional mapping destination:

```sh
otelwrench prom-names convert examples/prom-names/metrics.json \
  --promote-resource service.name --mapping promoted.mapping.json \
  --output promoted.prom
```

Repeat `--promote-resource EXACT_KEY` for each raw resource attribute key. No
implicit `job`, `instance`, `target_info`, scope labels or namespace is added.
Missing allowlisted keys are not fabricated, and can match zero resources.

```sh
otelwrench prom-names convert examples/prom-names/metrics.json \
  --output preview.prom --dry-run
```

Dry-run performs the full conversion/parser validation and prints mapping JSON
on stdout; it writes no files, does not evaluate input as code, and runs no
scripts. All path-alias protections also apply in dry-run.

## Supported OTLP JSON subset

Input is one UTF-8 JSON object with `resourceMetrics`, containing `resource`,
`scopeMetrics`, and `metrics`, using OTLP **lowerCamelCase** field names. This is
an ExportMetricsServiceRequest/MetricsData-shaped document, not NDJSON, protobuf,
an SDK object dump, or an HTTP wrapper. Omitted repeated fields mean empty
arrays; explicit nulls are rejected. `{}` is a valid empty snapshot.

- `gauge.dataPoints` supports exactly one `asInt` or `asDouble` per point.
- `sum.dataPoints` requires numeric `aggregationTemporality: 2` (cumulative).
  `isMonotonic: true` becomes a counter. False/omitted becomes a gauge, with a
  reported loss of sum semantics. A monotonic sum with a negative value fails.
- `asInt` and `intValue` accept signed int64 JSON integers or canonical decimal
  integer strings (no plus sign, fractional part, or redundant leading zeros).
  Integers remain exact in output text and mapping, including `9223372036854775807`.
- `asDouble` and `doubleValue` accept JSON numbers. Finite decimal text is retained
  exactly; sample values are scaled only with the explicit option below.
  Binary64 overflow and underflow to zero fail. Quoted
  finite doubles are rejected. OTLP's quoted `"NaN"`, `"Infinity"`, `"-Infinity"`
  require explicit `--allow-nonfinite`, including in attributes. Bare JSON
  `NaN`/`Infinity` are invalid. Opted-in samples use `NaN`, `+Inf`, `-Inf`.
- Attribute `AnyValue` supports exactly one scalar `stringValue`, `boolValue`,
  `intValue`, or `doubleValue`. Booleans become `true`/`false` label strings;
  numeric attributes become decimal strings. Arrays, kvlists, bytes, malformed
  scalars and duplicate raw attribute keys fail, even on unpromoted resources.
- Descriptions become escaped `# HELP` text; types become `# TYPE` lines.
  Strings must be UTF-8 encodable; lone surrogates fail.

Delta/unspecified sums, string enum temporality, histograms,
exponential histograms, summaries, nonempty exemplars, nonzero/unknown flags,
metric `metadata`, and **unknown fields at any level** fail explicitly. Flags
may be omitted or numeric zero; exemplars may be omitted or an empty array.
This strict schema intentionally accepts less than the full OTLP format.

## Naming rules

Both strategies normalize metric names by replacing runs of characters outside
ASCII letters/digits with one `_`, trimming edge underscores, and prefixing a
digit-leading result with `_`. Case is preserved. A name with no ASCII letter
or digit fails. This also normalizes existing repeated underscores and colons.

`UnderscoreEscapingWithSuffixes` applies the following exhaustive unit table:

| OTLP unit | Resulting gauge suffix | Counter unit suffix | Default value conversion |
| --- | --- | --- | --- |
| omitted / empty | none | none | none |
| `s` | `_seconds` | `_seconds` | none |
| `ms` | `_milliseconds` | `_milliseconds` | none |
| `By` | `_bytes` | `_bytes` | none |
| `1` | `_ratio` | none | none |

The gauge column also applies to nonmonotonic sums represented as gauges; this
is an explicit local policy. A unit token already at the end is not repeated.
For counters, an existing terminal `_total` is temporarily removed, then the
unit token is handled, then `_total` is appended once. For example,
`elapsed_seconds_total` with `s` stays `elapsed_seconds_total`, and
`elapsed_total` with `s` becomes `elapsed_seconds_total`.
`UnderscoreEscapingWithoutSuffixes` performs only metric-name escaping. Both
strategies reject all unlisted units, including `%`, `{requests}` and `m/s`.
By default milliseconds remain milliseconds. Bytes are never rescaled.

### Opt-in exact milliseconds to seconds

```sh
otelwrench prom-names convert examples/prom-names/milliseconds.json \
  --convert-unit ms:s --output seconds.prom
```

`--convert-unit ms:s` is the only supported conversion. It scales **only samples
whose OTLP unit is exactly `ms`** by the exact decimal factor `0.001`, records
target unit `s`, and applies the selected naming strategy using seconds. Other
supported units and all attribute values remain unscaled. Unsupported input
units still fail; requests such as `--convert-unit By:s` fail too.

For example, input metric `latency`, unit `ms`, with `asInt: "1250"` produces
`latency_seconds 1.250` under the default suffix strategy. An `asDouble` JSON
literal `0.100000000000000000001` produces
`latency_seconds 0.000100000000000000000001`. The fixture adds distinct `kind`
labels so those two points pass the collision detector. Its int64-max cumulative
counter becomes `elapsed_seconds_total 9223372036854775.807`. The independent
complete golden is `examples/prom-names/seconds.prom` (six samples).

Conversion shifts the decimal exponent without binary floating-point arithmetic
or Decimal context rounding. After normal escaping, a terminal `milliseconds`
unit token is replaced by `seconds`, including before a counter's `_total`:
`elapsed_milliseconds_total` becomes `elapsed_seconds_total`.
The no-suffix strategy also replaces that existing token, but adds no new unit
suffix: a bare `latency` remains `latency` with scaled values and unit `s` in its
mapping. All original descriptors are retained for collision checks; conversion
never merges differently named or differently declared metrics.

The library equivalent is `convert(document, convert_unit="ms:s")`; omit the
argument (or pass `None`) for the default unscaled behavior. Use `load_json` to
retain decimal literals from a JSON file; a caller that already decoded numbers
to Python floats has already lost the original decimal text.

To convert selected millisecond metrics in a mixed snapshot:

```sh
otelwrench prom-names convert examples/prom-names/milliseconds.json \
  --convert-unit ms:s --convert-metric latency --output selected.prom
```

This emits `latency_seconds{kind="integer"} 1.250` and
`latency_seconds{kind="decimal"} 0.000100000000000000000001`, while leaving
`elapsed_milliseconds_total 9223372036854775807` unchanged. The complete six-sample
golden is `examples/prom-names/selected-seconds.prom`. Repeat `--convert-metric EXACT_NAME`
to select more names, for example `--convert-metric elapsed_milliseconds_total`.

Selection matches the **raw OTLP name exactly**, before escaping or suffixes:
case, punctuation, Unicode and whitespace are literal; there is no glob, regex,
prefix or normalized-name matching. Every occurrence across resources/scopes is
selected, including descriptors with no data points. Duplicate selections are
coalesced. Every selected name must exist and every selected descriptor must have
unit exactly `ms`; an unmatched name, a selected `s`/`By`/unitless metric, or a
selection without `--convert-unit ms:s` fails with exit 2 before writing files or
a bundle. Dry-run applies the same rules. All metrics, including unselected ones,
still undergo the full schema, value and collision checks.

The library equivalent adds `convert_metrics=["latency"]` to `convert` or
`build_bundle`. Lists, tuples, sets and frozensets of nonempty UTF-8 names are
accepted; an explicit empty collection fails. Omit the argument (or pass `None`)
to keep converting all `ms` metrics with `ms:s`. An explicit selection adds the
sorted unique `unit_conversion_metrics` list to the mapping; unselected points
retain their units/values and have no conversion steps or `original_value` field.
Both naming strategies retain their existing suffix policies.

Input int64/binary64 overflow still fails, even if scaling would bring an invalid
input into range. A valid input that would underflow binary64 to zero after
conversion also fails. Existing nonfinite opt-in remains required; opted-in
`NaN`/infinities stay nonfinite. A conflicting target such as `latency`/`ms`
alongside `latency_seconds`/`s` fails with a collision report, even with different
labels. All these validation failures exit 2 before writing either artifact.

Mappings record `unit_conversion`, each metric's `original_unit`/`target_unit`,
the exact conversion factor in `normalizations`, and each converted point's
`original_value` alongside its emitted `value`. `value_kind` describes the input
field; an `asInt` input can emit fractional seconds. The `losses` list explains
the changed unit representation and identifies converted finite decimals that
cannot be represented exactly by binary64 backends. Decimal exposition/mapping
remain exact; those diagnostics do not assert exact downstream storage.

For labels, each non-ASCII-alphanumeric/non-underscore character becomes `_`;
leading and repeated underscores are preserved. Digit-leading label names get
`key_`. `host.name` becomes `host_name`; `2 cents` becomes `key_2_cents`.
Any resulting name beginning `__` fails as reserved. Label values escape
backslashes, quotes and newlines; values remain strings. Label and metric
escaping intentionally have different underscore rules.

## Mapping, losses and collisions

Mapping schema version 1 records input JSON paths, original name/type/unit/
description, sum semantics, target family/type, parser family/sample names,
every applied naming step, and all emitted point/label values and their scalar
types. Mapping values are strings to preserve integer/decimal precision.
Families, samples and labels are sorted in exposition. JSON object keys and
label mapping entries are sorted; repeated identical input produces byte-for-byte
identical artifacts. Mapping retains input order/paths for metrics and losses,
so reordering input can change mapping while preserving the `.prom` text.

The `losses` list explicitly identifies:

- resource attributes outside the allowlist, and all scope attributes;
- scope identity/version, schema URLs, and upstream dropped-attribute counts;
- data point start/end timestamps, which are recorded but not emitted;
- nonmonotonic sum semantics, units excluded by the no-suffix strategy, empty
  descriptors, and empty label values (equivalent to label absence in Prometheus).
- opted-in unit representation changes and binary64 rounding risk for converted
  decimal values (the original values remain available in mapping).

Output is a single snapshot, not a time series history exporter. Two points
with the same final labels fail even when timestamps differ. Attributes from
different scopes/resources can converge unless resource promotion disambiguates
them. Prometheus uses binary64 numbers; it may round exact int64 or decimal
text downstream. The Python parser retains integer literals but parses decimal
literals as floats. This risk is stated in every mapping's `numeric_policy`.

Collisions are checked across the **whole document** without auto-renaming:

| Collision kind | Check |
| --- | --- |
| `metric_name` | Different original names produce the same output family |
| `conflicting_descriptor` | One raw name has different type, unit, help, or monotonicity |
| `label_name` | Different raw keys in a family normalize identically, even on distinct points; promoted/resource and point labels also cannot overwrite each other |
| `parser_family` | Different originals converge after counter family canonicalization |
| `final_series` | Same canonical sample name and label values, including empty-label/absent equivalence and numeric/string attribute convergence |

The real Python parser canonicalizes counters: it removes a terminal `_total`
from a family name and ensures sample names have `_total`. Thus no-suffix raw
`requests` parses as sample `requests_total`. Mapping distinguishes these names,
and checks include this canonical form conservatively. This may reject inputs
a backend with different naming conventions would accept.

To inspect a deliberate failure:

```sh
otelwrench prom-names convert examples/prom-names/collisions.json \
  --output collision.prom
# Expected exit code: 2; JSON collision report on stderr; no output writes.
```

Each collision has `kind`, `target`, `first` and `second` input paths. A semantic
error stops conversion at the first unsupported structure; its stderr JSON has
`status: error`. CLI argument errors use argparse's text diagnostics. Successful
conversion exits 0. Collisions, invalid input and I/O errors exit 2.

Validation/collision failures leave existing output files untouched. Success
replaces destination files; input and destinations must be distinct, including
symlink/hard-link aliases. Parent directories must already exist. Each artifact
with `--output` is replaced atomically; that two-file pair is **not transactional**. A disk or
permission failure between replacements may update only the mapping. An I/O
failure reports this caveat; check both files before using them. Use the bundle
interface below when consumers need one consistent result.

## Atomic result bundles and verification

```sh
otelwrench prom-names convert examples/prom-names/milliseconds.json \
  --convert-unit ms:s --bundle result.zip
otelwrench prom-names verify result.zip
otelwrench prom-names verify result.zip \
  --input examples/prom-names/milliseconds.json
```

`--bundle PATH` is an alternative to `--output`; it cannot be combined with
`--output` or `--mapping`. It writes a single standard, uncompressed ZIP archive
with exactly three members:

| Member | Contents |
| --- | --- |
| `exposition.prom` | The same Prometheus text as the separate-file interface |
| `mapping.json` | The same deterministic mapping JSON as the separate-file interface |
| `manifest.json` | Schema version 1, exact input SHA-256/byte size, effective conversion configuration, and SHA-256/byte size for each result member |

The example produces the six samples in `examples/prom-names/seconds.prom`, including
`latency_seconds{kind="integer"} 1.250`. The manifest records `convert_unit:
"ms:s"`, the selected strategy, sorted unique resource promotion keys, and the
nonfinite opt-in. Neither source paths nor timestamps are included. Identical
input bytes and effective configuration produce identical member **and archive**
bytes with this implementation. ZIP timestamps, member order and permissions
are fixed. Mapping schema remains version 1.

With `--convert-metric`, the manifest configuration also stores the sorted unique
`convert_metrics` list, and `verify --input` replays that selection. This optional
schema-v1 field is absent when no selection is supplied, preserving prior
no-selection artifact bytes and reproduction of existing bundles. An explicit
empty, null, duplicate or unsorted selection in a manifest is rejected.

The complete archive is staged in the destination directory, closed and fsynced
before one atomic replacement publishes it. Failed member writes, archive
finalization, fsync or replacement leave the previous destination intact, or no
new destination when none existed. Staged files are removed on handled failures.
Consumers should open the bundle once and read its members from that handle;
`verify` does so even if another process replaces the path during verification.
There is no two-file publication interval. This guarantee relies on local
filesystem atomic replacement semantics; directory durability after power loss
is not guaranteed. A process killed during staging may leave an unreferenced
temporary file, but cannot publish that partial archive.

`verify` checks the exact member set (rejecting duplicates), manifest schema and
configuration, ZIP integrity, and both result hashes/sizes. A changed exposition
or mapping fails even when its ZIP CRC has been recomputed. It exits 0 with JSON
on stdout on success, or 2 with an error report on stderr on failure; it never
writes files or extracts archive paths. Compressed/encrypted members are rejected.

With `--input`, verification additionally requires the original input's exact
bytes (even whitespace changes fail its hash), re-runs conversion using the
manifest configuration, and compares both result members byte for byte. Success
reports `reproduced: true`; without input it reports `reproduced: false` and
checks integrity only. Hashes are for accidental corruption/mixing, not
authentication: someone who rewrites artifacts and manifest can recompute them.
Reproduction uses the currently installed converter and prometheus-client parser; a future
implementation change may require keeping the original otelwrench version to reproduce
old mappings. Bundles do not contain the source input.

`--dry-run` also works with `--bundle`: it validates and prints the mapping
without writing an archive. All existing conversion and alias checks apply.

### Inspect a verified bundle

```sh
otelwrench prom-names convert examples/prom-names/milliseconds.json \
  --convert-unit ms:s --convert-metric latency --bundle selected.zip
otelwrench prom-names inspect selected.zip
otelwrench prom-names inspect selected.zip \
  --input examples/prom-names/milliseconds.json
```

`inspect BUNDLE` verifies the exact archive member set, manifest configuration,
ZIP integrity and both artifact SHA-256 hashes/byte sizes **before** decoding the
mapping. Verification cannot be disabled. It then prints deterministic JSON
with these fields:

| Field | Meaning |
| --- | --- |
| `schema_version`, `status`, `verified` | `1`, `"ok"`, `true` on success |
| `reproduced` | `true` only when `--input` passed exact input/configuration replay; `false` without input |
| `configuration` | Stored conversion configuration; `convert_metrics` contains the exact selected names when supplied, and is absent otherwise |
| `sample_count` | Total mapped samples, checked against descriptor point counts |
| `metric_count` | Descriptor occurrences, including repeated names and empty descriptors |
| `loss_count`, `losses` | Number of loss entries and their full stored details in mapping order; this is not a count of dropped samples |
| `metrics` | One summary per descriptor, in mapping order |

Each metric summary contains its input `path`, `original_name`, normalized
`target_family`, `target_type`, `parser_family`, `parser_sample`, `original_unit`,
`target_unit`, `sample_count` and `unit_converted` boolean. Parser names are shown
separately because a no-suffix counter family `requests` parses as sample
`requests_total`. Empty descriptors have zero samples and still appear.
Every configured `convert_metrics` name must occur as an exact `original_name`
in the mapping, and every matching descriptor must have original unit `ms`,
including repeated and empty descriptors. Inspection rejects an absent selected
name or a selected non-`ms` descriptor with exit 2, empty stdout and a JSON
mapping error on stderr, even without `--input` and with valid artifact hashes.
Names are compared literally without trimming, case folding or normalization.
For each selected descriptor, inspection also checks that `target_family`,
`parser_family` and `parser_sample` agree with the raw name, stored strategy,
target type and converted unit. For example, selected `elapsed.milliseconds_total`
must report `elapsed_seconds_total` and parser family `elapsed_seconds` under
either strategy. Stale millisecond names or unrelated parser names fail with
the same read-only JSON mapping error, including on repeated/empty descriptors.
Selected descriptors must also declare `original_type` as `gauge` or `sum`
and include `is_monotonic`: null for gauges, a JSON boolean for sums. A gauge
or nonmonotonic sum must target `gauge`; a monotonic sum must target `counter`.
Missing/unsupported types, invalid monotonicity (including numeric `0`/`1`),
and contradictory target types fail with the same JSON mapping error, even if
target/parser names were coherently rewritten. Selected descriptors must include
`aggregation_temporality`: JSON null for gauges, or the exact JSON integer `2`
for cumulative sums, whether monotonic or nonmonotonic. Missing values, booleans,
strings (including `"2"`), floating-point `2.0`, other numbers and containers fail
with a mapping error identifying the selected raw name, descriptor path and field.
For each exact selected raw name, all occurrences must also agree on
`original_type`, `is_monotonic` and `aggregation_temporality`, across resources
and scopes, including empty descriptors. A gauge and a nonmonotonic sum conflict
even when both have identical gauge targets and names. A conflict exits 2 with
empty stdout and a JSON mapping error naming the selected raw name and both
occurrence paths; inspection writes no files. Identical repetitions remain valid;
omitted sum `isMonotonic` and explicit false both map to `is_monotonic: false`.
Every explicitly selected occurrence must also contain `original_description` as
a UTF-8 string; the empty string is valid. Missing, null, boolean, numeric or
container values fail. Repeated selected descriptions must match literally,
including empty descriptors across resources/scopes: no trimming, case folding,
Unicode normalization or HELP escaping is applied to this comparison. Empty
versus nonempty text conflicts, as do canonically equivalent but unequal Unicode strings.
For example, `latency` with `original_description: ""` followed by another
`latency` with `"Contradictory help text"` fails even with identical valid types,
units and target/parser names. The JSON mapping error identifies the selected
raw name, `original_description` and both occurrence paths. Identical empty or
nonempty descriptions remain valid. Description checks leave unselected and
no-selection validation and valid summary bytes unchanged.
Each `target_family` must also belong to only one distinct explicitly selected
raw `original_name`. For example, selected `wait.a` and `wait-a` cannot both
claim `wait_a_seconds`, even with identical gauge semantics, descriptions and
distinct point labels. Ownership compares raw names literally, so `wait.a` and
` wait.a ` are also distinct owners when their configured names coincide.
Conflicts exit 2 with empty stdout and a JSON mapping error naming the target
family, both selected raw names and their first/current occurrence paths;
inspection writes no files. Repeated and empty descriptors across resources/scopes
participate under both strategies. Identical repeats and distinct nonconflicting
names remain valid. Each `parser_family` must likewise have only one distinct
selected raw-name owner, even when the target families differ. For example,
selected `wait.a` gauge and `wait-a_total` cumulative counter have distinct targets
`wait_a_seconds` and `wait_a_seconds_total`, but both claim parser family
`wait_a_seconds` and fail. Without suffixes, the same conflict occurs for targets
`wait_a` and `wait_a_total` sharing parser family `wait_a`. These conflicts report
`parser_family`, both raw names and first/current paths with the same exit/error
and read-only behavior, including repeated/empty and filter-hidden owners.
Ownership checks cover explicitly selected descriptors; legacy/no-selection and
unselected validation retain their existing behavior.
Points in explicitly selected descriptors must also have unique canonical
final-series identities: the validated `parser_sample` plus sorted mapped
`target_label`/`value` strings, with empty values omitted to match Prometheus
empty-label/absent equivalence. Label order, scalar provenance, sample values
and timestamps do not distinguish an identity. Duplicates within a descriptor
or across descriptors/resources/scopes fail before filtering, under either
strategy. The JSON mapping error names the selected raw metric, canonical
sample/label identity and first/current point paths, with exit 2, empty stdout
and no writes. Selected points must contain a nonempty UTF-8 `path` and a
`labels` array of objects with nonempty string `target_label` and string `value`;
repeated target labels fail rather than being silently collapsed. Distinct label
identities and identical empty descriptors remain valid and deterministic.
Within each explicitly selected `target_family`, every mapped `target_label`
must have one literal raw `key` owner across all points/descriptors/resources/scopes.
For example, `site.a` and `site-a` cannot both claim `site_a`, even when their
values differ and final-series identities remain distinct. Empty-valued labels
also retain ownership. Conflicts fail before filtering with exit 2, empty stdout
and a JSON mapping error naming the selected raw metric, target label/family,
both literal keys and first/current label and point paths; inspection writes no
files. Consumed label `key` and `path` fields must be nonempty UTF-8 strings.
Same-key repeats, distinct mapped labels and different owners in distinct families
remain valid, as do empty descriptors and exact replay. Legacy/no-selection and
unselected validation retain their existing behavior.
Each explicitly selected mapped label must also be feasible under the installed
label naming policy: its literal raw `key` must normalize exactly to its stored
`target_label`. For example, `site.a` must map to `site_a`, a leading digit gains
`key_`, and existing underscores are retained. Raw keys normalizing to reserved
`__` names fail, including `__site` and `..site`. These checks cover promoted
resource and point labels, including empty values and filter-hidden points under
both strategies. Failures report the selected raw metric, point/label paths,
literal key, stored target label and expected normalized label or reserved-name
reason, with exit 2, empty stdout and no writes.
Each explicitly conversion-selected point must contain nonempty UTF-8 strings
`original_value` and `value`. Inspection validates finite decimal original text
against the existing binary64 overflow/underflow policy and requires `value` to
match the exact text emitted by the existing `ms:s` conversion, including decimal
precision and trailing zeros. For example, `original_value: "1000"` requires
`value: "1.000"`; a hash-valid coherent mapping/exposition rewrite to `"7.000"`
fails even when `--metric` hides that point. Numerically equivalent alternate
text such as `"1.0000"` also fails. `NaN`, `+Inf` and `-Inf` require the manifest's
nonfinite opt-in and remain unchanged by scaling. Unusable strings and converted
underflow fail. Errors exit 2 with empty stdout and contextual JSON identifying
the selected raw metric, point path, stored values and expected exact converted
text (or the invalid-field/policy reason); inspection writes no files. All
repeated points/descriptors/resources/scopes are checked under both strategies;
empty descriptors remain valid. Legacy/no-selection and unselected point checks
and valid summary bytes are unchanged.
These checks consume mapped labels and sample values; they do not reconstruct
input attributes, prove original value-kind/counter-sign claims or independently
validate all exposition semantics.
Every selected occurrence is checked before inspection filtering, so `--metric`
cannot hide an invalid selected descriptor. These are internal consistency checks, not
proof of the original input's semantics. The naming check uses the currently
installed naming rules; no-selection bundles retain their existing validation.

The example reports **6 samples, 5 descriptors and 2 loss entries**, with
`configuration.convert_metrics: ["latency"]`. Its `latency` descriptor has
`target_family: "latency_seconds"`, units `ms` to `s`, two samples and
`unit_converted: true`. `elapsed_milliseconds_total` retains its name and `ms`
unit, has one sample and `unit_converted: false`. With no selection field,
`configuration.convert_unit: "ms:s"` applies to all millisecond descriptors;
with no conversion, every `unit_converted` value is false. An empty snapshot
reports zero samples, descriptors and losses. The library interface is
`otelwrench.promnames.bundle.inspect_bundle(Path("selected.zip"))`.

With `--input INPUT`, inspection checks the exact original input hash/size,
re-runs conversion with the stored configuration (including exact metric
selection), and compares both result members byte for byte before emitting the
summary. It uses the same reproduction rules as `verify --input`. The library
equivalent adds `source=Path("examples/prom-names/milliseconds.json").read_bytes()`.
Verification, reproduction and summary all use the same already-read archive
generation, even if the bundle path is replaced during conversion.

Success exits 0 with JSON on stdout and no stderr. Missing/unreadable files,
malformed archives/manifests, changed artifacts, unsupported mapping schema or
status, unusable summary fields, configuration disagreement and inconsistent
point totals exit 2 with a JSON error on stderr and no summary on stdout.
With `--input`, even a whitespace-only input change or a hash-valid artifact
that cannot be reproduced fails the same way. Missing/unreadable input also fails.
Argument errors retain argparse's text diagnostics. Mapping JSON rejects
duplicate keys, nonfinite JSON numbers and invalid UTF-8 strings. Inspection
reads one archive generation even during concurrent replacement and never
extracts members or writes files.

`verified: true` means hash integrity. `reproduced: true` additionally confirms
exact input reproduction using the currently installed converter/parser;
neither is authentication. Without input, inspection validates the mapping
fields it consumes but does not recheck all point/exposition semantics.
Bundles do not contain or recover the original input.

### Focus inspection on exact raw metric names

```sh
otelwrench prom-names inspect selected.zip --metric latency
otelwrench prom-names inspect selected.zip --metric latency \
  --input examples/prom-names/milliseconds.json
```

Repeat `--metric EXACT_NAME` to include more raw names. This filters only the
`metrics` summary array; it is independent of `--convert-metric` used when
creating the bundle. Filtering supports every input unit and both naming
strategies, including bundles with no conversion selection. It matches
`original_name` literally: no case folding, trimming, Unicode normalization,
escaping, globbing, regex, prefix or target-family matching. Duplicate requested
names coalesce. All matching descriptor occurrences across resources/scopes
remain in mapping order, including repeated names and zero-sample descriptors.
Every requested name must match; one unmatched name fails even if others match.

Filtered JSON adds these fields; unfiltered JSON remains unchanged:

| Field | Meaning |
| --- | --- |
| `filter_metrics` | Sorted unique requested raw names; separate from stored conversion configuration |
| `matched_sample_count` | Sum of point counts for the returned descriptors |
| `matched_metric_count` | Number of returned descriptor occurrences, including empty descriptors |

`sample_count`, `metric_count`, `loss_count` and `losses` **always describe the
whole bundle**, even when `metrics` is filtered. Loss details remain complete in
their stored order; they are not attributed to just the matches. `configuration`
also remains the original whole-bundle conversion configuration. For the selected
fixture above, filtering `latency` returns one descriptor with two samples:

```json
{
  "filter_metrics": ["latency"],
  "matched_sample_count": 2,
  "matched_metric_count": 1,
  "sample_count": 6,
  "metric_count": 5,
  "loss_count": 2
}
```

This excerpt omits the other unchanged fields and the returned `metrics` array.
The entire archive is hash-verified and all existing inspection validation runs
on every descriptor, loss entry and whole-bundle total **before filtering**.
An invalid hidden descriptor or changed artifact still fails. With `--input`,
exact original-byte replay covers the entire archive, including unshown metrics;
`reproduced: true` describes that whole-archive replay. Without input it remains
`false`, and the existing integrity-only inspection limits apply. Verification,
replay, validation and filtering use the same already-read archive generation.

Unmatched names (including a filter on an empty snapshot) or invalid empty names
exit 2 with a JSON error on stderr, empty stdout and no writes. A missing option
argument retains argparse's text error. The library equivalent is
`inspect_bundle(Path("selected.zip"), metric_names=["latency"])`, optionally with
`source=original_bytes`. `metric_names` accepts a nonempty list, tuple, set or
frozenset of nonempty UTF-8 names; invalid types/strings or an empty collection
raise `ConversionError`. Omit it or pass `None` for the unchanged full summary.

## Tests

The suite lives in `tests/promnames/` (run `python -m pytest tests/promnames` from
a clone with `pip install -e ".[test]"`). It covers both strategy goldens, all
values/labels/types through the real parser, CLI subprocesses launched outside
the source directory, integer precision, punctuation, unit/counter suffixes,
deterministic artifacts, resource promotion, collision paths, unsupported formats,
dry-run, invalid destinations and alias protection. It also covers exact opt-in
unit conversion, metric selection, bundle reproduction and tampering, inspection
filters and every selected-descriptor consistency check described above. The
golden `.prom` files in `examples/prom-names/` are independently written
expectations, not generated by the converter during tests.

## Limits

In-memory, local, single-document conversion only. No network ingestion,
remote_write, query rewriting, temporal aggregation, counter reset inference,
UTF-8 metric naming, arbitrary unit translation, OpenMetrics export, or automatic
collision resolution. Parser success is not proof of compatibility with every
Prometheus server; no live backend is involved. No restricted upstream server
source is copied. Attribution and dependency licenses are in
[THIRD_PARTY.md](../THIRD_PARTY.md).
