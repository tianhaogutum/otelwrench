# SPDX-License-Identifier: Apache-2.0
"""Capture-free inspection of the same strict pair used by compare/conversion."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from otelwrench.spanmetrics import cli
from . import test_dimension_config as pairs


class ValidateDimensionTests(unittest.TestCase):
    setUp = pairs.DimensionConfigTests.setUp
    save = pairs.DimensionConfigTests.save

    def run_cli(self, *options):
        return subprocess.run(
            [*pairs.CLI, "validate-dimensions", "--dimensions-file", str(self.config),
             *map(str, options)], capture_output=True, text=True, timeout=60, cwd=self.directory)

    def expected(self, doc):
        return {"schema_version": 1, "report_type": "dimension_configuration", "status": "ok",
                "configurations": {side: {"dimensions": sorted(doc[side], key=lambda d: d["label"]),
                                          "custom_dimension_count": len(doc[side])}
                                   for side in ("baseline", "candidate")}}

    def assert_invalid(self, expected):
        original = self.config.read_bytes()
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn(expected, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ["pair.json"])

    def test_canonical_pair_without_capture_or_destinations(self):
        original = self.config.read_bytes()
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(json.loads(proc.stdout), self.expected(pairs.PAIR))
        self.assertEqual(proc.stdout, json.dumps(self.expected(pairs.PAIR), ensure_ascii=True,
                                               indent=2, sort_keys=True) + "\n")
        self.assertEqual(self.config.read_bytes(), original)
        reversed_pair = copy.deepcopy(pairs.PAIR)
        reversed_pair["candidate"].reverse()
        self.save(reversed_pair)
        again = self.run_cli()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout, proc.stdout)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ["pair.json"])

    def test_never_reads_capture_or_stdin_or_publishes(self):
        # This command must stay usable even when capture input is unavailable.
        stdin = unittest.mock.Mock()
        stdin.buffer.read.side_effect = AssertionError("stdin consumed")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli.sys, "stdin", stdin), redirect_stdout(stdout), redirect_stderr(stderr):
            with patch.object(cli, "read_input", side_effect=AssertionError("capture read")), \
                 patch.object(cli, "convert", side_effect=AssertionError("conversion")), \
                 patch.object(cli, "compare", side_effect=AssertionError("comparison")), \
                 patch.object(cli, "write_files", side_effect=AssertionError("publication")):
                code = cli.main(["validate-dimensions", "--dimensions-file", str(self.config)])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue()), self.expected(pairs.PAIR))
        self.assertEqual(stderr.getvalue(), "")
        stdin.buffer.read.assert_not_called()

    def test_empty_arrays_source_identity_and_unicode_keys(self):
        for doc in ({"schema_version": 1, "baseline": [], "candidate": []},
                    {"schema_version": 1,
                     "baseline": [{"source": "span", "key": "zone", "label": "zone"}],
                     "candidate": [{"source": "resource", "key": "zone", "label": "zone"},
                                   {"source": "span", "key": "zone", "label": "span_zone"},
                                   {"source": "span", "key": "区域:🚀", "label": "unicode"}]}):
            with self.subTest(doc=doc):
                self.save(doc)
                original = self.config.read_bytes()
                proc = self.run_cli()
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(proc.stdout), self.expected(doc))
                self.assertEqual(self.config.read_bytes(), original)

    def test_requires_file_and_rejects_capture_or_conversion_options(self):
        original = self.config.read_bytes()
        commands = [[*pairs.CLI, "validate-dimensions"],
                    [*pairs.CLI, "validate-dimensions", "--dimensions-file"]]
        for options in (("capture.json",), ("--output", self.config), ("--report", self.config),
                        ("--dimensions-side", "candidate"), ("--dimension", "span:x=x"),
                        ("--input", "-"), ("--input-format", "jsonl"), ("--max-input-bytes", "1"),
                        ("--force",), ("--dry-run",), ("--unknown",),
                        ("--dimensions-f", self.config)):
            commands.append([*pairs.CLI, "validate-dimensions", "--dimensions-file", str(self.config),
                             *map(str, options)])
        for command in commands:
            with self.subTest(command=command):
                proc = subprocess.run(command, capture_output=True, text=True, timeout=60, cwd=self.directory)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn("error:", proc.stderr)
                self.assertEqual(self.config.read_bytes(), original)
                self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ["pair.json"])
        help_result = self.run_cli("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--dimensions-file", help_result.stdout)
        self.assertNotIn("--output", help_result.stdout)

    # Exercise the established malformed-pair matrix through the new CLI route.
    test_schema_fields_and_types_are_strict = pairs.DimensionConfigTests.test_schema_fields_and_types_are_strict
    test_sources_keys_and_labels_are_validated_without_lossy_coercion = pairs.DimensionConfigTests.test_sources_keys_and_labels_are_validated_without_lossy_coercion
    test_json_syntax_duplicate_keys_constants_and_encoding = pairs.DimensionConfigTests.test_json_syntax_duplicate_keys_constants_and_encoding

    def test_byte_dimension_and_collision_boundaries_on_both_sides(self):
        valid = json.dumps(pairs.PAIR).encode()
        self.config.write_bytes(valid + b" " * (pairs.MAX_BYTES - len(valid)))
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), self.expected(pairs.PAIR))
        self.config.write_bytes(self.config.read_bytes() + b" ")
        self.assert_invalid("65536")
        for side in ("baseline", "candidate"):
            entries = [{"source": "span", "key": f"x{i}", "label": f"d{i}"} for i in range(33)]
            self.save(dict(pairs.PAIR, **{side: entries[:32]}))
            proc = self.run_cli()
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["configurations"][side]["custom_dimension_count"], 32)
            self.save(dict(pairs.PAIR, **{side: entries}))
            self.assert_invalid("at most 32")
            for invalid, error in (([entries[0], dict(entries[1], label="d0")], "label collision"),
                                   ([entries[0], dict(entries[0], label="other")], "duplicate dimension source"),
                                   ([{"source": "resource", "key": "service.name", "label": "svc"}], "built-in")):
                self.save(dict(pairs.PAIR, **{side: invalid}))
                self.assert_invalid(error)

    def test_missing_nonregular_paths_and_local_file_aliases(self):
        original = self.config.read_bytes()
        fifo = self.directory / "fifo"
        os.mkfifo(fifo)
        for path in (self.directory / "missing.json", self.directory, fifo):
            proc = self.run_cli("--dimensions-file", path)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertIn("regular file", proc.stderr)
            self.assertEqual(proc.stdout, "")
        fifo.unlink()
        for name, kind in (("hardlink", "hardlink"), ("symlink", "symlink"), ("-", "hardlink")):
            alias = self.directory / name
            if kind == "hardlink":
                os.link(self.config, alias)
            else:
                alias.symlink_to(self.config)
            proc = self.run_cli("--dimensions-file", name)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), self.expected(pairs.PAIR))
            self.assertEqual(alias.read_bytes(), original)
            alias.unlink()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ["pair.json"])


if __name__ == "__main__":
    unittest.main()
