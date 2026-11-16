import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from .helpers import ROOT, attr, logs


class CliTests(unittest.TestCase):
    def run_cli(self, *arguments, cwd=None):
        return subprocess.run([sys.executable, "-m", "otelwrench", "loki", *map(str, arguments)],
                              cwd=cwd or ROOT, text=True, capture_output=True, timeout=60,
                              env={**os.environ, "UV_OFFLINE": "1"})

    def test_preview_push_e2e_matches_independent_golden(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "push.json"
            result = self.run_cli("preview", ROOT / "examples/loki/logs.json", "--config", ROOT / "examples/loki/loki.yaml",
                                  "--push-json", target, cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            report = json.loads(result.stdout)
            # Golden was authored independently from the converter.
            self.assertEqual(json.loads(target.read_text()), json.loads((ROOT / "examples/loki/expected-push.json").read_text()))
            self.assertEqual(report["summary"], {"recordCount": 2, "streamCount": 1, "droppedAttributeCount": 2})
            self.assertEqual([r["dropped"][0]["key"] for r in report["records"]], ["debug_secret", "debug_secret"])
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["push.json"])

    def test_compare_e2e_stream_counts_and_placements(self):
        result = self.run_cli("compare", "examples/loki/logs.json", "--before", "examples/loki/defaults.yaml", "--after", "examples/loki/loki.yaml")
        self.assertEqual(result.returncode, 0, result.stderr)
        diff = json.loads(result.stdout)
        self.assertEqual(diff["before"], {"recordCount": 2, "streamCount": 2, "droppedAttributeCount": 0})
        self.assertEqual(diff["after"], {"recordCount": 2, "streamCount": 1, "droppedAttributeCount": 2})
        self.assertEqual(diff["streamCountDelta"], -1)
        self.assertEqual(diff["changedPlacementCount"], 4)
        self.assertEqual({c["key"] for c in diff["changedPlacements"]}, {"k8s_pod_name", "debug_secret"})
        self.assertEqual(len(diff["changedStreams"]), 2)

    def test_read_only_preview_creates_no_files(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli("preview", ROOT / "examples/loki/logs.json", "--config", ROOT / "examples/loki/loki.yaml", cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_existing_file_and_symlink_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "existing"
            existing.write_text("keep me")
            link = Path(directory) / "link"
            link.symlink_to(existing)
            for target in (existing, link):
                with self.subTest(target=target):
                    result = self.run_cli("preview", "examples/loki/logs.json", "--config", "examples/loki/loki.yaml", "--push-json", target)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("File exists", result.stderr)
                    self.assertEqual(existing.read_text(), "keep me")
                    self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["existing", "link"])

    def test_invalid_second_record_does_not_write_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            data = json.loads((ROOT / "examples/loki/logs.json").read_text())
            data["resourceLogs"][1]["scopeLogs"][0]["logRecords"][0]["timeUnixNano"] = "0"
            source, target = Path(directory) / "bad.json", Path(directory) / "push.json"
            source.write_text(json.dumps(data))
            result = self.run_cli("preview", source, "--config", "examples/loki/loki.yaml", "--push-json", target)
            self.assertEqual(result.returncode, 2)
            self.assertIn("missing timestamp", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(target.exists())
            self.assertEqual([x.name for x in Path(directory).iterdir()], ["bad.json"])

    def test_missing_files_bad_json_and_bad_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bad.json"
            for content in (b"{", b"\xff", b'{"resourceLogs": [], "resourceLogs": []}', b'{"resourceLogs": null}'):
                source.write_bytes(content)
                with self.subTest(content=content):
                    result = self.run_cli("preview", source, "--config", "examples/loki/loki.yaml")
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("error:", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(self.run_cli("preview", Path(directory) / "missing", "--config", "examples/loki/loki.yaml").returncode, 2)
            self.assertEqual(self.run_cli("preview", "examples/loki/logs.json", "--config", Path(directory) / "missing").returncode, 2)

    def test_unsafe_yaml_cannot_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "executed"
            config = Path(directory) / "unsafe.yaml"
            config.write_text(f'!!python/object/apply:os.system ["touch {marker}"]')
            result = self.run_cli("preview", "examples/loki/logs.json", "--config", config)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertFalse(marker.exists())
            self.assertNotIn("Traceback", result.stderr)

    def test_malformed_yaml_duplicate_and_unsupported_mapping_options(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bad.yaml"
            for text in ("{", "limits_config: {}\nlimits_config: {}\n", "limits_config: {discover_service_name: []}",
                         "limits_config: {otlp_config: {log_attributes: [{action: index_label, attributes: [x]}]}}",
                         "limits_config: {otlp_config: {log_attributes: [{action: drop, regex: '['}]}}"):
                config.write_text(text)
                with self.subTest(text=text):
                    result = self.run_cli("preview", "examples/loki/logs.json", "--config", config)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn("Traceback", result.stderr)

    def test_missing_output_parent_does_not_create_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "absent" / "push.json"
            result = self.run_cli("preview", "examples/loki/logs.json", "--config", "examples/loki/loki.yaml", "--push-json", target)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(target.parent.exists())

    def test_no_labels_push_rejected_but_preview_available(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "no-labels.yaml"
            config.write_text("limits_config: {otlp_config: {resource_attributes: {ignore_defaults: true}}}")
            target = Path(directory) / "push.json"
            result = self.run_cli("preview", "examples/loki/logs.json", "--config", config)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["summary"]["streamCount"], 1)
            result = self.run_cli("preview", "examples/loki/logs.json", "--config", config, "--push-json", target)
            self.assertEqual(result.returncode, 2)
            self.assertIn("nonempty index label", result.stderr)
            self.assertFalse(target.exists())

    def test_normalization_error_is_actionable(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "collision.json"
            source.write_text(json.dumps(logs(log=[attr("a.b"), attr("a_b")])) )
            result = self.run_cli("preview", source, "--config", "examples/loki/loki.yaml")
            self.assertEqual(result.returncode, 2)
            self.assertIn("normalized key collision 'a_b'", result.stderr)
            self.assertIn("logRecords[0]", result.stderr)

    def test_compare_rejects_invalid_after_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bad.yaml"
            config.write_text("limits_config: {otlp_config: {unsupported: true}}")
            result = self.run_cli("compare", "examples/loki/logs.json", "--before", "examples/loki/defaults.yaml", "--after", config)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")

    def test_help_version_and_required_arguments(self):
        self.assertEqual(self.run_cli("--help").returncode, 0)
        self.assertEqual(self.run_cli("--version").stdout.strip(), "otelwrench loki 0.1.0")
        self.assertEqual(self.run_cli("preview", "examples/loki/logs.json").returncode, 2)
        self.assertEqual(self.run_cli("compare", "examples/loki/logs.json", "--before", "examples/loki/defaults.yaml").returncode, 2)
        self.assertEqual(self.run_cli("preview", "examples/loki/logs.json", "--config", "examples/loki/loki.yaml", "--endpoint", "https://example.invalid").returncode, 2)
