# otelwrench sentry

Convert **already saved Sentry SDK envelopes or event JSON** into inspectable
OTLP/HTTP JSON request bodies (`logs.json`, `traces.json`) and an explicit
conversion loss manifest, offline. Use it when you are moving from Sentry to an
OpenTelemetry pipeline and want local fixtures, or want to see exactly which
fields of a saved error or transaction survive the translation. Python standard
library only: no network calls, DSN, credentials, server, Collector, upload or
script execution.

The [Collector Sentry exporter](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/sentryexporter)
exports OTel toward Sentry. This command translates saved SDK envelopes in the
reverse direction. [Sentry OTLP ingestion](https://develop.sentry.dev/sdk/telemetry/traces/otlp/)
already exists; this command addresses local inspection and fixtures. It makes no
market exclusivity or production performance claim.

## Quick start

```sh
pip install otelwrench
git clone https://github.com/tianhaogutum/otelwrench && cd otelwrench

# Validate and print the full manifest; creates no files
otelwrench sentry convert examples/sentry/mixed.envelope --dry-run

# Convert to a new output directory (logs.json, traces.json, manifest.json)
otelwrench sentry convert examples/sentry/error.envelope --output-dir out

# Print one converted document on stdout instead
otelwrench sentry convert examples/sentry/mixed.envelope --stdout-document logs
```

Requires Python 3.11+. Publishing an output directory (`--output-dir`) requires
**Linux** (libc `renameat2` with `RENAME_NOREPLACE`) or **macOS** (`renamex_np`
with `RENAME_EXCL`) and a filesystem that supports atomic no-replace renames. On
other platforms use `--dry-run` or `--stdout-document`.

All output directories in these examples must be new. Re-running the same convert
command with an existing directory is an intentional error; select a fresh name.
The output parent must already exist.

`otelwrench sentry --version` prints `otelwrench sentry 0.1.0`. Text diagnostics
on stderr start with `otelwrench sentry: `.

## Examples with synthetic inputs

```sh
# Standalone transaction JSON -> root plus child span; unsupported fields reported
otelwrench sentry convert examples/sentry/transaction.json --output-dir out-transaction

# UTF-8, pretty-printed payload with literal newlines; exact byte-length framing
otelwrench sentry convert examples/sentry/mixed.envelope --output-dir out-mixed
python -m tests.sentry.verify_output out-mixed --source examples/sentry/mixed.envelope --expected-items 2 --expected-logs 1 --expected-spans 2

# The same envelope, gzip-compressed; independent file and expansion limits
otelwrench sentry convert examples/sentry/mixed.envelope.gz --input-compression gzip --max-input-bytes 1024 --max-decompressed-bytes 2048 --output-dir out-gzip
python -m tests.sentry.verify_output out-gzip --source examples/sentry/mixed.envelope.gz --expected-items 2 --expected-logs 1 --expected-spans 2

# Pipe saved gzip bytes directly into conversion; no intermediate input file
cat examples/sentry/mixed.envelope.gz | otelwrench sentry convert - --input-compression gzip --max-input-bytes 1024 --max-decompressed-bytes 2048 --output-dir out-stdin
python -m tests.sentry.verify_output out-stdin --source examples/sentry/mixed.envelope.gz --expected-items 2 --expected-logs 1 --expected-spans 2

# Attachment rejected by default (exit 2, creates no output)
otelwrench sentry convert examples/sentry/with-attachment.envelope --output-dir out-rejected

# Explicit opt-in: convert 2 items and record 1 skipped attachment
otelwrench sentry convert examples/sentry/with-attachment.envelope --skip-unsupported --output-dir out-skipped
python -m tests.sentry.verify_output out-skipped --source examples/sentry/with-attachment.envelope --expected-items 3 --expected-logs 1 --expected-spans 2

# Validation plus full manifest on stdout; creates no files
otelwrench sentry convert examples/sentry/mixed.envelope --dry-run

# One complete OTLP document on stdout, after validation; no output directory
otelwrench sentry convert examples/sentry/mixed.envelope --stdout-document logs
cat examples/sentry/mixed.envelope.gz | otelwrench sentry convert - --input-compression gzip --stdout-document traces
```

`python -m tests.sentry.verify_output` (run from the repository root) is an
independent checker that imports no converter modules; see
[Validation and boundaries](#validation-and-boundaries).

`examples/sentry/generate.py` regenerates the two mixed envelope fixtures, the
trace consistency fixture and a gzip copy of `mixed.envelope` (fixed gzip
timestamp), using only invented text/IDs. Run it explicitly with
`python examples/sentry/generate.py` when changing fixtures. The converter never
executes this or any input-supplied script.

The error example produces this record (attributes omitted here):

```json
{
  "timeUnixNano": "1700000000250000000",
  "severityNumber": 17,
  "severityText": "error",
  "body": {"stringValue": "boom"},
  "traceId": "11111111111111111111111111111111",
  "spanId": "2222222222222222"
}
```

The mixed fixture produces 1 log record and 2 spans, with **9 loss entries** for
unsupported header/field subtrees. With its attachment explicitly skipped, there
are 3 input items, 2 converted, 1 skipped and **11 loss entries**. These counts
are checked by real CLI end-to-end tests and the independent schema/invariant
checker. The gzip mixed fixture produces the same `logs.json`, `traces.json`, item
framing metadata and losses as the plain fixture: **1 log, 2 spans, 9 loss
entries**. Only the manifest's input provenance differs. For example, setting
`--max-decompressed-bytes 1946` for that fixture fails (its envelope is 1947 bytes),
returns exit 2 and creates no output directory. Truncated gzip, corrupt member
trailers and malformed item lengths also fail before publication, even if an
earlier item is valid or `--skip-unsupported` is selected.

## Input contract

```
otelwrench sentry convert INPUT [--output-dir DIR] [--input-format auto|json|envelope]
    [--input-compression none|gzip] [--skip-unsupported] [--dry-run]
    [--fail-on-trace-conflicts] [--fail-on-loss]
    [--fail-on-loss-category NAME]... [--error-format text|json]
    [--stdout-document logs|traces|manifest]
    [--max-input-bytes N] [--max-decompressed-bytes N]
```

- `INPUT` is a local file path or bare `-` for binary stdin. Plain and gzip stdin
  use the same format, validation, byte budgets and output rules as files,
  including `--dry-run` and explicit skip opt-in. For a literal file named `-`,
  use `./-` or its absolute path. Stdin is read until EOF or the input budget plus
  one detection byte; overflow fails without waiting for EOF. Valid input needs
  EOF before conversion. No prompt is printed, and stdin is left open for callers
  of the Python CLI entry point. This accepts one envelope stream or JSON object,
  not a sequence of independently converted documents.
  If descriptor 0 is closed, Python's stdin is absent or has no binary `.buffer`,
  or the binary stream is closed or raises a read error, conversion returns exit 2
  with a binary stdin diagnostic on stderr, empty stdout and no final or staging
  output. Existing output is untouched. Text-only stdin is not re-encoded as a
  fallback. Invalid arguments still reject before stdin access; file conversion
  does not require stdin.
  Use blocking binary input. A read that would block (`None` or
  `BlockingIOError`) is rejected with exit 2 and an `input read would block`
  diagnostic, empty stdout and no final or staging output. Available bytes on a
  nonblocking pipe do not prove that its writer has finished, even if they form
  valid JSON or a complete gzip member. Actual EOF (`b''`) is required for success;
  input overflow still rejects immediately after the detection byte. The
  converter does not wait or retry on would-block reads or change descriptor flags.
  For Python callers supplying a custom binary `.buffer`, it must provide a
  callable `read` method. A missing, `None` or other non-callable `read` rejects
  before reading or conversion with exit 2 and a `binary stdin must provide a
  callable read method` diagnostic, empty stdout and no final or staging output.
  Caller streams and existing output remain untouched. This interface check
  does not mask programming errors inside a callable reader or the converter.
  Each `read(size)` must return `bytes` (including bytes subclasses). Only actual
  zero-byte data means EOF; subclass overrides of truthiness or length are not
  used for EOF or byte-budget accounting. Nonempty chunks are retained through
  actual EOF or the budget plus one detection byte, so an invalid suffix cannot
  disappear behind a valid prefix. Text and other return types, including empty
  strings, `bytearray` and `memoryview`, reject with exit 2 and a `binary input
  read must return bytes` diagnostic before conversion, with empty stdout and no
  final or staging output. Returned values are neither encoded/coerced nor echoed
  in the diagnostic; caller streams and existing output remain untouched.
  Custom readers must honor the requested `size`. A result with more actual bytes
  than requested rejects immediately with exit 2 and a `binary input read returned
  more bytes than requested` diagnostic, without another read, conversion or final
  or staging output. This also applies to bytes subclasses and each 64 KiB request
  within a larger budget. Caller streams remain open and existing output is
  untouched. The converter cannot undo allocations made inside a noncompliant
  reader before it returns.
- Compression defaults to `none`; gzip requires explicit `--input-compression gzip`.
  Neither filename suffix nor magic bytes opt in. Gzip is decoded before format
  detection or explicit format parsing, so saved event JSON is also accepted.
  Concatenated gzip members form one byte stream with a shared expansion budget;
  each member's CRC32/size trailer is checked. Standard gzip zero padding is
  accepted; nonzero trailing garbage is rejected. Other compression formats fail.
- Default format `auto`: a complete JSON object is standalone event JSON;
  otherwise parse as an envelope. Use `--input-format envelope` for a header-only
  envelope such as `{}`. Standalone transactions are accepted for offline use.
- Decoded input is UTF-8 only; valid JSON objects; duplicate keys, BOMs, unpaired
  surrogates, nonfinite numbers and malformed JSON fail, including in unknown fields.
- Envelope headers are single, unpadded JSON lines. Separators are LF. Explicit
  `length` must be a nonnegative integer **byte count**, not character count. A
  length-delimited payload can contain literal newlines and multibyte UTF-8.
  Without length, payload ends at the next LF or EOF. A final LF is optional.
  No extra blank lines/whitespace after the envelope. Header-only envelopes are
  supported in explicit envelope mode.
- Only `event` and `transaction` items convert. Event payload type may be absent
  (means `event`); transaction payload type must be `transaction`. Header/payload
  type conflicts fail. Other envelope item types fail by default.
  `--skip-unsupported` records each unsupported item without decoding its payload;
  framing is still validated, and malformed supported items still fail.
- File or stdin bytes are bounded by positive `--max-input-bytes N` (default 64 MiB).
  For gzip this caps compressed bytes, including headers/trailers. Separately,
  positive `--max-decompressed-bytes N` caps total gzip expansion (default 64 MiB);
  decoding reads at most that budget plus one detection byte, with bounded
  internal buffering. The expansion limit applies only in gzip mode.
  Both budgets are enforced before JSON/envelope parsing; a size exactly at the
  limit is accepted if otherwise valid. Both arguments must be integers in
  `1..sys.maxsize - 1` (`1..9223372036854775806` on 64-bit Python), reserving one
  representable detection byte. Unsupported arguments return exit 2 with the
  offending flag and supported byte-budget range, empty stdout and no output or
  staging files, before opening input, accessing stdin or decoding gzip. Both
  arguments are validated even when the expansion limit is inactive in plain mode.
  File/stdin and gzip expansion reads request at most 64 KiB at a time, so a
  supported large budget does not allocate a correspondingly large read buffer.
  Input chunks are joined in memory; input/decoded bytes and parsed/output objects
  coexist, so budgets are not total memory ceilings. This is not a streaming
  converter. Deep JSON beyond the Python decoder's recursion limit is rejected.

For example, this unsupported budget returns exit 2 without reading input:

```sh
otelwrench sentry convert - --dry-run --max-input-bytes 9223372036854775807
```

For identical bytes and options, file and stdin conversion produce byte-identical
`logs.json`, `traces.json` and `manifest.json`; provenance records the bytes rather
than their transport. The piped mixed example above has **2 input items, 1 log,
2 spans and 9 losses**. Input/expansion overflow, empty input, corrupt gzip and
malformed later items return exit 2 with a stderr diagnostic and no final or
staging output. `--skip-unsupported` never suppresses those errors. Dry-run prints
only the manifest on stdout and creates no files.

Use **`--stdout-document logs|traces|manifest`** to print exactly one converted
document (`logs.json`, `traces.json` or `manifest.json`) plus a trailing newline,
with exit 0 and no success summary. No output directory is required or created;
the directory writer and staging are never used. JSON content and formatting
match the corresponding directory file (UTF-8, two-space indentation). An unused
signal still emits its complete empty request, e.g. `{"resourceSpans": []}`.
The selected document is serialized only after **all** input and policies pass:
errors in another signal, later envelope items or gzip trailers still return
exit 2 with empty stdout and the selected `--error-format` diagnostic on stderr.
Trace-conflict, any-loss and selective-loss policies apply to the entire input,
regardless of the selected document. This mode buffers conversion and serialization
in memory; it does not stream partial results. A stdout write failure can leave
partial bytes at the receiving pipe/file; stdout has no atomic publication guarantee.

`--stdout-document` cannot be combined with `--output-dir` or `--dry-run`.
These combinations, an unknown selection or a missing selection are argparse
text usage errors (exit 2), before file/stdin access, even with `--error-format json`.
Without this flag, directory mode still requires `--output-dir`; `--dry-run` still
prints the manifest and permits an ignored `--output-dir` for compatibility.

The library function `otelwrench.sentry.mapping.convert_bytes` accepts
`input_compression="gzip"`, `max_input_bytes=N` and `max_decompressed_bytes=N`
with the same defaults and limits. Its keyword-only `fail_on_trace_conflicts=False`
keeps diagnostics by default; set it to `True` for the same strict policy as the
CLI, described below. The separate keyword-only `fail_on_loss=False` opts into
rejecting enumerated conversion losses when `True`, as described under output and
losses below. Use `fail_on_loss_categories=()` to select particular documented loss
categories. Input must be `bytes` (including subclasses); other objects reject with
`ConversionError`, without coercion. Both byte budgets validate before inspecting
the input. The input budget uses its actual byte extent before gzip decoding or
JSON/envelope parsing, so overridden subclass length cannot bypass it. Accepted
subclasses are read as ordinary byte content, ignoring overridden truthiness,
length, conversion and framing methods. Source/payload byte counts, hashes and
offsets match an ordinary `bytes` input, including at exact budgets. Empty input
retains the ordinary JSON/envelope or missing-gzip-header diagnostic.

Framing follows the public [Sentry envelope specification](https://develop.sentry.dev/sdk/foundations/envelopes/).
Outputs follow [OTLP JSON encoding](https://github.com/open-telemetry/opentelemetry-proto/blob/main/docs/specification.md#json-protobuf-encoding):
lowerCamelCase fields, integer enums, string nanoseconds and hex trace/span IDs.

## Explicit mapping

| Input | Output / policy |
| --- | --- |
| `release` | Resource `service.version` (string); no service name inferred |
| `environment` | Resource `deployment.environment.name` (string) |
| `event_id` | `sentry.event_id` attribute, original spelling retained; accepts nonzero 32-hex or canonical hyphenated UUID |
| Envelope `event_id` | Used if payload ID absent; if both exist, must match ignoring case/hyphens |
| Error `message` | Body and `sentry.message` attribute (string) |
| Error `logentry.message`, `.formatted` | Both retained as named `sentry.logentry.*` attributes; body uses formatted when present |
| Error `exception.values` | Ordered `sentry.exception.chain` array of key/value lists; type, value, frames and rendered stack text retained |
| Last exception | Standard `exception.type`, `exception.message`, `exception.stacktrace` when supplied; chain remains intact |
| Stack frames | `filename`, `abs_path`, `function`, `module`, nonnegative int64 `lineno`/`colno`, boolean `in_app`; source/vars and other fields reported as losses |
| Error body precedence | `message`, then logentry formatted/message, then last exception type/value; at least one is required |
| Error `level` | `debug=5`, `info=9`, `warning=13`, `error=17`, `fatal=21`; severityText is original; other values fail |
| Error `timestamp` | `timeUnixNano`; absent timestamp omitted with note, no current time invented |
| Error `contexts.trace` | Nonzero 32-hex trace ID plus 16-hex span ID; absent pair means uncorrelated log; partial/invalid pair fails |
| Transaction | Root span name from nonempty `transaction`; explicit trace/span IDs from `contexts.trace` and both timestamps required |
| Child span | Own trace/span IDs, both timestamps and nonempty description or op required; trace ID must match root |
| Parent ID | Only emitted when explicitly provided, valid and nonzero; missing parents are never inferred; external parent references allowed |
| Span `op` / `description` | `sentry.op` / `sentry.description` attributes; child description (else op) also supplies name |
| Span `status` | Original retained as `sentry.status`; `ok` -> OTLP 1, known Sentry error statuses -> 2, unknown status -> 0 plus semantic loss entry |
| Span kind | Always UNSPECIFIED integer `0`; no role inferred from op |

Body text does not interpolate logentry params; those are reported as lost.
Exception chains use SDK oldest-first ordering. Stack text renders filename,
line and function with `<unknown>`/`?` for absent fields; the selected original
frame fields are also retained structurally. Missing selected optional fields
are omitted; explicitly malformed/null selected fields fail. Unknown fields are
covered by the loss policy below.

Numeric timestamps use exact decimal seconds; strings must be RFC3339 with `Z`
or a known numeric UTC offset. ASCII digits only; no naive dates, leap seconds,
unknown `-00:00` timezone or numeric strings. Output must fit
`0..18446744073709551615` nanoseconds. Sub-nanosecond values fail rather than round;
extra trailing fractional zeros are accepted. End must be >= start. Zero duration
is valid. IDs normalize to lowercase hex except the preserved original event ID.
Within each transaction, duplicate span IDs, self-parenting and parent cycles
fail. No interval containment, deduplication or cross-item trace assembly is
performed; cross-item consistency is reported without changing records (below).
Each converted item has its own resource/scope group; transaction
provenance event ID is on its root span.

The OTLP instrumentation scope name is `sentry2otlp`, and the manifest records
`tool.name: "sentry2otlp"`. Both are part of the saved data format and are kept
unchanged so existing fixtures and consumers keep working.

Known error status names are `unknown_error`, `invalid_argument`,
`deadline_exceeded`, `not_found`, `already_exists`, `permission_denied`,
`resource_exhausted`, `failed_precondition`, `aborted`, `out_of_range`,
`unimplemented`, `internal_error`, `unavailable`, `data_loss`, `unauthenticated`,
`cancelled`. No status is invented when status is absent.

## Cross-item trace consistency

Every conversion includes `manifest.json.traceConsistency`, also available through
`--dry-run`. It checks the transaction roots and child spans supplied in this input,
including references to later items. A span's identity is the normalized pair
`(traceId, spanId)`; reusing a span ID in different traces is allowed.

`duplicates` lists identities occurring more than once. A group is `identical`
when **all mapped span fields and resource attributes** are equal, otherwise
`conflicting`. Comparison includes supplied parent, times, name, mapped attributes
and event ID provenance. IDs and timestamps use their normalized OTLP values.
This is equality of the mapped subset: discarded unknown fields, item headers,
notes, losses and the transaction's child list do not participate. All occurrences
remain in `traces.json`, including conflicting ones; no winner is selected.

`parents` contains one relationship per span occurrence with its explicit IDs,
item index, payload JSON Pointer `path` and `outputPointer` into `traces.json`.
Duplicate occurrences use the same location format. Parent statuses are:

| Status | Evidence in the supplied transaction spans |
| --- | --- |
| `not_supplied` | No parent ID was provided; this can be a root, and no ID is inferred |
| `resolved` | The explicit parent exists in the same trace with one mapped definition (identical copies allowed) |
| `ambiguous` | The same-trace parent identity has conflicting definitions |
| `missing` | The explicit parent ID does not occur in any supplied transaction span |
| `other_trace_only` | That span ID occurs only in other traces; it is never attached across traces |

`missing` and `other_trace_only` do not prove invalid ancestry: the actual parent
may be outside the saved input. `cycles` lists same-trace cycles of unambiguous
definitions, with span IDs in child-to-parent order rotated to the smallest ID.
Conflicting identities are excluded from cycle edges and targets. The report
does not test interval containment or reconstruct a trace. `uncorrelatedLogs`
lists logs with no supplied ID pair. Even correlated logs do not define parent
spans; missing context is never filled using nearby transactions or resource data.

For an explicit synthetic example:

```sh
otelwrench sentry convert examples/sentry/trace-consistency.envelope --output-dir out-consistency
python -m tests.sentry.verify_output out-consistency --source examples/sentry/trace-consistency.envelope --expected-items 8 --expected-logs 1 --expected-spans 9
```

Expected: **8 items, 1 log, 9 spans, 0 losses**, each transaction's converted data
equal to converting it independently. The report summary is:

```json
{
  "traceIds": 2,
  "spanOccurrences": 9,
  "spanIdentities": 7,
  "identicalDuplicateGroups": 1,
  "conflictingDuplicateGroups": 1,
  "parents": {
    "not_supplied": 5,
    "resolved": 1,
    "ambiguous": 1,
    "missing": 1,
    "other_trace_only": 1
  },
  "cycles": 0,
  "uncorrelatedLogs": 1
}
```

By default these diagnostics return exit 0 and do not count as conversion losses.
Invalid IDs, malformed supported items, and duplicate IDs/self-parenting/cycles
**within one transaction** retain their exit 2 behavior and publish no output,
even after earlier items produced consistency diagnostics. The same report is
produced for equivalent plain/gzip inputs; empty/all-skipped inputs have empty lists
and zero counts. This report covers only the supplied input, not live Sentry data.

Use **`--fail-on-trace-conflicts`** to reject conflicting duplicate identities or
confirmed cross-item cycles after full input validation, before creating output.
This CLI policy also applies to `--dry-run`: rejection returns exit 2, prints no
stdout or manifest, and writes only a stderr diagnostic with both counts. No final
or staging output is created, and an existing destination is left untouched.
File, binary stdin and explicit gzip input follow the same policy;
`--skip-unsupported` does not bypass it. Malformed supported items and gzip
integrity errors retain their validation diagnostics.

```sh
# Exit 2; no out-strict directory or staging files
otelwrench sentry convert examples/sentry/trace-consistency.envelope --fail-on-trace-conflicts --output-dir out-strict
# stderr: otelwrench sentry: trace conflicts: conflicting duplicate groups=1, confirmed cycles=0

# Inspect the full report using the default diagnostic-only policy
otelwrench sentry convert examples/sentry/trace-consistency.envelope --dry-run
```

Identical mapped duplicates, missing/external or other-trace-only parents,
span ID reuse in different traces, and uncorrelated logs remain accepted with the
flag. Conflicting definitions are still excluded from cycle detection, but the
conflicting identity itself triggers rejection. Empty/all-skipped inputs also
pass. Accepted conversions produce the same three documents as default mode;
the flag does not deduplicate, assemble traces or change the manifest schema.
Python callers use `convert_bytes(data, fail_on_trace_conflicts=True)`. The CLI
passes its flag to this same library boundary. Rejection raises `ConversionError`
with `trace conflicts: conflicting duplicate groups=N, confirmed cycles=M` after
all input validation, including later items and gzip member trailers. Input type,
byte budgets, malformed framing/mapping and unsupported-item errors retain their
own diagnostics. Omitting the keyword or passing `False` preserves the default
documents, including conflict diagnostics. The policy also works with bytes
subclasses and explicit gzip input.

```python
from pathlib import Path
from otelwrench.sentry.mapping import convert_bytes
from otelwrench.sentry.parser import ConversionError

try:
    documents = convert_bytes(
        Path("examples/sentry/trace-consistency.envelope").read_bytes(),
        fail_on_trace_conflicts=True,
    )
except ConversionError as exc:
    print(exc)
# trace conflicts: conflicting duplicate groups=1, confirmed cycles=0
```

## Output, losses and failure guarantees

`logs.json` is an `ExportLogsServiceRequest` body; `traces.json` is an
`ExportTraceServiceRequest` body. An unused signal has an empty resource array.
These are ordinary JSON documents, not Collector fileexporter's JSON Lines stream
or generic Protobuf JSON with base64 IDs. No sending transport is implemented.

`manifest.json` schema version 1 contains:

- Input format, original file or stdin byte count and SHA-256; no local input filename is copied.
  Gzip adds `input.compression: "gzip"`, `input.decompressedBytes` and
  `input.decompressedSha256` for the exact decoded stream. Uncompressed manifests
  keep their existing fields. Item offsets, byte counts and hashes always refer
  to the decoded envelope/JSON bytes, never to compressed-file positions.
- `tool`: `{"name": "sentry2otlp", "version": "0.1.0"}` (the converter's original
  name, unchanged for compatibility).
- Every item's index/type, payload byte count, byte offset and SHA-256, status,
  log/span counts and output JSON Pointer. Skipped items have no output pointer.
- Per-item `losses` and mapping `notes`; `envelopeLosses` covers unused envelope
  headers such as SDK, DSN and sent_at (no clock correction).
- `traceConsistency` contains the cross-item diagnostic summary, duplicate groups,
  parent relationships, cycles and uncorrelated log locations described above.
- Loss categories `unsupported_field`, `unsupported_item`, `semantic_loss` and
  `unused_metadata`; only categories actually present appear in the counts.
  `unsupportedFieldCategories` counts unsupported field names across all levels.
- Item conservation: `inputItems = convertedItems + skippedItems`. Log/span totals
  match output files; `lossEntries` counts the enumerated loss entries.

Loss paths use escaped JSON Pointers relative to payload (`/user`, `/spans/0/data`),
item header (`/itemHeader/filename`) or envelope (`/envelope/sdk`). One loss for an
unknown field **covers its whole subtree**, not each leaf. Unknown values are
never copied wholesale into logs or the manifest. Paths/field names are retained.
This includes user/request/breadcrumbs/tags/data/measurements, source context,
SDK metadata and any other field outside the table. Unknown fields are accepted
with explicit loss entries; unsupported item types require explicit skip opt-in.

Use **`--fail-on-loss`** or library `convert_bytes(data, fail_on_loss=True)` to
reject any enumerated conversion loss. The default is `False`; omitting the
option or explicitly passing `False` preserves the same documents and counts.
After complete input validation, the policy checks `manifest.summary.lossEntries`
and raises `ConversionError` with `conversion losses: loss entries=N` if nonzero.
This counts all four loss categories across envelope headers, item headers and
payloads. A skipped unsupported item remains a loss even with `--skip-unsupported`;
an envelope event ID with no converted items counts as unused metadata. Notes
and trace consistency diagnostics do not count. Zero-loss input remains accepted,
including empty envelopes in explicit envelope mode. This checks the documented
mapping's loss entries; it does not guarantee lossless representation of Sentry.

The CLI returns exit 2 with this diagnostic on stderr, empty stdout and no final
or staging output, including `--dry-run`; existing destinations are untouched.
The policy applies equally to JSON/envelopes, plain/gzip, file/stdin and direct
library bytes/subclasses. Input type, byte budgets, framing, JSON/mapping,
unsupported-item rejection and gzip integrity errors keep their own diagnostics.
Trace-conflict rejection precedes any-loss rejection, which precedes selective
category rejection. Accepted conversions produce the same three documents as default mode.

```sh
# Exit 2; no out-loss directory or staging files
otelwrench sentry convert examples/sentry/mixed.envelope --fail-on-loss --output-dir out-loss
# stderr: otelwrench sentry: conversion losses: loss entries=9

# Zero-loss input succeeds and prints its manifest
otelwrench sentry convert examples/sentry/error.envelope --fail-on-loss --dry-run

# Inspect the detailed loss entries using default conversion
otelwrench sentry convert examples/sentry/mixed.envelope --dry-run
```

To tolerate some losses while rejecting others, repeat **`--fail-on-loss-category NAME`**
or pass keyword-only `convert_bytes(data, fail_on_loss_categories=("semantic_loss",))`.
The default selection is empty. Supported names are exactly `unsupported_field`,
`unsupported_item`, `semantic_loss` and `unused_metadata`, matching `lossCategories`.
The library accepts a tuple, list, set or frozenset of string names; bare strings,
bytes, mappings, iterators, `None`, non-string members and unknown names raise
`ConversionError` before input decoding. CLI invalid/missing names return exit 2
before opening the file or accessing stdin. Selection arguments validate even
when `fail_on_loss=True` is also supplied.

After complete validation, only existing counts in the selected categories
trigger rejection. Names are deduplicated and sorted alphabetically. The error
lists every selected category, including zero counts, then the selected total:
`conversion losses: semantic_loss=1, unsupported_field=2; selected loss entries=3`.
An unsupported field's whole subtree still counts once; repeated CLI names do
not increase the total. Selecting only absent categories accepts lossy input
with exactly the default documents. Notes and trace diagnostics remain outside
loss accounting. `--fail-on-loss` / `fail_on_loss=True` still rejects **every**
loss category, regardless of the narrower selection.

```sh
# Accept the mixed fixture's unsupported fields while rejecting semantic losses
otelwrench sentry convert examples/sentry/mixed.envelope --fail-on-loss-category semantic_loss --dry-run

# Reject selected field losses, also checking semantic losses; exit 2, empty stdout
otelwrench sentry convert examples/sentry/mixed.envelope --fail-on-loss-category unsupported_field --fail-on-loss-category semantic_loss --output-dir out-selected
# stderr: otelwrench sentry: conversion losses: semantic_loss=0, unsupported_field=9; selected loss entries=9
```

Selective rejection has the same complete-validation precedence and no-output
guarantees as any-loss rejection, including dry-run, gzip, file/stdin, direct
bytes/subclasses and untouched existing destinations. Unsupported items still
require `--skip-unsupported`; category selection never suppresses validation.
This policy uses the existing loss classification and does not change the
manifest schema or guarantee lossless Sentry representation.

### Machine-readable CLI errors

Use **`convert --error-format json`** for scripts. After argument validation,
the CLI's caught conversion, policy, I/O and recursion failures return exit 2
and write exactly one JSON object followed by a newline to stderr, with no text
prefix. Version 1 has exactly these fields:

```json
{"schemaVersion": 1, "kind": "conversion_error", "message": "conversion losses: loss entries=9"}
```

| Field | Contract |
| --- | --- |
| `schemaVersion` | Integer `1`, identifying this error format independently of the manifest schema |
| `kind` | `conversion_error` for caught `ConversionError`, `io_error` for caught `OSError`, or `recursion_error` for caught `RecursionError` |
| `message` | The existing exception's diagnostic string, without the `otelwrench sentry: ` text prefix |

Kinds follow the exception type at the CLI boundary, not diagnostic prose.
For example, stdin read errors and JSON nesting failures already wrapped as
`ConversionError` remain `conversion_error`, as do output-directory validation
errors. An unwrapped file read/write `OSError` is `io_error`. Policy rejections
all use `conversion_error`; this version does not add separate policy codes.
Messages remain human-readable and may vary with Python/OS versions; scripts
should branch on the version and kind. JSON escaping preserves quotes, newlines
and Unicode (including OS filename surrogates) in one line. No exception class,
traceback, input body or additional metadata is attached. Existing diagnostic
text can include paths or input field names; this option does not sanitize it.

```sh
# Exit 2; stderr is the object above; empty stdout, no output/staging directory
otelwrench sentry convert examples/sentry/mixed.envelope --fail-on-loss --error-format json --output-dir out-json-error

# Accepted conversion still prints the ordinary manifest on stdout
otelwrench sentry convert examples/sentry/error.envelope --error-format json --dry-run
```

The default **`--error-format text`** keeps `otelwrench sentry: MESSAGE` diagnostics.
Argparse usage errors, including invalid/missing option values and the required
`--output-dir` check, keep their text format even with `json`.
Successful stdout and all three documents are unchanged. Conversion rejection
still leaves stdout empty, never calls the writer, creates no final/staging
output and leaves existing destinations untouched, including with `--dry-run`.
Validation and trace/any-loss/selective-loss precedence are unchanged across
JSON/envelopes, file/stdin and plain/gzip. Write/rename failures keep the
staging cleanup guarantees below. This is a CLI diagnostic format; library
exceptions and manifest schemas are unchanged.

### Atomic publication

All supported items are validated before writing. The three files are fully
written and fsynced in a temporary sibling directory, then published together
with an atomic no-replace rename: `renameat2(RENAME_NOREPLACE)` on Linux,
`renamex_np(RENAME_EXCL)` on macOS. An existing file, directory or dangling
symlink at the destination is never replaced, including concurrent creation.
Write/rename errors remove staging and publish nothing. On other platforms, or
when the filesystem does not support the no-replace rename, `--output-dir` fails
with exit 2 and a diagnostic; use `--dry-run` or `--stdout-document` there.
Dry-run only prints the manifest; `--stdout-document` prints the selected document
without staging. Exit 0 means success; invalid input, unavailable atomic publication
or I/O/argument errors and opt-in trace conflict or loss rejection return 2 with
stderr diagnostics.

Atomic visibility is not a power-loss durability guarantee. SIGKILL/power failure
may leave a hidden `.NAME.staging-*` directory; no partial final directory is
published. Use a trusted local output parent. Conversion memory can exceed input
size because parsed objects and output documents coexist. Saved message, exception
and filename text can be sensitive: inspect local outputs before sharing.

## Validation and boundaries

The test suite in `tests/sentry/` exercises hand-written error/transaction golden
outputs (`tests/sentry/golden/`), binary stdin/file equivalence, exact stdin budgets
and overflow before pipe EOF, byte-budget range rejection before input access, tiny
plain/gzip inputs at the supported upper bound without large read requests, short
reads, bytes subclasses with falsy nonempty chunks and truthy empty EOF (including
overrides that raise), retained invalid suffixes before validation/publication,
direct library subclass input budgets, exact-budget JSON/envelope/gzip equivalence
and source/payload provenance without subclass conversion or framing hooks,
unavailable/closed binary stdin, missing/non-callable custom binary read methods,
read errors without closing caller streams and propagation of reader/converter
programming errors, argument rejection before stdin access and file conversion with
descriptor 0 closed, nonblocking pipes with no/partial/complete JSON, envelope or
gzip bytes while the writer stays open, would-block rejection for raw/buffered
streams and actual EOF, gzip/plain equivalence, separate size budgets, concatenated
gzip members, truncated streams and corrupt trailers, UTF-8/embedded LF framing,
decimal/RFC3339 precision, malformed framing/JSON/IDs, invalid transactions,
cross-item duplicate/parent/cycle diagnostics, opt-in library and CLI conflict/loss
rejection after complete validation and before publication or dry-run stdout,
selective category rejection and argument validation before input, exact loss
counts and trace-policy precedence, versioned JSON stderr kinds/messages with
unchanged text/usage errors and accepted output, stdout document selection with
directory-byte equivalence and complete-input/policy validation before emission,
empty signals and incompatible flags before input, preservation of independent
transactions and absent context, explicit skip accounting, dry-run, concurrent
publication, I/O failure cleanup and full CLI runs in separate processes.

The independent checker `tests/sentry/verify_output.py` (run it from the repository
root as `python -m tests.sentry.verify_output DIR [--source FILE] [--expected-items N]
[--expected-logs N] [--expected-spans N]`) imports no converter modules and checks
the generated OTLP **subset** schema, enum/ID/time invariants, manifest item/loss
counts, pointers, consistency relationships/counts and source payload hashes. It is
not a full upstream schema validator or evidence of successful live ingestion.

No Sentry API/DSN access, attachment decoding, profiling, replay, metrics,
compression other than gzip, uploads or production performance claims. This is a
bounded offline translator, not a lossless archive.

License and attribution: see [THIRD_PARTY.md](../THIRD_PARTY.md).
