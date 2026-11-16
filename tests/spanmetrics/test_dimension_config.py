# SPDX-License-Identifier: Apache-2.0
"""Reusable pair contract, exercised through the real CLI before implementation."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "otelwrench", "spanmetrics"]
CAPTURE = ROOT / "examples/spanmetrics/dimensions.json"
PAIR = json.loads((ROOT / "examples/spanmetrics/dimension-pair.json").read_text())
MAX_BYTES = 65_536


class DimensionConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.config = self.directory / "pair.json"
        self.output = self.directory / "comparison.json"
        self.save(PAIR)

    def save(self, doc):
        self.config.write_text(json.dumps(doc), encoding="utf-8")

    def run_cli(self, *options, source=CAPTURE, file=True):
        return subprocess.run(
            [*CLI, "compare", str(source), "--output", str(self.output),
             *(["--dimensions-file", str(self.config)] if file else []), *map(str, options)],
            capture_output=True, text=True, timeout=60, cwd=self.directory)

    def assert_invalid(self, expected):
        self.output.write_bytes(b"reviewed output sentinel")
        original = self.config.read_bytes()
        proc = self.run_cli("--force")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn(expected, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(self.output.read_bytes(), b"reviewed output sentinel")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ["comparison.json", "pair.json"])

    def test_reviewed_pair_equals_flags_and_normalizes_order(self):
        # Independent hand-counted fixture: six unique spans, seven input records.
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, self.output.read_text())
        result = json.loads(proc.stdout)
        self.assertTrue(result["comparison_complete"])
        self.assertEqual(result["unique_records"], 6)
        self.assertEqual(result["configurations"]["baseline"]["groups"], 2)
        self.assertEqual(result["configurations"]["candidate"]["groups"], 5)
        self.assertEqual(result["configurations"]["baseline"]["series"], 16)
        self.assertEqual(result["configurations"]["candidate"]["series"], 40)
        self.assertEqual(result["series_delta"], 24)
        options = []
        for side in ("baseline", "candidate"):
            for d in PAIR[side]:
                options += [f"--{side}-dimension", f'{d["source"]}:{d["key"]}={d["label"]}']
        flags = self.run_cli(*options, "--force", file=False)
        self.assertEqual(flags.returncode, 0, flags.stderr)
        self.assertEqual(flags.stdout, proc.stdout)
        reversed_pair = copy.deepcopy(PAIR)
        reversed_pair["candidate"].reverse()
        self.save(reversed_pair)
        repeat = self.run_cli("--force")
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertEqual(repeat.stdout, proc.stdout)

    def test_empty_selections_and_source_identity_are_independent_per_side(self):
        self.save({"schema_version": 1, "baseline": [], "candidate": []})
        proc = self.run_cli("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["series_delta"], 0)
        self.assertFalse(self.output.exists())
        # Same key across sources is valid; same label across sides is valid.
        self.save({"schema_version": 1,
                   "baseline": [{"source": "span", "key": "zone", "label": "zone"}],
                   "candidate": [{"source": "resource", "key": "zone", "label": "zone"},
                                 {"source": "span", "key": "zone", "label": "span_zone"}]})
        proc = self.run_cli("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["series_delta"], 8)

    def test_schema_fields_and_types_are_strict(self):
        cases = [([], "$"), ({}, "schema_version"),
                 (dict(PAIR, extra=True), "unsupported fields"),
                 (dict(PAIR, baseline=None), "baseline: expected array"),
                 (dict(PAIR, candidate={}), "candidate: expected array")]
        for version in (True, "1", 1.0, None, 0, 2):
            cases.append((dict(PAIR, schema_version=version), "schema_version"))
        for field in ("baseline", "candidate"):
            doc = copy.deepcopy(PAIR)
            del doc[field]
            cases.append((doc, field))
        for side in ("baseline", "candidate"):
            for entry in ("span:x=x", None, {}, dict(PAIR["baseline"][0], typo="x")):
                cases.append((dict(PAIR, **{side: [entry]}), side + "[0]"))
            for field in ("source", "key", "label"):
                for value in (None, 3, True, []):
                    entry = dict(PAIR["baseline"][0], **{field: value})
                    cases.append((dict(PAIR, **{side: [entry]}), side + "[0]." + field))
        for doc, error in cases:
            with self.subTest(doc=doc):
                self.save(doc)
                self.assert_invalid(error)

    def test_sources_keys_and_labels_are_validated_without_lossy_coercion(self):
        cases = [("source", "scope", "source"), ("source", "span:x=y", "source"),
                 ("key", "", "key"), ("key", "  ", "key"), ("key", "x=y", "key"),
                 ("key", "\ud800", "Unicode"), ("label", "x-y", "dimension"),
                 ("label", "1label", "dimension"), ("label", "", "dimension")]
        for field, value, error in cases:
            with self.subTest(field=field, value=value):
                self.save(dict(PAIR, candidate=[dict(PAIR["candidate"][0], **{field: value})]))
                self.assert_invalid(error)
        for label in ("service_name", "span_name", "span_kind", "status_code", "le", "__private"):
            with self.subTest(label=label):
                self.save(dict(PAIR, candidate=[{"source": "span", "key": "x", "label": label}]))
                self.assert_invalid("label collision/reserved name")

    def test_collisions_and_dimension_caps_on_both_sides(self):
        for side in ("baseline", "candidate"):
            cases = [([{"source": "span", "key": "x", "label": "same"},
                       {"source": "resource", "key": "x", "label": "same"}], "label collision"),
                     ([{"source": "span", "key": "x", "label": "a"},
                       {"source": "span", "key": "x", "label": "b"}], "duplicate dimension source: span:x"),
                     ([{"source": "resource", "key": "service.name", "label": "svc"}], "built-in")]
            for entries, error in cases:
                with self.subTest(side=side, entries=entries):
                    self.save(dict(PAIR, **{side: entries}))
                    self.assert_invalid(error)
            entries = [{"source": "span", "key": f"x{i}", "label": f"d{i}"} for i in range(33)]
            self.save(dict(PAIR, **{side: entries[:32]}))
            proc = self.run_cli("--dry-run")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.save(dict(PAIR, **{side: entries}))
            self.assert_invalid("at most 32")

    def test_json_syntax_duplicate_keys_constants_and_encoding(self):
        valid = json.dumps(PAIR).encode()
        cases = [(b"{", "JSON"), (valid + b" {}", "JSON"), (b"\xff", "utf-8"),
                 (valid.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1'), "duplicate JSON object key"),
                 (valid.replace(b'"source": "span"', b'"source": "span", "source": "resource"', 1), "duplicate JSON object key")]
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            cases.append((valid.replace(b'"schema_version": 1', b'"schema_version": ' + constant), "non-JSON numeric constant"))
        for data, error in cases:
            with self.subTest(data=data):
                self.config.write_bytes(data)
                self.assert_invalid(error)

    def test_file_byte_limit_is_independent_of_capture_limit(self):
        valid = json.dumps(PAIR).encode()
        self.config.write_bytes(valid + b" " * (MAX_BYTES - len(valid)))
        proc = self.run_cli("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.config.write_bytes(self.config.read_bytes() + b" ")
        self.assert_invalid("65536")

    def test_file_and_dimension_flags_cannot_be_mixed(self):
        for option in ("--baseline-dimension", "--candidate-dimension"):
            proc = self.run_cli(option, "span:x=x", "--dry-run")
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("cannot combine", proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertFalse(self.output.exists())

    def test_output_cannot_alias_configuration_even_with_force(self):
        original = self.config.read_bytes()
        for kind in ("path", "hardlink", "symlink"):
            with self.subTest(kind=kind):
                if kind == "hardlink":
                    os.link(self.config, self.output)
                elif kind == "symlink":
                    self.output.symlink_to(self.config)
                target = self.config if kind == "path" else self.output
                proc = self.run_cli("--output", target, "--force")
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("overwrite input", proc.stderr)
                self.assertEqual(self.config.read_bytes(), original)
                self.assertEqual(proc.stdout, "")
                self.output.unlink(missing_ok=True)

    def test_missing_file_directory_and_invalid_config_create_no_output(self):
        for path in (self.directory / "missing.json", self.directory):
            proc = self.run_cli("--dimensions-file", path)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("regular file", proc.stderr)
            self.assertFalse(self.output.exists())
        self.save(dict(PAIR, candidate=None))
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertFalse(self.output.exists())

    def test_valid_file_retains_dry_run_and_capture_rejection_contracts(self):
        self.output.write_text("sentinel")
        proc = self.run_cli("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.output.read_text(), "sentinel")
        proc = self.run_cli("--max-series", "16", "--force")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report, json.loads(self.output.read_text()))
        self.assertFalse(report["comparison_complete"])
        self.assertIsNone(report["series_delta"])
        self.assertEqual(report["partitions"], [])
        self.assertIsNone(report["configurations"]["baseline"]["series"])
        self.assertIsNone(report["configurations"]["candidate"]["series"])
        # A valid pair works on a multi-request capture as well.
        doc = json.loads(CAPTURE.read_bytes())
        source = self.directory / "capture.jsonl"
        source.write_text(json.dumps(doc) + "\n" + json.dumps(doc) + "\n")
        proc = self.run_cli("--input-format", "jsonl", "--force", source=source)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["series_delta"], 24)
        self.assertEqual(report["unique_records"], 6)


if __name__ == "__main__":
    unittest.main()
