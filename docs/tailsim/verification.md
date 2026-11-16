# Verifying saved `otelwrench tailsim` reports

Saved replay and comparison reports carry SHA-256 fingerprints of their inputs (see
[Reports and byte accounting](../tailsim.md#reports-and-byte-accounting)). These commands
check a saved report against the corpus, configuration and schedule files you supply.
The four `verify-*` commands check **input provenance only**; the four
`verify-*-evidence` commands regenerate the report locally and compare every field.
All of them exit **0** on agreement, **1** for valid inputs that differ, and **2**
(with empty stdout) for invalid input. Commands below run from a repository clone.

## Check a saved static replay's provenance

```sh
otelwrench tailsim replay examples/tailsim/traces.json \
  --config examples/tailsim/policy.yaml > static-report.json
otelwrench tailsim verify-replay examples/tailsim/traces.json \
  --report static-report.json --config examples/tailsim/policy.yaml
```

`verify-replay` checks one saved static replay against a supplied local supported
OTLP corpus and policy-only config. Both source files are fully validated before
output. It recomputes the existing version-1 exact-byte corpus and normalized
effective-config identities; it does not evaluate saved decision evidence.

The saved UTF-8 JSON envelope must have exactly `schema_version` (integer 1),
`mode` (`complete_trace_offline`), `fingerprints`, `assumptions`, `byte_measure`,
`summary`, and `traces`. `assumptions` and `summary` must be objects,
`byte_measure` a string, and `traces` an array. Their contents are opaque.
`fingerprints` must contain exactly integer `version: 1`, `algorithm: sha256`,
`corpus_source_sha256`, and `effective_config_sha256`. Each digest must be
64 lowercase hexadecimal characters. Static comparisons and arrival reports
are unsupported by this command. Missing/unknown envelope or fingerprint fields,
duplicate JSON keys, nonfinite JSON constants, malformed JSON/UTF-8/digests,
unsupported modes/versions/algorithms, invalid or unsupported source inputs,
missing files and missing/unknown arguments return **2**, a stderr explanation,
and **empty stdout**. Nothing is repaired and no input file is changed.

Output contains exactly `schema_version: 1`, `mode: replay_provenance_verification`,
`fingerprint_version: 1`, `algorithm: sha256`, aggregate boolean `matches`, and
`checks`. `checks` contains exactly `corpus_source_sha256` and
`effective_config_sha256`, each with `reported_sha256`, `supplied_sha256` and
boolean `matches`. Exit **0** means both match; exit **1** means valid inputs
with at least one mismatch. Both print every check, empty stderr and no paths.
Changing a valid declared digest is a mismatch; changing corpus bytes (including
formatting) or effective policy parameters mismatches the corresponding identity.
Equivalent supported config defaults, sets and encodings retain config identity.

This verifies declared **input provenance, not evidence authenticity**.
`assumptions`, `byte_measure`, `summary` and `traces` are not authenticated or
compared. Matching fingerprints do not prove that decisions or byte measurements
are correct or untampered. Re-run replay for evidence, and retain the source
files and otelwrench version. No capture-completeness or cross-version evaluator
equivalence is established.

## Check saved static replay evidence by local regeneration

```sh
otelwrench tailsim verify-replay-evidence examples/tailsim/traces.json \
  --report static-report.json --config examples/tailsim/policy.yaml
```

`verify-replay-evidence` takes one saved schema-1 `complete_trace_offline` report,
a supported local OTLP corpus, and a supported policy-only configuration. Retain
all three files and the otelwrench version. It validates the same exact saved
static replay envelope, top-level container types, and version-1 sha256 block
specified for `verify-replay`, then regenerates `replay` from the fully validated
supplied corpus/config. Saved evidence is compared as JSON data; it is never
used as configuration, source spans, or instructions for regeneration.

Default output has exactly `schema_version: 1`, `mode: replay_evidence_verification`,
aggregate boolean `matches`, and `checks`. `checks` has exactly the seven saved
report fields: `schema_version`, `mode`, `fingerprints`, `assumptions`,
`byte_measure`, `summary`, and `traces`. Each has exactly a boolean `matches`.
Every field is checked, even after a mismatch. Values and paths are not copied
into the output. Exit **0** means every field agrees; exit **1** means a valid
envelope and valid supplied inputs with at least one differing field. Both
print the full check report and empty stderr.

Equality recursively ignores object-key order and JSON formatting/escaping,
but preserves array order, keys, and scalar types/values. Booleans differ from
numbers (`true` differs from `1`); integer and floating-point representations
also differ (`1` differs from `1.0`). Extra/missing nested evidence keys, altered
decisions/explanations/assumptions/summary/byte measure, and valid altered
digests are mismatches, not trusted assertions. Nested evidence contents need
not follow the producer's schema: any finite JSON value there is compared as
supplied, so a nested type change is a mismatch. Only the envelope, top-level
container types, and fingerprint metadata/digest syntax are schema-validated.

Malformed JSON/UTF-8, duplicate keys anywhere, nonfinite numbers (including
exponent overflow such as `1e999`), missing/unknown envelope or fingerprint
fields, wrong envelope/container types, unsupported modes/schema/fingerprint
versions/algorithms, malformed digests, invalid or unsupported corpus/config,
missing files and missing/unknown arguments return **2**, stderr explanation
and **empty stdout**. Validation completes before any output; files are not
changed. Static comparisons and arrival reports are unsupported here.

Agreement means **local regeneration agreement with this otelwrench version**.
It does not establish cryptographic authorship/authenticity, Collector runtime
equivalence, capture completeness, or cross-version evaluator equivalence.
`verify-replay`, `verify-compare`, `verify-arrivals`, and
`verify-compare-arrivals` retain their provenance-only, opaque-evidence contracts.

### Optional static replay mismatch paths

```sh
otelwrench tailsim verify-replay-evidence examples/tailsim/traces.json \
  --report static-report.json --config examples/tailsim/policy.yaml --diagnostic-limit 10
```

`verify-replay-evidence` accepts one explicit `--diagnostic-limit N`: an ASCII
decimal integer in **1–1000**, with leading zeros accepted. Missing values,
duplicates, abbreviations, unknown flags, signs, whitespace, non-ASCII digits,
fractions, exponents and out-of-range limits return **2** with empty stdout.
Omission preserves the exact default bytes and **0/1/2** exit behavior.

Opt-in output appends exactly `diagnostics` after all seven field checks:
`{"limit":1,"mismatches":[{"path":"/byte_measure","reason":"value_mismatch"}],"truncated":false}`.
The single total budget spans fingerprints, assumptions, byte measure, summary,
trace decisions and all nested policy explanations. All checks still run.
Agreement returns **0**, an empty mismatch array and `truncated: false`;
valid differences return **1**. Existing malformed report/source/config errors
still return **2** with empty stdout, before any diagnostics are emitted.
Saved schema/mode/envelope and fingerprint metadata errors remain validation
errors. Saved evidence never configures replay or reconstructs source spans.

Use the same traversal and six reasons as **Optional arrival mismatch paths**:
sort object-key unions by Unicode code point, walk depth first, and visit arrays
in numeric index order. Paths are JSON Pointers from the report root, escaping
`~` as `~0` and `/` as `~1`. Empty, Unicode and JSON-escaped surrogate member
names are supported; opt-in output uses ASCII JSON escapes. Emit one
`missing_key`/`extra_key` or `missing_item`/`extra_item` at an absent subtree,
without descending; reasons are relative to saved versus regenerated evidence.
Type changes emit `type_mismatch` (including bool/int/float distinctions), and
unequal same-type scalars emit `value_mismatch`. Keep the first `N` records;
one additional mismatch makes `truncated: true`, while exactly `N` does not.
Mapping insertion order and JSON escaping cannot change the result.

Diagnostics contain only paths/reasons, no evidence values, digests or input
file paths. Pointers expose member names. The bound covers record count, not
pointer lengths, input size or in-memory replay work. No authenticity claim is
added. Both arrival diagnostic commands, static comparison evidence verification
and all four opaque provenance commands retain their behavior.

## Check a saved static comparison's provenance

```sh
otelwrench tailsim compare examples/tailsim/traces.json \
  --baseline examples/tailsim/baseline.yaml --candidate examples/tailsim/policy.yaml > static-comparison.json
otelwrench tailsim verify-compare examples/tailsim/traces.json \
  --report static-comparison.json \
  --baseline examples/tailsim/baseline.yaml --candidate examples/tailsim/policy.yaml
```

`verify-compare` accepts one saved schema-1 `complete_trace_offline_comparison`
report, a supported local OTLP corpus and separately labelled baseline/candidate
policy-only configs. All supplied inputs are fully validated. It recomputes the
existing exact-byte corpus and normalized effective-config identities without
policy evaluation. No paths or saved evidence values enter the preimages. Equal
configs have equal labelled identities; reversing configs swaps their labels.
Equivalent supported defaults, sets, YAML/JSON encodings and Unicode escapes
preserve config identity. Corpus reformatting changes its exact-byte identity.

The saved UTF-8 JSON envelope must contain exactly `schema_version` (integer 1),
`mode` (`complete_trace_offline_comparison`), `fingerprints`, `assumptions`,
`byte_measure`, `baseline`, `candidate`, and `comparison`. `assumptions` and
`comparison` must be objects, `byte_measure` a string. Each embedded side must
satisfy the complete static replay envelope and fingerprint contract above.
The wrapper fingerprints must contain exactly integer `version: 1`,
`algorithm: sha256`, `corpus_source_sha256`, `baseline_effective_config_sha256`,
and `candidate_effective_config_sha256`. All seven digests across the wrapper
and embedded blocks must be 64 lowercase hexadecimal characters. Unknown/missing
envelope or fingerprint fields, duplicate JSON keys anywhere, nonfinite JSON
constants, malformed JSON/UTF-8/metadata/digests, unsupported schema/fingerprint
versions, algorithms or modes, invalid corpus or either config, missing files
and missing/unknown arguments return **2**, stderr explanation and **empty
stdout**. No input is repaired or changed; arrival reports are unsupported.

Output contains exactly `schema_version: 1`,
`mode: comparison_provenance_verification`, `fingerprint_version: 1`,
`algorithm: sha256`, aggregate boolean `matches`, and `checks`. `checks` has
exactly three sections: `fingerprints` (the three wrapper digest keys), `baseline`
and `candidate` (each static replay's two digest keys). Every check contains
`reported_sha256`, `supplied_sha256`, and boolean `matches`. All **seven** are
independently compared against supplied-input identities: an embedded digest
can mismatch even when the wrapper matches. Exit **0** means all match; exit
**1** means valid inputs with at least one mismatch. Both print all seven checks,
empty stderr and no paths. Changing corpus bytes mismatches all three corpus
checks; changing one effective config mismatches only its labelled wrapper and
embedded config checks. A valid modified saved digest mismatches only that check.

Only declared input provenance is checked. Wrapper/embedded `assumptions`,
`byte_measure`, `comparison`, `summary` and `traces` contents remain opaque and
unauthenticated. Matching digests cannot prove decisions, explanations or byte
measurements are correct or untampered. Re-run comparison for evidence and retain
source files plus the otelwrench version; no capture-completeness or cross-version
evaluator equivalence is established.

## Check saved static comparison evidence by local regeneration

```sh
otelwrench tailsim verify-compare-evidence examples/tailsim/traces.json \
  --report static-comparison.json \
  --baseline examples/tailsim/baseline.yaml --candidate examples/tailsim/policy.yaml
```

`verify-compare-evidence` accepts one saved schema-1
`complete_trace_offline_comparison` report, a supported local OTLP corpus and
separately labelled baseline/candidate policy-only configs. Retain these files
and the otelwrench version. It validates the exact wrapper, both complete embedded
static replay envelopes, top-level container types and all three version-1
sha256 blocks specified for `verify-compare`, then regenerates `compare` from
fully validated supplied inputs. Saved evidence never supplies regeneration inputs.

Output contains exactly `schema_version: 1`,
`mode: comparison_evidence_verification`, aggregate boolean `matches`, and
`checks`. `checks` contains exactly the eight top-level saved fields:
`schema_version`, `mode`, `fingerprints`, `assumptions`, `byte_measure`,
`baseline`, `candidate`, and `comparison`; each has exactly a boolean `matches`.
Every field is checked, including the entire nested replay reports and comparison
evidence. Values and paths are not copied into output. Exit **0** means all
fields agree; exit **1** means valid envelopes and inputs with differing fields.
Both print all checks and empty stderr. A changed wrapper digest mismatches
`fingerprints`; a changed embedded digest or evidence mismatches its labelled
side; comparison evidence edits mismatch `comparison` independently.

Equality uses the same recursive type-aware JSON contract as
`verify-replay-evidence`: object-key order and formatting/escaping normalize;
array order, keys, scalar types and values remain significant (`true` differs
from `1`, and `1` from `1.0`). Nested finite evidence is compared as data rather
than schema-validated or trusted. Equal, equivalent and reversed configurations
retain the producer's labelled comparison semantics.

Malformed wrapper or either embedded envelope/metadata/digest, duplicate keys
or nonfinite numbers anywhere (including exponent overflow), malformed JSON/UTF-8,
unsupported schemas/modes/fingerprint versions/algorithms, invalid or unsupported
corpus/either config, missing files or missing/unknown arguments return **2**,
stderr explanation and **empty stdout**. Validation completes before output;
files are unchanged. Arrival reports remain unsupported by this command.

Agreement establishes **local regeneration agreement with this otelwrench version**,
not authorship/authenticity, verified capture completeness, Collector runtime
or cross-version equivalence. Reports and inputs are held in memory. The four
provenance-only commands keep their opaque-evidence contracts.

## Check a saved arrival replay's provenance

```sh
otelwrench tailsim replay-arrivals examples/tailsim/arrival-order/traces.json \
  --arrivals examples/tailsim/arrival-order/arrivals.json \
  --config examples/tailsim/arrival-order/policy.yaml --decision-wait-ms 100 > arrival-report.json
otelwrench tailsim verify-arrivals examples/tailsim/arrival-order/traces.json \
  --report arrival-report.json --arrivals examples/tailsim/arrival-order/arrivals.json \
  --config examples/tailsim/arrival-order/policy.yaml --decision-wait-ms 100
```

`verify-arrivals` checks a saved **single-wait** report against supplied local
corpus/config/schedule files and a positive integer wait. It uses the same full
input validation, uint64 deadline checks and version-1 preimages as replay.
It prints one JSON object with `schema_version: 1`,
`mode: arrival_provenance_verification`, `fingerprint_version: 1`,
`algorithm: sha256`, aggregate boolean `matches`, and `checks`. The latter has
exactly four keys: `corpus_source_sha256`, `effective_config_sha256`,
`arrival_schedule_sha256`, `effective_arrival_model_sha256`. Each value contains
`reported_sha256`, `supplied_sha256` and boolean `matches`. Exit **0** means
all four match; exit **1** means valid inputs but at least one identity differs.
Every mismatch is listed; input paths do not enter the output.

The saved UTF-8 JSON must have exactly the replay envelope fields:
`schema_version` (integer 1), `mode` (`arrival_order_offline`), `fingerprints`,
`model`, `assumptions`, `summary`, `traces`. The `fingerprints`, `model`,
`assumptions` and `summary` fields must be objects; `traces` must be an array. The fingerprint block must contain exactly integer `version: 1`,
`algorithm: sha256` and the four digests above, each 64 lowercase hex characters.
Missing/unknown envelope or fingerprint fields, duplicate JSON keys, nonfinite
JSON constants, unsupported modes/versions/algorithms, malformed digests, invalid
source inputs/waits, missing files and deadline overflow return **2**, a stderr
explanation and **empty stdout**. A comparison report is not a single-wait report
and is rejected. No report is repaired and no input file is changed.

This verifies the declared **input identities only**. Contents inside `model`,
`assumptions`, `summary` and `traces` are opaque evidence and are not authenticated
or compared. A matching hash block cannot establish that a report's decisions
are correct or untampered. Re-run replay to obtain decision evidence from the
supplied inputs. Exact-byte corpus changes mismatch even when decisions stay
equal; equivalent config/schedule encodings preserve their normalized identities.
The otelwrench version and source inputs must still be retained with the report.

## Check saved arrival evidence by local regeneration

```sh
otelwrench tailsim verify-arrivals-evidence examples/tailsim/arrival-order/traces.json \
  --report arrival-report.json --arrivals examples/tailsim/arrival-order/arrivals.json \
  --config examples/tailsim/arrival-order/policy.yaml --decision-wait-ms 100
```

`verify-arrivals-evidence` accepts one saved schema-1 `arrival_order_offline`
report, a supported local OTLP reference corpus, policy-only config, version-1
arrival schedule and positive integer decision wait. Retain inputs and the
otelwrench version. It validates the exact seven-field single-wait envelope and
version-1 sha256 metadata described for `verify-arrivals`: integer
`schema_version: 1`, exact `mode`, object `fingerprints`, `model`, `assumptions`
and `summary`, and array `traces`; no missing or extra top-level fields.
Fingerprints require exactly integer `version: 1`, `algorithm: sha256` and the
four 64-character lowercase hexadecimal digests. It then regenerates
`replay-arrivals` from fully validated supplied inputs, including all uint64
deadline checks. Saved model/evidence never supplies replay inputs.

By default, output has exactly `schema_version: 1`, `mode: arrival_evidence_verification`,
aggregate boolean `matches`, and `checks`. The latter has exactly
`schema_version`, `mode`, `fingerprints`, `model`, `assumptions`, `summary`,
and `traces`; each contains exactly a boolean `matches`. Every field and its
complete nested evidence is compared. No values or paths are copied to default output.
Exit **0** means agreement; exit **1** means valid inputs with differing fields.
Both print all seven checks and empty stderr. A changed valid digest mismatches
`fingerprints`; model edits mismatch `model`, independently of the digests;
snapshot/full-trace decisions, span identities and explanation edits mismatch
`traces`. Supplied wait/schedule/config/corpus changes drive regeneration.

Equality is recursive and type-aware: object-key order, whitespace and Unicode
escaping normalize; array order, keys, scalar types and values remain significant.
`true` differs from `1`, and `1` from `1.0`. Equivalent config/schedule encodings
retain the producer's normalization. Nested finite evidence is data to compare,
not a trusted assertion or a schema-validated producer structure: even an edited
model name or a nonsensical finite nested decision yields a mismatch, not a new
replay mode or a validation failure.

Malformed envelope/metadata/digests, duplicate keys or nonfinite numbers anywhere
(including exponent overflow), malformed JSON/UTF-8, unsupported envelope schemas,
modes, fingerprint versions/algorithms, invalid or unsupported corpus/config/
schedule/wait, uint64 deadline overflow, missing files or missing/unknown arguments
return **2**, a stderr explanation and **empty stdout**. Validation finishes before
output. Files are unchanged. Paired arrival and static reports are unsupported
by this command; empty reference corpora with empty schedules are valid.

Agreement means **local regeneration agreement with this otelwrench version**.
It does not establish authorship/authenticity, capture completeness, Collector
runtime or cross-version equivalence. Inputs and reports are held in memory.
The four provenance-only commands keep their opaque-evidence contracts; static
evidence verifiers retain their existing scopes.

### Optional single-wait arrival mismatch paths

```sh
otelwrench tailsim verify-arrivals-evidence examples/tailsim/arrival-order/traces.json \
  --report arrival-report.json --arrivals examples/tailsim/arrival-order/arrivals.json \
  --config examples/tailsim/arrival-order/policy.yaml --decision-wait-ms 100 --diagnostic-limit 10
```

`verify-arrivals-evidence` also accepts the explicit `--diagnostic-limit N`
contract detailed under **Optional arrival mismatch paths** below: one ASCII
decimal integer in **1–1000**, leading zeros accepted, no abbreviations or
repeated flags. Omission preserves the exact default output bytes and exits.
Opt-in output appends `diagnostics` after the seven complete field checks, with
exactly `limit`, `mismatches` (records containing only `path` and `reason`) and
boolean `truncated`. For example, a changed saved wait yields
`{"limit":1,"mismatches":[{"path":"/model/decision_wait_ms","reason":"value_mismatch"}],"truncated":false}`.
The limit is global across all seven fields, not per trace or field. Walk the
whole report in sorted-key, depth-first order and arrays in numeric index order;
use JSON Pointer escaping, strict scalar types and the same six reasons and
exact one-record lookahead truncation as paired diagnostics. Empty, Unicode and
JSON-escaped surrogate member names are supported. Saved schema/mode/envelope
or fingerprint metadata errors remain validation errors, not diagnostic paths.
All seven checks run regardless of the limit. Saved model/summary/snapshot/
explanation/span-array evidence is only compared with local regeneration;
it cannot select the wait or configure replay. Diagnostics never echo evidence
values or input file paths; member names are visible in pointers. Existing
invalid report/input/wait/deadline errors and invalid diagnostic options return
**2** with empty stdout. Valid agreement/difference still returns **0**/**1**.
Paired diagnostics and all four opaque provenance-only commands keep their
existing behavior. This bounds emitted record count, not pointer byte lengths,
input size or replay memory/work; no authenticity claim is added.

## Check a saved arrival comparison's provenance

```sh
otelwrench tailsim compare-arrivals examples/tailsim/arrival-order/traces.json \
  --arrivals examples/tailsim/arrival-order/arrivals.json \
  --config examples/tailsim/arrival-order/policy.yaml \
  --baseline-wait-ms 100 --candidate-wait-ms 200 > arrival-comparison.json
otelwrench tailsim verify-compare-arrivals examples/tailsim/arrival-order/traces.json \
  --report arrival-comparison.json --arrivals examples/tailsim/arrival-order/arrivals.json \
  --config examples/tailsim/arrival-order/policy.yaml \
  --baseline-wait-ms 100 --candidate-wait-ms 200
```

`verify-compare-arrivals` accepts one saved schema-1 paired-wait report and the
same shared local input files and two positive integer waits as
`compare-arrivals`. It validates both supplied waits and every uint64 deadline
through the existing comparison path. It does not read input values from saved
evidence. Reversing waits checks the reversed baseline/candidate model identities;
equal waits give equal model identities. Version-1 preimages are unchanged.

The saved UTF-8 JSON envelope must contain exactly `schema_version` (integer 1),
`mode` (`arrival_order_offline_comparison`), `fingerprints`, `assumptions`,
`baseline`, `candidate`, `comparison`. `assumptions` and `comparison` must be
objects, with opaque contents. Both `baseline` and `candidate` must satisfy the
complete single-wait envelope and fingerprint validation contract above. The
wrapper fingerprint block must contain exactly integer `version: 1`,
`algorithm: sha256`, `corpus_source_sha256`, `effective_config_sha256`,
`arrival_schedule_sha256`, `baseline_effective_arrival_model_sha256` and
`candidate_effective_arrival_model_sha256`. Every digest in all three blocks
must be 64 lowercase hexadecimal characters. Unknown/missing envelope or
fingerprint fields, malformed JSON/UTF-8/digests, duplicate keys, nonfinite JSON
constants, unsupported schemas/modes/fingerprint versions/algorithms, invalid
supplied files/waits and overflow on either side return **2**, with a stderr
explanation and **empty stdout**. Nothing is repaired or ignored.

Output is one JSON object containing exactly `schema_version: 1`,
`mode: arrival_comparison_provenance_verification`, `fingerprint_version: 1`,
`algorithm: sha256`, aggregate boolean `matches`, and `checks`. `checks` has
exactly three sections: `fingerprints` (the five wrapper digest keys), `baseline`
and `candidate` (the four single-wait digest keys each). Every digest check
contains `reported_sha256`, `supplied_sha256` and boolean `matches`. All **13**
digests are independently compared to supplied-input identities, including
embedded shared/model identities; a matching wrapper never excuses a differing
embedded digest. Exit **0** means all match; exit **1** means valid inputs with
one or more mismatches, including an inconsistent wrapper/embedded digest.
Both print all checks with empty stderr and no paths. Input files are unchanged.

As with single-wait verification, only declared input identities are checked.
Comparison evidence and embedded model/assumptions/summary/traces contents are
opaque and unauthenticated. Matching digests cannot prove the decisions, state
changes or span deltas are correct or untampered. Re-run comparison for evidence.

## Check saved arrival comparison evidence by local regeneration

```sh
otelwrench tailsim verify-compare-arrivals-evidence examples/tailsim/arrival-order/traces.json \
  --report arrival-comparison.json --arrivals examples/tailsim/arrival-order/arrivals.json \
  --config examples/tailsim/arrival-order/policy.yaml \
  --baseline-wait-ms 100 --candidate-wait-ms 200
```

`verify-compare-arrivals-evidence` accepts one saved schema-1
`arrival_order_offline_comparison` report, a supported local OTLP reference
corpus, shared policy-only config, version-1 arrival schedule, and separately
labelled positive integer baseline/candidate waits. Retain inputs and the
otelwrench version. It validates the exact seven-field wrapper and both complete
seven-field arrival envelopes described for `verify-compare-arrivals`, including
all 13 digests and exact integer version-1/sha256 metadata. No missing or extra
envelope/fingerprint fields are allowed. Wrapper `assumptions`/`comparison` and
embedded `model`/`assumptions`/`summary` must be objects; embedded `traces` must
be arrays. It regenerates `compare-arrivals` using only fully validated supplied
inputs, including both waits and every uint64 deadline. Saved evidence never
supplies model parameters, decisions, identities or comparison inputs.

Default output has exactly `schema_version: 1`,
`mode: arrival_comparison_evidence_verification`, aggregate boolean `matches`,
and `checks`. The latter has exactly `schema_version`, `mode`, `fingerprints`,
`assumptions`, `baseline`, `candidate`, `comparison`; each contains exactly a
boolean `matches`. Both full embedded reports and every comparison field,
including changed-decision/state snapshots and unresolved pairs, are compared.
Default output copies no saved values or paths. Exit **0** means agreement;
exit **1** means valid inputs with differences. Both print all seven checks
with empty stderr. A valid altered wrapper digest mismatches `fingerprints`;
any embedded digest/model/evidence edit mismatches its labelled side. Shared
identities are checked independently, never excused by a matching wrapper.
Supplied corpus/config/schedule/wait changes drive regeneration. Equal and
reversed waits retain the producer's labelled semantics.

Recursive equality ignores object-key order, whitespace and Unicode escaping;
array order, keys, scalar types and values remain significant (`true` differs
from `1`, and `1` from `1.0`). Equivalent config/schedule encodings retain
producer normalization. Finite nested evidence is data to compare, not trusted
input or a schema-validated producer structure: nonsensical nested assertions
and edited model names yield mismatches, not new evaluation behavior.

Malformed wrapper/either embedded envelope or metadata, duplicate keys,
nonfinite numbers anywhere (including exponent overflow), malformed JSON/UTF-8,
unsupported envelope schema/mode/fingerprint version/algorithm, invalid or
unsupported corpus/config/schedule/either wait, either-side uint64 deadline
overflow, missing files or missing/unknown arguments return **2**, a stderr
explanation and **empty stdout**. Validation finishes before output; files are
unchanged. Single-wait and static reports are unsupported. Empty reference
corpora with empty schedules are valid.

Agreement means **local regeneration agreement with this otelwrench version**,
not authorship/authenticity, capture completeness, Collector runtime or
cross-version equivalence. Inputs and reports are held in memory. All four
provenance-only commands remain opaque; the other three evidence verifiers
retain their existing scopes.

### Optional arrival mismatch paths


```sh
otelwrench tailsim verify-compare-arrivals-evidence examples/tailsim/arrival-order/traces.json \
  --report arrival-comparison.json --arrivals examples/tailsim/arrival-order/arrivals.json \
  --config examples/tailsim/arrival-order/policy.yaml \
  --baseline-wait-ms 100 --candidate-wait-ms 200 --diagnostic-limit 10
```

`verify-arrivals-evidence` and `verify-compare-arrivals-evidence` accept
`--diagnostic-limit N`.
`N` must be an ASCII decimal integer in **1–1000** (leading zeros accepted;
no sign, fraction, exponent, boolean, or whitespace). The option may appear
once; missing values, duplicates, abbreviations, unknown diagnostic flags and
out-of-range limits return **2** with empty stdout. Omit it to retain the exact
existing output, checks, and exit codes.

With the option, append exactly one `diagnostics` object after `checks`:
`{"limit":N,"mismatches":[{"path":"/baseline/model/decision_wait_ms",
"reason":"value_mismatch"}],"truncated":false}`. The seven checks still cover
the entire report, regardless of the diagnostic limit. Exit **0** agreement has
an empty mismatch array and `truncated: false`; **1** denotes valid differences;
all existing malformed/unsupported inputs still return **2** with empty stdout.

Paths are JSON Pointers from the saved report root. Paired paths preserve `baseline` and
`candidate` labels (also within comparison snapshots). Escape each member name
by replacing `~` with `~0`, then `/` with `~1`; the empty member name produces
an empty path segment. Walk depth first, sorting the union of object keys by
Unicode code point at every level (including the root), and walking arrays by
ascending numeric index. Mapping insertion order and JSON escaping do not
change diagnostics. Opt-in JSON uses ASCII Unicode escapes, including escaped
lone surrogates in member names, to keep the output valid UTF-8. Reasons are
relative to the saved report versus local regeneration: `missing_key` / `extra_key` for a key absent from / present only
in saved data; `missing_item` / `extra_item` for trailing array indices. Emit
one record at that key/index and do not descend into a wholly absent subtree.
A type change emits `type_mismatch` at that node without descending (including
bool/int/float distinctions); unequal same-type scalars emit `value_mismatch`.
Arrays compare by position, not identity: reordering may yield multiple nested
records. Common indices are walked before trailing indices; there is no
additional array-length or parent-object record.

Keep only the first `N` records **across the whole report**. `truncated` is true
if and only if at least one additional record would exist, including when the
limit ends on a subtree boundary; exactly `N` differences is not truncated.
Traversal looks ahead at most one mismatch beyond the limit. This bounds record
count, not pointer byte lengths, input size, replay memory or work. Pointers
necessarily expose JSON member names; diagnostics never include evidence
values, digests, source-file names/paths, or regenerated values. Saved finite
evidence remains comparison data and cannot configure replay. No authenticity
or Collector equivalence claim is added.

