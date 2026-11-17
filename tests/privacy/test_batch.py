import json

import pytest
import yaml

from otelwrench.privacy.cli import main
from otelwrench.privacy.inputs import load_yaml


@pytest.mark.integration
@pytest.mark.parametrize("first", [True, False])
def test_real_batch_flushes_on_shutdown_in_either_privacy_pipeline_order(
    collector, root, local_tmp, first
):
    config = load_yaml(root / "examples/privacy/collector.yaml")
    config["processors"]["batch/replay"] = {
        "timeout": "1h",
        "send_batch_size": 8192,
        "send_batch_max_size": 8192,
    }
    names = (
        ["batch/replay", "redaction/privacy"] if first else ["redaction/privacy", "batch/replay"]
    )
    config["service"]["pipelines"]["traces"]["processors"] = names
    path = local_tmp / "collector.yaml"
    path.write_text(yaml.safe_dump(config))
    out = local_tmp / "out"
    code = main(
        [
            "run",
            "--collector",
            str(collector),
            "--lock",
            str(root / ".tools/collector.lock.json"),
            "--config",
            str(path),
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
    assert code == 0
    report = json.loads((out / "report.json").read_text())
    assert report["input_records"] == report["output_records"] == 1
    assert report["chain_verification"]["matched"]
    assert report["privacy_verdict"] == "pass"
