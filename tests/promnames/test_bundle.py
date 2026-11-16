# SPDX-License-Identifier: Apache-2.0
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from otelwrench.promnames.core import WITHOUT
from .helpers import CLI, ROOT


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.output = self.directory / "result.zip"
        self.source = (ROOT / "examples/prom-names/milliseconds.json").read_bytes()

    def run_cli(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        return subprocess.run([*CLI, *map(str, args)], cwd=self.directory, env=env,
                              capture_output=True, encoding="utf-8", timeout=60)

    def create(self, *options):
        return self.run_cli("convert", ROOT / "examples/prom-names/milliseconds.json",
                            "--bundle", self.output, "--convert-unit", "ms:s", *options)

    def read_members(self):
        with zipfile.ZipFile(self.output) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def rewrite(self, members):
        with zipfile.ZipFile(self.output, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)

    def test_bundle_golden_hashes_reproduction_and_determinism(self):
        result = self.create("--promote-resource", "absent", "--promote-resource", "absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        members = self.read_members()
        self.assertEqual(set(members), {"exposition.prom", "mapping.json", "manifest.json"})
        self.assertEqual(members["exposition.prom"], (ROOT / "examples/prom-names/seconds.prom").read_bytes())
        manifest = json.loads(members["manifest.json"])
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["input"], {"sha256": hashlib.sha256(self.source).hexdigest(),
                                             "size_bytes": len(self.source)})
        self.assertEqual(manifest["configuration"], {
            "strategy": "UnderscoreEscapingWithSuffixes", "convert_unit": "ms:s",
            "promote_resource": ["absent"], "allow_nonfinite": False})
        for name in ("exposition.prom", "mapping.json"):
            self.assertEqual(manifest["artifacts"][name], {
                "sha256": hashlib.sha256(members[name]).hexdigest(), "size_bytes": len(members[name])})
        self.assertEqual(json.loads(members["mapping.json"])["sample_count"], 6)
        original = self.output.read_bytes()
        self.assertEqual(self.create("--promote-resource", "absent").returncode, 0)
        self.assertEqual(self.output.read_bytes(), original)
        for source_args, reproduced in [((), False), (("--input", ROOT / "examples/prom-names/milliseconds.json"), True)]:
            verified = self.run_cli("verify", self.output, *source_args)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(json.loads(verified.stdout)["reproduced"], reproduced)
            self.assertEqual(self.output.read_bytes(), original)
        # Reproduced mapping agrees with the established loose-file interface.
        legacy = self.directory / "legacy.prom"
        result = self.run_cli("convert", ROOT / "examples/prom-names/milliseconds.json", "--output", legacy,
                              "--convert-unit", "ms:s", "--promote-resource", "absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(str(legacy) + ".mapping.json").read_bytes(), members["mapping.json"])

    def test_verify_rejects_changed_artifacts_even_with_valid_zip_crc(self):
        self.assertEqual(self.create().returncode, 0)
        original = self.read_members()
        for name in ("exposition.prom", "mapping.json"):
            for changed in (original[name] + b"\n", b"!" + original[name][1:]):
                with self.subTest(name=name, same_size=len(changed) == len(original[name])):
                    self.rewrite({**original, name: changed})
                    result = self.run_cli("verify", self.output)
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertIn(name, json.loads(result.stderr)["errors"][0])
                    self.assertNotIn("Traceback", result.stderr)

    def test_reproduction_detects_self_consistent_but_wrong_mapping_or_config(self):
        self.assertEqual(self.create().returncode, 0)
        original = self.read_members()
        for changed_config in (False, True):
            with self.subTest(changed_config=changed_config):
                members = dict(original)
                manifest = json.loads(members["manifest.json"])
                if changed_config:
                    manifest["configuration"]["strategy"] = WITHOUT
                else:
                    members["mapping.json"] += b"\n"
                    manifest["artifacts"]["mapping.json"] = {
                        "sha256": hashlib.sha256(members["mapping.json"]).hexdigest(),
                        "size_bytes": len(members["mapping.json"])}
                members["manifest.json"] = json.dumps(manifest).encode()
                self.rewrite(members)
                self.assertEqual(self.run_cli("verify", self.output).returncode, 0)
                result = self.run_cli("verify", self.output, "--input", ROOT / "examples/prom-names/milliseconds.json")
                self.assertEqual(result.returncode, 2)
                self.assertIn("reproduce", result.stderr)

    def test_verify_rejects_changed_input(self):
        self.assertEqual(self.create().returncode, 0)
        source = self.directory / "changed.json"
        source.write_bytes(self.source + b"\n")
        result = self.run_cli("verify", self.output, "--input", source)
        self.assertEqual(result.returncode, 2)
        self.assertIn("input", result.stderr)

    def test_replays_all_configuration_and_empty_input(self):
        source = self.directory / "nonfinite.json"
        source.write_text('{"resourceMetrics":[{"resource":{"attributes":[{"key":"service.name",'
                          '"value":{"stringValue":"test"}}]},"scopeMetrics":[{"metrics":[{"name":'
                          '"wait.milliseconds","unit":"ms","gauge":{"dataPoints":[{"asDouble":"NaN"}]}}]}]}]}')
        result = self.run_cli("convert", source, "--bundle", self.output, "--strategy", WITHOUT,
                              "--convert-unit", "ms:s", "--promote-resource", "service.name", "--allow-nonfinite")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'wait_seconds{service_name="test"} NaN', self.read_members()["exposition.prom"])
        result = self.run_cli("verify", self.output, "--input", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        source.write_text("{}")
        self.assertEqual(self.run_cli("convert", source, "--bundle", self.output).returncode, 0)
        self.assertEqual(self.read_members()["exposition.prom"], b"")
        self.assertEqual(self.run_cli("verify", self.output, "--input", source).returncode, 0)

    def test_write_failures_preserve_old_bundle_or_publish_nothing(self):
        from otelwrench.promnames import bundle

        members, _ = bundle.build_bundle(self.source, convert_unit="ms:s")
        real_write = zipfile.ZipFile.writestr
        real_close = zipfile.ZipFile.close

        def partial_write(archive, info, data, *args, **kwargs):
            # Real partial archive bytes reach disk before the injected failure.
            real_write(archive, info, data[:3], *args, **kwargs)
            raise OSError("injected member write failure")

        def close_failure(archive):
            was_open = archive.fp is not None
            real_close(archive)
            if was_open:
                raise OSError("injected archive finalization failure")

        for exists in (False, True):
            for stage in ("exposition.prom", "mapping.json", "manifest.json", "close", "fsync", "replace"):
                with self.subTest(exists=exists, stage=stage), contextlib.ExitStack() as stack:
                    self.output.unlink(missing_ok=True)
                    if exists:
                        # A different, valid previous generation.
                        old_members, _ = bundle.build_bundle(b"{}")
                        bundle.atomic_write_bundle(self.output, old_members)
                    previous = self.output.read_bytes() if exists else None
                    if stage.endswith((".prom", ".json")):
                        def fail_selected(archive, info, data, *args, **kwargs):
                            if info.filename == stage:
                                return partial_write(archive, info, data, *args, **kwargs)
                            return real_write(archive, info, data, *args, **kwargs)
                        stack.enter_context(patch.object(zipfile.ZipFile, "writestr", fail_selected))
                    elif stage == "close":
                        stack.enter_context(patch.object(zipfile.ZipFile, "close", close_failure))
                    else:
                        stack.enter_context(patch.object(bundle.os, stage, side_effect=OSError("injected " + stage)))
                    with self.assertRaises(OSError):
                        bundle.atomic_write_bundle(self.output, members)
                    self.assertEqual(self.output.read_bytes() if exists else None, previous)
                    if not exists:
                        self.assertFalse(self.output.exists())
                    self.assertEqual(list(self.directory.iterdir()), [self.output] if exists else [])
        bundle.atomic_write_bundle(self.output, members)
        self.assertTrue(bundle.verify_bundle(self.output, source=self.source)["reproduced"])

    def test_cli_reports_publication_failure_without_updating_previous_bundle(self):
        from otelwrench.promnames import cli

        self.assertEqual(self.create().returncode, 0)
        previous = self.output.read_bytes()
        source = self.directory / "empty.json"
        source.write_text("{}")
        errors = io.StringIO()
        with patch.object(cli.os, "replace", side_effect=OSError("injected replacement failure")), \
                contextlib.redirect_stderr(errors):
            result = cli.main(["convert", str(source), "--bundle", str(self.output)])
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(errors.getvalue())["status"], "error")
        self.assertIn("atomic replacement", errors.getvalue())
        self.assertEqual(self.output.read_bytes(), previous)

    def test_verifier_reads_one_generation_during_concurrent_replacement(self):
        from otelwrench.promnames import bundle

        previous, _ = bundle.build_bundle(b"{}")
        replacement, _ = bundle.build_bundle(self.source, convert_unit="ms:s")
        bundle.atomic_write_bundle(self.output, previous)
        real_read = zipfile.ZipFile.read

        def swap_after_first_member(archive, name, *args, **kwargs):
            result = real_read(archive, name, *args, **kwargs)
            if name == "exposition.prom":
                bundle.atomic_write_bundle(self.output, replacement)
            return result

        with patch.object(zipfile.ZipFile, "read", swap_after_first_member):
            self.assertTrue(bundle.verify_bundle(self.output, source=b"{}")["reproduced"])
        self.assertTrue(bundle.verify_bundle(self.output, source=self.source)["reproduced"])

    def test_dry_run_validation_and_destination_protection(self):
        self.assertEqual(self.create().returncode, 0)
        previous = self.output.read_bytes()
        dry = self.create("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(json.loads(dry.stdout)["sample_count"], 6)
        self.assertEqual(self.output.read_bytes(), previous)
        failure = self.run_cli("convert", ROOT / "examples/prom-names/collisions.json", "--bundle", self.output)
        self.assertEqual(failure.returncode, 2)
        self.assertEqual(self.output.read_bytes(), previous)
        self.output.unlink()
        self.assertEqual(self.create("--dry-run").returncode, 0)
        self.assertFalse(self.output.exists())
        source = self.directory / "input.json"
        source.write_bytes(self.source)
        for alias in ("same", "symlink", "hardlink"):
            with self.subTest(alias=alias):
                target = source
                if alias != "same":
                    target = self.directory / alias
                    if alias == "symlink":
                        target.symlink_to(source)
                    else:
                        target.hardlink_to(source)
                result = self.run_cli("convert", source, "--bundle", target)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(source.read_bytes(), self.source)
        for options in [("--output", self.directory / "out.prom"), ("--mapping", self.directory / "map.json")]:
            self.assertEqual(self.create(*options).returncode, 2)
        for destination in (self.directory, self.directory / "missing" / "result.zip"):
            self.assertEqual(self.run_cli("convert", source, "--bundle", destination).returncode, 2)

    def test_malformed_archives_and_manifests_fail_cleanly(self):
        self.assertEqual(self.create().returncode, 0)
        original = self.read_members()
        manifest = json.loads(original["manifest.json"])
        cases = [
            {**original, "manifest.json": b"{"},
            {**original, "manifest.json": b"[]"},
            {**original, "manifest.json": b'{"schema_version":1,"schema_version":1}'},
            {**original, "manifest.json": json.dumps({**manifest, "schema_version": True}).encode()},
            {**original, "manifest.json": json.dumps({**manifest, "configuration": []}).encode()},
            {**original, "manifest.json": json.dumps({**manifest, "configuration": {
                **manifest["configuration"], "allow_nonfinite": "false"}}).encode()},
            {**original, "manifest.json": json.dumps({**manifest, "artifacts": []}).encode()},
            {**original, "manifest.json": json.dumps({**manifest, "input": {"sha256": "bad", "size_bytes": 1}}).encode()},
            {name: content for name, content in original.items() if name != "mapping.json"},
            {**original, "../extra": b"never extract me"},
        ]
        for members in cases:
            with self.subTest(members=members):
                self.rewrite(members)
                result = self.run_cli("verify", self.output)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(json.loads(result.stderr)["status"], "error")
                self.assertNotIn("Traceback", result.stderr)
        self.rewrite(original)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.output, "a") as archive:
                archive.writestr("mapping.json", b"duplicate")
        self.assertEqual(self.run_cli("verify", self.output).returncode, 2)
        for raw in (b"not a zip", self.output.read_bytes()[:40]):
            self.output.write_bytes(raw)
            result = self.run_cli("verify", self.output)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
