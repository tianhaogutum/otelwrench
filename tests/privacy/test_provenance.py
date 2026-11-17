import hashlib
from importlib.metadata import version as package_version
import json

import pytest

from otelwrench.privacy import cli


@pytest.mark.integration
def test_hashes_describe_consumed_snapshots_even_if_paths_change(
    collector, root, local_tmp, monkeypatch
):
    config = local_tmp / "config.yaml"
    sample = local_tmp / "sample.pb"
    config_bytes = (root / "examples/privacy/collector.yaml").read_bytes()
    sample_bytes = (root / "examples/privacy/traces.pb").read_bytes()
    config.write_bytes(config_bytes)
    sample.write_bytes(sample_bytes)
    original_yaml = cli.load_yaml
    original_sample = cli.sample_messages

    def mutate_after_parse(path, data=None):
        result = original_yaml(path, data)
        if path == config:
            path.write_text("changed after parsing")
        return result

    def mutate_after_decode(path, signal, fmt="auto", data=None):
        result = original_sample(path, signal, fmt, data)
        path.write_bytes(b"changed after snapshot")
        return result

    monkeypatch.setattr(cli, "load_yaml", mutate_after_parse)
    monkeypatch.setattr(cli, "sample_messages", mutate_after_decode)
    out = local_tmp / "result"
    code = cli.main(
        [
            "run",
            "--collector",
            str(collector),
            "--lock",
            str(root / ".tools/collector.lock.json"),
            "--config",
            str(config),
            "--sample",
            str(sample),
            "--pipeline",
            "traces",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    report = json.loads((out / "report.json").read_text())
    assert report["input"]["config_sha256"] == hashlib.sha256(config_bytes).hexdigest()
    assert report["input"]["sample_sha256"] == hashlib.sha256(sample_bytes).hexdigest()
    assert (
        report["outputs"]["raw_sha256"]
        == hashlib.sha256((out / "output.json").read_bytes()).hexdigest()
    )
    assert report["dependencies"]["jsonpointer"] == package_version("jsonpointer")
    assert report["privacy_verdict"] == "not_asserted"


@pytest.mark.integration
def test_runtime_change_invalidates_evidence(collector, root, local_tmp, monkeypatch):
    original = cli.digest
    monkeypatch.setattr(
        cli, "digest", lambda path: "0" * 64 if path == collector else original(path)
    )
    out = local_tmp / "changed-runtime"
    code = cli.main(
        [
            "run",
            "--collector",
            str(collector),
            "--lock",
            str(root / ".tools/collector.lock.json"),
            "--config",
            str(root / "examples/privacy/collector.yaml"),
            "--sample",
            str(root / "examples/privacy/traces.pb"),
            "--pipeline",
            "traces",
            "--out",
            str(out),
        ]
    )
    assert code == 2
    report = json.loads((out / "report.json").read_text())
    assert report["status"] == "error"
    assert report["privacy_verdict"] == "not_evaluated"


@pytest.mark.integration
def test_whole_pipeline_discrepancy_never_passes_privacy(collector, root, local_tmp, monkeypatch):
    monkeypatch.setattr(cli, "run_pipeline", lambda *args, **kwargs: {"resourceSpans": []})
    out = local_tmp / "discrepancy"
    code = cli.main(
        [
            "run",
            "--collector",
            str(collector),
            "--lock",
            str(root / ".tools/collector.lock.json"),
            "--config",
            str(root / "examples/privacy/collector.yaml"),
            "--sample",
            str(root / "examples/privacy/traces.pb"),
            "--pipeline",
            "traces",
            "--expect",
            str(root / "examples/privacy/expectations.yaml"),
            "--out",
            str(out),
        ]
    )
    assert code == 2
    report = json.loads((out / "report.json").read_text())
    assert report["chain_verification"]["matched"] is False
    assert report["privacy_verdict"] == "not_evaluated"
    assert json.loads((out / "output.json").read_text()) == {"resourceSpans": []}
