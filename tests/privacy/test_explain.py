import json

import pytest

from otelwrench.privacy.cli import main
from otelwrench.privacy.collector import run_stage
from otelwrench.privacy.evidence import diagnostics, view
from otelwrench.privacy.explain import explain_ignored


@pytest.mark.integration
def test_counterfactual_explains_ignored_bypass_without_changing_actual_output(
    collector, root, local_tmp
):
    out = local_tmp / "explained"
    code = main(
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
            "--explain-ignored",
            "--expect",
            str(root / "examples/privacy/privacy-fails.yaml"),
            "--out",
            str(out),
        ]
    )
    assert code == 1
    report = json.loads((out / "report.json").read_text())
    experiment = report["stages"][0]["ignored_rule_counterfactual"]
    assert experiment["status"] == "completed"
    affected = [
        row for row in experiment["affected_locations"] if '"ignored_secret"' in row["path"]
    ]
    assert len(affected) == 2
    assert all(row["status"] == "removed" for row in affected)
    primary = json.loads((out / "output.view.json").read_text())
    span = primary["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["attributes"]["ignored_secret"]["stringValue"] == "SYNTHETIC-SECRET"
    assert report["privacy_verdict"] == "fail"
    assert report["chain_verification"]["matched"]


def test_no_ignored_settings_starts_no_experiment(local_tmp):
    result = explain_ignored(
        local_tmp / "missing-binary",
        "traces",
        {},
        {},
        ("redaction", {"allow_all_keys": True}),
        local_tmp / "unused",
        1,
    )
    assert result["status"] == "not_applicable"
    assert not (local_tmp / "unused").exists()


@pytest.mark.integration
@pytest.mark.parametrize(
    "key,allow_all,summary,expected_changes,expected_data_effect",
    [
        ("ignored_secret", False, "silent", True, True),
        ("safe", True, "silent", False, False),
        ("safe", True, "debug", True, False),
    ],
)
def test_silent_counterfactual_distinguishes_real_bypass_impact_from_no_change(
    collector, root, local_tmp, key, allow_all, summary, expected_changes, expected_data_effect
):
    source = json.loads((root / "examples/privacy/traces.json").read_text())
    config = {
        "allow_all_keys": allow_all,
        "allowed_keys": ["safe", "card"],
        "ignored_keys": [key],
        "summary": summary,
        "blocked_values": ["4[0-9]{12}(?:[0-9]{3})?"],
    }
    processor = ("redaction/silent", config)
    actual = view(
        run_stage(
            collector,
            "traces",
            [json.dumps(source).encode()],
            "application/json",
            local_tmp / "actual",
            processor,
        )
    )
    if summary == "silent":
        assert diagnostics(view(source), actual) == []
    experiment = explain_ignored(
        collector, "traces", source, actual, processor, local_tmp / "without", 10
    )
    assert bool(experiment["changes_from_actual_stage"]) == expected_changes
    assert experiment["has_non_diagnostic_effect"] == expected_data_effect
