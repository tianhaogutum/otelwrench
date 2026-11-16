"""Cross-item diagnostics preserve independently converted OTLP and supplied IDs."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from otelwrench.sentry.mapping import convert_bytes
from .test_parser import envelope
from .verify_output import verify_documents, verify_directory

ROOT = Path(__file__).resolve().parents[2]
TRACE = "a" * 32
OTHER = "b" * 32


def transaction(span, parent=None, trace=TRACE):
    context = {"trace_id": trace, "span_id": span * 16}
    if parent is not None:
        context["parent_span_id"] = parent * 16
    return {"type": "transaction", "transaction": "synthetic", "release": "demo@1",
            "start_timestamp": 1, "timestamp": 2, "contexts": {"trace": context}}


def source_for(*events):
    return b"{}\n" + b"".join(envelope(json.dumps(event).encode(), kind=event.get("type", "event"))[3:]
                               for event in events)


def report_for(*events):
    return convert_bytes(source_for(*events))["manifest.json"]["traceConsistency"]


class ConsistencyTests(unittest.TestCase):
    def test_multi_item_fixture_reports_duplicates_and_parent_states_without_changing_data(self):
        source = (ROOT / "examples/sentry/trace-consistency.envelope").read_bytes()
        result = convert_bytes(source)
        self.assertEqual(verify_documents(result, source), {"items": 8, "logs": 1, "spans": 9, "losses": 0})
        report = result["manifest.json"]["traceConsistency"]
        self.assertEqual(report["summary"], {
            "traceIds": 2, "spanOccurrences": 9, "spanIdentities": 7,
            "identicalDuplicateGroups": 1, "conflictingDuplicateGroups": 1,
            "parents": {"not_supplied": 5, "resolved": 1, "ambiguous": 1,
                        "missing": 1, "other_trace_only": 1},
            "cycles": 0, "uncorrelatedLogs": 1,
        })
        self.assertEqual([(d["kind"], d["traceId"], d["spanId"]) for d in report["duplicates"]],
                         [("identical", TRACE, "1" * 16), ("conflicting", TRACE, "2" * 16)])
        self.assertEqual([r["itemIndex"] for r in report["duplicates"][0]["occurrences"]], [0, 1])
        self.assertEqual([r["path"] for r in report["duplicates"][1]["occurrences"]],
                         ["/spans/0", "/contexts/trace"])
        self.assertEqual([(p["itemIndex"], p["status"]) for p in report["parents"]],
                         [(0, "not_supplied"), (0, "resolved"), (0, "not_supplied"),
                          (1, "not_supplied"), (2, "not_supplied"), (3, "ambiguous"),
                          (4, "missing"), (5, "other_trace_only"), (6, "not_supplied")])
        # Every transaction remains exactly what converting it independently emits.
        for entry, group in zip(result["manifest.json"]["items"], result["traces.json"]["resourceSpans"]):
            payload = source[entry["payloadOffset"]:entry["payloadOffset"] + entry["payloadBytes"]]
            self.assertEqual(group, convert_bytes(payload)["traces.json"]["resourceSpans"][0])
        first = result["traces.json"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertNotIn("parentSpanId", first[2])
        log = result["logs.json"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        self.assertNotIn("traceId", log)
        self.assertEqual(report["uncorrelatedLogs"], [{
            "itemIndex": 7, "path": "/contexts/trace",
            "outputPointer": "logs.json#/resourceLogs/0/scopeLogs/0/logRecords/0"}])

    def test_equivalent_ids_times_and_object_key_order_are_identical(self):
        one = transaction("c")
        two = deepcopy(one)
        two["contexts"]["trace"] = {"span_id": "C" * 16, "trace_id": "A" * 32}
        two["start_timestamp"] = "1970-01-01T00:00:01Z"
        report = report_for(one, two)
        self.assertEqual([d["kind"] for d in report["duplicates"]], ["identical"])

    def test_each_mapped_definition_and_resource_difference_conflicts(self):
        one = transaction("1")
        for field, value in [("transaction", "changed"), ("release", "demo@2"),
                             ("timestamp", 3), ("event_id", "e" * 32)]:
            two = {**one, field: value}
            with self.subTest(field=field):
                self.assertEqual(report_for(one, two)["duplicates"][0]["kind"], "conflicting")
        self.assertEqual(report_for(one, transaction("1", parent="9"))["duplicates"][0]["kind"], "conflicting")

    def test_identity_is_trace_and_span_pair_not_span_id_alone(self):
        report = report_for(transaction("1"), transaction("1", trace=OTHER))
        self.assertEqual(report["duplicates"], [])
        self.assertEqual(report["summary"]["spanIdentities"], 2)

    def test_parent_can_be_supplied_by_later_item_and_identical_copies_are_unambiguous(self):
        report = report_for(transaction("2", "1"), transaction("1"), transaction("1"),
                            transaction("1", trace=OTHER))
        self.assertEqual(report["parents"][0]["status"], "resolved")
        self.assertEqual(report["parents"][0]["parentSpanId"], "1" * 16)

    def test_supplied_log_correlation_does_not_define_a_parent_span(self):
        log = {"message": "supplied context", "contexts": {"trace": {"trace_id": TRACE, "span_id": "1" * 16}}}
        report = report_for(transaction("2", "1"), log, {"message": "no context"})
        self.assertEqual(report["parents"][0]["status"], "missing")
        self.assertEqual(len(report["uncorrelatedLogs"]), 1)
        self.assertEqual(report["uncorrelatedLogs"][0]["itemIndex"], 2)

    def test_cross_item_cycles_are_diagnostics_from_unambiguous_same_trace_edges(self):
        report = report_for(transaction("2", "1"), transaction("1", "2"), transaction("1", "2"))
        self.assertEqual(report["cycles"], [{"traceId": TRACE, "spanIds": ["1" * 16, "2" * 16]}])
        self.assertEqual(report["summary"]["cycles"], 1)
        self.assertEqual([p["status"] for p in report["parents"]], ["resolved"] * 3)
        # Conflicting definitions never contribute a guessed edge to a cycle.
        report = report_for(transaction("1", "2"), transaction("2", "1"), transaction("2", "9"))
        self.assertEqual(report["cycles"], [])
        self.assertEqual(report["parents"][0]["status"], "ambiguous")

    def test_missing_or_other_trace_only_parents_never_create_cycles(self):
        report = report_for(transaction("1", "2"), transaction("2", "1", trace=OTHER))
        self.assertEqual([p["status"] for p in report["parents"]], ["other_trace_only"] * 2)
        self.assertEqual(report["cycles"], [])

    def test_empty_and_skipped_inputs_have_an_empty_report(self):
        for source, options in [(b"{}\n", {"input_format": "envelope"}),
                                (envelope(b"opaque", kind="attachment"), {"skip_unsupported": True})]:
            with self.subTest(options=options):
                report = convert_bytes(source, **options)["manifest.json"]["traceConsistency"]
                self.assertEqual(report["summary"]["spanOccurrences"], 0)
                self.assertEqual(report["parents"], [])
                self.assertEqual(report["duplicates"], [])
                self.assertEqual(report["cycles"], [])

    def test_cli_fixture_dry_run_and_gzip_report_match(self):
        source = ROOT / "examples/sentry/trace-consistency.envelope"
        expected = convert_bytes(source.read_bytes())["manifest.json"]["traceConsistency"]
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for compressed in (False, True):
                path = source
                extra = []
                if compressed:
                    path = base / "input.gz"
                    path.write_bytes(gzip.compress(source.read_bytes(), mtime=0))
                    extra = ["--input-compression", "gzip"]
                output = base / str(compressed)
                command = [sys.executable, "-m", "otelwrench", "sentry", "convert", str(path), *extra]
                result = subprocess.run([*command, "--output-dir", str(output)], cwd=base,
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(verify_directory(output, path.read_bytes())["spans"], 9)
                self.assertEqual(json.loads((output / "manifest.json").read_text())["traceConsistency"], expected)
                result = subprocess.run([*command, "--dry-run"], cwd=base, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["traceConsistency"], expected)
            self.assertEqual({p.name for p in base.iterdir()}, {"False", "True", "input.gz"})

    def test_diagnostics_do_not_hide_later_invalid_items_or_publish_partial_output(self):
        source = (ROOT / "examples/sentry/trace-consistency.envelope").read_bytes()
        bad = transaction("8")
        bad["timestamp"] = 0
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            path = base / "input.envelope"
            path.write_bytes(source + source_for(bad)[3:])
            result = subprocess.run([sys.executable, "-m", "otelwrench", "sentry", "convert", str(path),
                                     "--skip-unsupported", "--output-dir", str(base / "output")],
                                    cwd=base, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("end precedes start", result.stderr)
            self.assertEqual({p.name for p in base.iterdir()}, {path.name})

    def test_independent_verifier_rejects_invented_relationship_or_wrong_occurrence(self):
        source = (ROOT / "examples/sentry/trace-consistency.envelope").read_bytes()
        result = convert_bytes(source)
        for change in ("status", "pointer", "counts", "duplicates"):
            altered = deepcopy(result)
            report = altered["manifest.json"]["traceConsistency"]
            if change == "status":
                report["parents"][7]["status"] = "resolved"  # other trace is never a parent
            elif change == "pointer":
                report["parents"][0]["itemIndex"] = 1
            elif change == "counts":
                report["summary"]["spanOccurrences"] -= 1
            else:
                report["duplicates"][1]["kind"] = "identical"
            with self.subTest(change=change), self.assertRaises(AssertionError):
                verify_documents(altered, source)


if __name__ == "__main__":
    unittest.main()
