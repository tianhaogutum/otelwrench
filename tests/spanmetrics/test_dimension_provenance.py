# SPDX-License-Identifier: Apache-2.0
"""Opt-in reviewed-byte provenance without changing default flag parity."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from otelwrench.spanmetrics import cli
from . import test_dimension_config as pairs
from . import test_conversion_dimensions as conversion
from .test_jsonl import jsonl, split_requests


class DimensionProvenanceTests(unittest.TestCase):
    setUp = conversion.ConversionDimensionTests.setUp
    save = pairs.DimensionConfigTests.save
    run_cli = conversion.ConversionDimensionTests.run_cli
    flags = conversion.ConversionDimensionTests.flags

    def provenance(self, side, data=None):
        return {"schema_version": 1, "selected_side": side,
                "sha256": hashlib.sha256(self.config.read_bytes() if data is None else data).hexdigest()}

    def test_opt_in_both_sides_hash_exact_bytes_and_preserve_defaults(self):
        original = self.config.read_bytes()
        for side in ("baseline", "candidate"):
            with self.subTest(side=side):
                default = self.run_cli("--force", side=side)
                self.assertEqual(default.returncode, 0, default.stderr)
                manifest, metrics = self.report.read_bytes(), self.output.read_bytes()
                proc = self.run_cli("--dimension-provenance", "--force", side=side)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                report = json.loads(proc.stdout)
                self.assertEqual(report.pop("dimension_pair_provenance"), self.provenance(side))
                self.assertEqual(report, json.loads(manifest))
                self.assertEqual(self.report.read_bytes(), proc.stdout.encode())
                self.assertEqual(self.output.read_bytes(), metrics)
                flags = self.run_cli(*self.flags(side), "--force", file=False)
                self.assertEqual(flags.returncode, 0, flags.stderr)
                self.assertEqual(self.report.read_bytes(), manifest)
                self.assertEqual(self.output.read_bytes(), metrics)
                self.assertEqual(self.config.read_bytes(), original)
        # Byte identity is deliberately stronger than semantic identity.
        doc = copy.deepcopy(pairs.PAIR)
        doc["candidate"].reverse()
        self.config.write_bytes(json.dumps(doc, indent=4).encode() + b"\n ")
        changed = self.config.read_bytes()
        proc = self.run_cli("--dimension-provenance", "--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["dimension_pair_provenance"], self.provenance("candidate"))
        self.assertNotEqual(report["dimension_pair_provenance"], self.provenance("candidate", original))
        self.assertEqual(self.output.read_bytes(), metrics)
        self.assertEqual(self.config.read_bytes(), changed)

    def test_hash_and_dimensions_come_from_one_bounded_read(self):
        original = self.config.read_bytes()
        real_open = Path.open
        opened = []

        def open_once(path, *args, **kwargs):
            if path == self.config and args == ("rb",):
                opened.append(path)
                self.assertEqual(len(opened), 1, "pair reopened to hash")
            return real_open(path, *args, **kwargs)

        stdout, stderr = io.StringIO(), io.StringIO()
        # Short reads expose code that hashes only a prefix or assumes one read.
        stream = io.BytesIO(original)
        sizes = []

        def short_read(size):
            sizes.append(size)
            return stream.read(min(size, 7))

        wrapped = unittest.mock.MagicMock()
        wrapped.__enter__.return_value.read.side_effect = short_read
        with patch.object(Path, "open", side_effect=lambda path, *a, **kw: wrapped
                          if path == self.config else real_open(path, *a, **kw), autospec=True) as opening, \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main([str(pairs.CAPTURE), "--output", str(self.output), "--dry-run",
                             "--dimensions-file", str(self.config), "--dimensions-side", "candidate",
                             "--dimension-provenance"])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(sum(call.args[0] == self.config for call in opening.call_args_list), 1)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["dimension_pair_provenance"], self.provenance("candidate", original))
        self.assertEqual(report["dimensions"][4:], sorted(pairs.PAIR["candidate"], key=lambda d: d["label"]))
        self.assertTrue(all(0 < size <= 65536 for size in sizes))
        self.assertFalse(self.output.exists())
        # If a caller changes the file after loading, the digest still describes
        # the validated selection, rather than the later contents at that path.
        read_input = cli.read_input

        def mutate_then_read(*args):
            self.config.write_bytes(b"invalid later content")
            return read_input(*args)

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli, "read_input", side_effect=mutate_then_read), \
             patch.object(Path, "open", autospec=True, side_effect=open_once), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main([str(pairs.CAPTURE), "--output", str(self.output), "--dry-run",
                             "--dimensions-file", str(self.config), "--dimensions-side", "candidate",
                             "--dimension-provenance"])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["dimension_pair_provenance"],
                         self.provenance("candidate", original))

    def test_jsonl_stdin_dry_run_and_rejections_keep_provenance(self):
        original = self.config.read_bytes()
        data = jsonl(*split_requests(json.loads(pairs.CAPTURE.read_bytes()))).decode()
        for side in ("baseline", "candidate"):
            proc = self.run_cli("--dimension-provenance", "--input-format", "jsonl", "--force",
                                source="-", side=side, data=data)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["dimension_pair_provenance"], self.provenance(side))
            self.assertEqual(report["input_sha256"], hashlib.sha256(data.encode()).hexdigest())
        metrics, manifest = self.output.read_bytes(), self.report.read_bytes()
        for content, options, code in ((data, ("--dry-run",), 0),
                                       (data + "{\n", (), 1),
                                       (data, ("--max-input-bytes", "10"), 1),
                                       (data, ("--max-series", "16"), 1)):
            proc = self.run_cli("--dimension-provenance", "--input-format", "jsonl", "--force",
                                *options, source="-", data=content)
            self.assertEqual(proc.returncode, code, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["dimension_pair_provenance"], self.provenance("candidate"))
            self.assertFalse(report["metrics_emitted"])
            self.assertEqual(self.output.read_bytes(), metrics)
            if code == 1:
                self.assertNotIn("prometheus_sha256", report)
                self.assertEqual(self.report.read_bytes(), proc.stdout.encode())
            else:
                self.assertEqual(self.report.read_bytes(), manifest)
            self.assertEqual(self.config.read_bytes(), original)

    def assert_option_error_before_capture(self, options, diagnostic):
        self.output.write_text("old metrics")
        self.report.write_text("old manifest")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli, "read_input", side_effect=AssertionError("capture consumed")), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["-", "--input-format", "jsonl", "--output", str(self.output),
                             "--report", str(self.report), "--force", "--dimension-provenance", *options])
        self.assertEqual(code, 2, stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn(diagnostic, stderr.getvalue())
        self.assertEqual(self.output.read_text(), "old metrics")
        self.assertEqual(self.report.read_text(), "old manifest")

    def test_incomplete_options_reject_before_capture(self):
        for options, error in (([], "requires --dimensions-file"),
                               (["--dimension", "span:method=method"], "requires --dimensions-file"),
                               (["--dimensions-file", str(self.config)], "requires --dimensions-side"),
                               (["--dimensions-side", "baseline"], "requires --dimensions-file"),
                               (["--dimensions-file", str(self.config), "--dimensions-side", "candidate",
                                 "--dimension", "span:method=method"], "cannot combine")):
            with self.subTest(options=options):
                self.assert_option_error_before_capture(options, error)

    def test_invalid_full_pair_and_limits_before_capture(self):
        options = ["--dimensions-file", str(self.config), "--dimensions-side", "candidate"]
        for data, error in ((b"{", "JSON"),
                            (json.dumps(dict(pairs.PAIR, baseline=[{"source": "span", "key": "x",
                                                                  "label": "le"}])).encode(), "baseline"),
                            (b" " * (pairs.MAX_BYTES + 1), "65536")):
            self.config.write_bytes(data)
            self.assert_option_error_before_capture(options, error)
            self.assertEqual(self.config.read_bytes(), data)
        self.config.unlink()
        self.assert_option_error_before_capture(options, "regular file")
        self.config.mkdir()
        self.assert_option_error_before_capture(options, "regular file")

    def test_provenance_preserves_both_destination_alias_checks(self):
        original = self.config.read_bytes()
        for destination in ("--output", "--report"):
            for kind in ("path", "hardlink", "symlink"):
                alias = self.directory / "alias"
                if kind == "hardlink":
                    os.link(self.config, alias)
                elif kind == "symlink":
                    alias.symlink_to(self.config)
                target = self.config if kind == "path" else alias
                self.assert_option_error_before_capture(
                    ["--dimensions-file", str(self.config), "--dimensions-side", "candidate",
                     destination, str(target)], "overwrite input")
                self.assertEqual(self.config.read_bytes(), original)
                alias.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
