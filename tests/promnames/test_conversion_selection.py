# SPDX-License-Identifier: Apache-2.0
import copy
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from prometheus_client.parser import text_string_to_metric_families
from otelwrench.promnames import ConversionError, convert, load_json
from otelwrench.promnames.bundle import build_bundle, atomic_write_bundle, verify_bundle
from otelwrench.promnames.core import WITH, WITHOUT
from .helpers import CLI, ROOT, attr, document, gauge, sum_metric


class ConversionSelectionTests(unittest.TestCase):
    def test_mixed_fixture_exact_golden_mapping_and_parser(self):
        source = load_json((ROOT / "examples/prom-names/milliseconds.json").read_text())
        before = copy.deepcopy(source)
        text, report = convert(source, convert_unit="ms:s", convert_metrics=["latency"])
        self.assertEqual(text, (ROOT / "examples/prom-names/selected-seconds.prom").read_text())
        self.assertEqual(source, before)
        self.assertEqual(report["unit_conversion_metrics"], ["latency"])
        self.assertEqual(report["sample_count"], 6)
        latency, elapsed = report["metrics"][:2]
        self.assertEqual((latency["original_unit"], latency["target_unit"]), ("ms", "s"))
        self.assertEqual([(p["original_value"], p["value"]) for p in latency["data_points"]],
                         [("1250", "1.250"), ("0.100000000000000000001", "0.000100000000000000000001")])
        self.assertEqual(elapsed["target_unit"], "ms")
        self.assertNotIn("original_value", elapsed["data_points"][0])
        self.assertFalse(any(x["operation"].startswith("exact_unit_conversion") for x in elapsed["normalizations"]))
        self.assertFalse(any(x["path"].startswith(elapsed["path"]) for x in report["losses"]))
        parsed = {f.name: f for f in text_string_to_metric_families(text)}
        self.assertEqual(parsed["elapsed_milliseconds"].samples[0].value, 9223372036854775807)
        self.assertEqual(parsed["latency_seconds"].samples[1].value, 1.25)

    def test_exact_raw_names_both_strategies_and_canonical_selection(self):
        for strategy in (WITH, WITHOUT):
            with self.subTest(strategy=strategy):
                source = document(gauge("wait.milliseconds", "ms", "1250"),
                                  gauge("wait-milliseconds", "ms", "2500"),
                                  sum_metric("work_milliseconds_total", "ms", value="5000"))
                names = ["work_milliseconds_total", "wait.milliseconds", "wait.milliseconds"]
                text, report = convert(source, strategy=strategy, convert_unit="ms:s", convert_metrics=names)
                self.assertIn("wait_seconds 1.250\n", text)
                self.assertIn("wait_milliseconds 2500\n", text)
                self.assertIn("work_seconds_total 5.000\n", text)
                self.assertEqual(report["unit_conversion_metrics"], sorted(set(names)))
                self.assertEqual(convert(source, strategy=strategy, convert_unit="ms:s",
                                         convert_metrics=sorted(set(names))), (text, report))
        # Stars, Unicode and surrounding spaces are literal parts of the raw name.
        for raw in ("latency*", "latency.µ", " latency "):
            with self.subTest(raw=raw):
                _, report = convert(document(gauge(raw, "ms")), convert_unit="ms:s", convert_metrics=[raw])
                self.assertEqual(report["unit_conversion_metrics"], [raw])

    def test_every_matching_descriptor_across_resources_scopes_and_empty_points(self):
        source = {"resourceMetrics": [
            {"scopeMetrics": [{"metrics": [gauge("latency", "ms", "1250", [attr("site", "a")])]},
                              {"metrics": [gauge("latency", "ms", "2500", [attr("site", "b")])]}]},
            {"scopeMetrics": [{"metrics": [gauge("latency", "ms", "3750", [attr("site", "c")]),
                                           {"name": "empty", "unit": "ms", "gauge": {}}]}]}]}
        text, report = convert(source, convert_unit="ms:s", convert_metrics=["latency", "empty"])
        self.assertIn('latency_seconds{site="c"} 3.750\n', text)
        self.assertEqual([m["target_unit"] for m in report["metrics"]], ["s"] * 4)
        self.assertEqual(report["sample_count"], 3)
        self.assertIn("# TYPE empty_seconds gauge\n", text)

    def test_invalid_selection_contract_and_unselected_validation(self):
        source = document(gauge("latency", "ms"), gauge("uptime", "s"))
        for names in (["missing"], ["Latency"], ["latency_seconds"], ["latency*"], [" latency"],
                      ["latency", "missing"]):
            with self.subTest(names=names), self.assertRaisesRegex(ConversionError, "unmatched.*" + names[-1].replace("*", r"\*")):
                convert(source, convert_unit="ms:s", convert_metrics=names)
        with self.assertRaisesRegex(ConversionError, "unmatched"):
            convert({}, convert_unit="ms:s", convert_metrics=["latency"])
        with self.assertRaisesRegex(ConversionError, "unit.*ms"):
            convert(source, convert_unit="ms:s", convert_metrics=["uptime"])
        with self.assertRaisesRegex(ConversionError, "requires.*ms:s"):
            convert(source, convert_metrics=["latency"])
        for names in ([], "latency", {"latency": True}, [""], [123], ["\ud800"], 3):
            with self.subTest(names=repr(names)), self.assertRaises(ConversionError):
                convert(source, convert_unit="ms:s", convert_metrics=names)
        for invalid in (gauge("other", "us"), gauge("other", "ms", str(2**63)),
                        {"name": "other", "unit": "ms", "histogram": {}}):
            with self.subTest(invalid=invalid), self.assertRaises(ConversionError):
                convert(document(gauge("latency", "ms"), invalid),
                        convert_unit="ms:s", convert_metrics=["latency"])
        tiny = {"name": "tiny", "unit": "ms", "gauge": {"dataPoints": [{"asDouble": Decimal("5e-324")}]}}
        mixed = document(gauge("latency", "ms"), tiny)
        text, _ = convert(mixed, convert_unit="ms:s", convert_metrics=["latency"])
        self.assertIn("tiny_milliseconds 5E-324\n", text)
        with self.assertRaisesRegex(ConversionError, "underflows"):
            convert(mixed, convert_unit="ms:s", convert_metrics=["tiny"])

    def test_selected_and_unselected_metrics_keep_all_collision_registries(self):
        cases = [
            (document(gauge("latency", "ms"), gauge("latency_seconds", "s")), WITH, "metric_name"),
            (document(sum_metric("work_milliseconds_total", "ms"), gauge("work_seconds", "s")), WITH, "parser_family"),
            (document(sum_metric("work_milliseconds", "ms"), gauge("work_seconds_total", "s")), WITHOUT, "final_series"),
            (document(gauge("latency", "ms"), gauge("latency", "s")), WITH, "conflicting_descriptor"),
            (document(gauge("latency", "ms", attrs=[attr("host.name", "a")]),
                      gauge("latency", "ms", attrs=[attr("host-name", "b")])), WITH, "label_name"),
            (document(gauge("latency", "ms"), gauge("other", "ms"), gauge("other", "ms")), WITH, "final_series"),
        ]
        for source, strategy, kind in cases:
            name = source["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["name"]
            with self.subTest(kind=kind):
                with self.assertRaises(ConversionError) as caught:
                    convert(source, strategy=strategy, convert_unit="ms:s", convert_metrics=[name])
                self.assertEqual(caught.exception.report["status"], "collision")
                self.assertIn(kind, {c["kind"] for c in caught.exception.report["collisions"]})

    def test_cli_selection_dry_run_and_failed_selection_preserves_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            output = directory / "selected.prom"
            mapping = Path(str(output) + ".mapping.json")
            bundle = directory / "result.zip"

            def run(*options):
                env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
                return subprocess.run([*CLI, "convert", str(ROOT / "examples/prom-names/milliseconds.json"), *map(str, options)],
                                      cwd=directory, env=env, capture_output=True, text=True, timeout=60)

            options = ("--convert-unit", "ms:s", "--convert-metric", "latency")
            result = run("--output", output, *options)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(), (ROOT / "examples/prom-names/selected-seconds.prom").read_text())
            previous = (output.read_bytes(), mapping.read_bytes())
            dry = run("--output", output, *options, "--convert-metric", "latency", "--dry-run")
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertEqual(json.loads(dry.stdout), json.loads(previous[1]))
            for existing in (False, True):
                for target in ("--output", "--bundle"):
                    for bad in (("--convert-metric", "latency"),
                                ("--convert-unit", "ms:s", "--convert-metric", "Latency"),
                                (*options, "--convert-metric", "uptime"),
                                (*options, "--convert-metric", "missing"),
                                (*options, "--convert-metric", "")):
                        for dry_run in ((), ("--dry-run",)):
                            with self.subTest(existing=existing, target=target, bad=bad, dry_run=dry_run):
                                paths = (output, mapping) if target == "--output" else (bundle,)
                                for path in paths:
                                    path.unlink(missing_ok=True)
                                    if existing:
                                        path.write_bytes(b"previous artifact")
                                result = run(target, paths[0], *bad, *dry_run)
                                self.assertEqual(result.returncode, 2, result.stdout)
                                self.assertEqual(json.loads(result.stderr)["status"], "error")
                                self.assertNotIn("Traceback", result.stderr)
                                for path in paths:
                                    if existing:
                                        self.assertEqual(path.read_bytes(), b"previous artifact")
                                    else:
                                        self.assertFalse(path.exists())

    def test_bundle_canonical_selection_replays_and_detects_changed_selection(self):
        source = (ROOT / "examples/prom-names/milliseconds.json").read_bytes()
        members, report = build_bundle(source, convert_unit="ms:s", convert_metrics=["latency", "latency"])
        self.assertEqual(members["exposition.prom"], (ROOT / "examples/prom-names/selected-seconds.prom").read_bytes())
        self.assertEqual(json.loads(members["manifest.json"])["configuration"]["convert_metrics"], ["latency"])
        self.assertEqual(build_bundle(source, convert_unit="ms:s", convert_metrics=["latency"]), (members, report))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.zip"
            atomic_write_bundle(output, members)
            self.assertTrue(verify_bundle(output, source=source)["reproduced"])
            for selection in (["elapsed_milliseconds_total"], None):
                manifest = json.loads(members["manifest.json"])
                if selection is None:
                    del manifest["configuration"]["convert_metrics"]
                else:
                    manifest["configuration"]["convert_metrics"] = selection
                atomic_write_bundle(output, {**members, "manifest.json": json.dumps(manifest).encode()})
                self.assertFalse(verify_bundle(output)["reproduced"])
                with self.assertRaisesRegex(ConversionError, "reproduce"):
                    verify_bundle(output, source=source)

    def test_bundle_rejects_noncanonical_or_inapplicable_selection(self):
        source = (ROOT / "examples/prom-names/milliseconds.json").read_bytes()
        members, _ = build_bundle(source, convert_unit="ms:s", convert_metrics=["latency"])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.zip"
            for names in ([], None, "latency", [""], [3], ["\ud800"], ["latency", "latency"],
                          ["latency", "elapsed_milliseconds_total"]):
                with self.subTest(names=repr(names)):
                    manifest = json.loads(members["manifest.json"])
                    manifest["configuration"]["convert_metrics"] = names
                    atomic_write_bundle(output, {**members, "manifest.json": json.dumps(manifest).encode()})
                    with self.assertRaises(ConversionError):
                        verify_bundle(output)
            manifest = json.loads(members["manifest.json"])
            manifest["configuration"]["convert_unit"] = None
            atomic_write_bundle(output, {**members, "manifest.json": json.dumps(manifest).encode()})
            with self.assertRaises(ConversionError):
                verify_bundle(output)
