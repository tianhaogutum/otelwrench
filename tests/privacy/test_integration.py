import json

import pytest

from otelwrench.privacy.cli import main
from otelwrench.privacy.collector import run_pipeline, run_stage
from otelwrench.privacy.evidence import differences, view
from otelwrench.privacy.inputs import ReplayError, load_yaml, sample_messages, selected_pipeline


pytestmark = pytest.mark.integration


def test_real_redaction_retains_ignored_and_outside_attributes(collector, root, local_tmp):
    messages, kind = sample_messages(root / "examples/privacy/traces.pb", "traces")
    signal, chain = selected_pipeline(load_yaml(root / "examples/privacy/collector.yaml"), "traces")
    result = view(run_stage(collector, signal, messages, kind, local_tmp / "redaction", chain[0]))
    rs = result["resourceSpans"][0]
    span = rs["scopeSpans"][0]["spans"][0]
    assert span["attributes"]["card"]["stringValue"] == "****"
    assert "customer.email" not in span["attributes"]
    assert span["attributes"]["ignored_secret"]["stringValue"] == "SYNTHETIC-SECRET"
    assert span["attributes"]["integer_card"]["intValue"] == "4111111111111111"
    assert span["events"][0]["attributes"]["event.secret"]["stringValue"] == "SYNTHETIC-SECRET"
    assert span["links"][0]["attributes"]["link.secret"]["stringValue"] == "SYNTHETIC-SECRET"
    assert "SYNTHETIC-SECRET" in span["name"]
    assert "SYNTHETIC-SECRET" in span["status"]["message"]
    assert "owner.email" not in rs["resource"]["attributes"]
    assert (
        rs["scopeSpans"][0]["scope"]["attributes"]["scope.secret"]["stringValue"]
        == "SYNTHETIC-SECRET"
    )


@pytest.mark.parametrize("signal", ["logs", "metrics"])
def test_unsupported_signals_are_explicit_errors(collector, root, local_tmp, signal):
    messages, kind = sample_messages(root / f"examples/privacy/{signal}.pb", signal)
    _, chain = selected_pipeline(load_yaml(root / "examples/privacy/collector.yaml"), signal)
    with pytest.raises(ReplayError, match="failed to start"):
        run_stage(collector, signal, messages, kind, local_tmp / signal, chain[0])


@pytest.mark.parametrize("signal", ["logs", "metrics"])
def test_real_sdk_signals_normalize_without_redaction(collector, root, local_tmp, signal):
    messages, kind = sample_messages(root / f"examples/privacy/{signal}.pb", signal)
    result = run_stage(collector, signal, messages, kind, local_tmp / signal, None)
    assert result
    assert "SYNTHETIC-SECRET" in json.dumps(result)


def test_cli_privacy_failure_is_nonzero_with_actual_leaks(collector, root, local_tmp):
    out = local_tmp / "privacy-fail"
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
            "--expect",
            str(root / "examples/privacy/privacy-fails.yaml"),
            "--out",
            str(out),
        ]
    )
    assert code == 1
    report = json.loads((out / "report.json").read_text())
    assert report["privacy_verdict"] == "fail"
    assert report["chain_verification"]["matched"]
    leaks = report["checks"][1]["matches"]
    assert any(row["area"] == "status_message" for row in leaks)
    assert any(row["area"] == "event_attributes" for row in leaks)
    assert (out / "output.json").is_file()


def test_whole_chain_matches_ordered_stages(collector, root, local_tmp):
    messages, kind = sample_messages(root / "examples/privacy/traces.pb", "traces")
    _, redaction = selected_pipeline(load_yaml(root / "examples/privacy/collector.yaml"), "traces")
    delete = (
        "attributes/delete_ignored",
        {"actions": [{"key": "ignored_secret", "action": "delete"}]},
    )
    first = run_stage(collector, "traces", messages, kind, local_tmp / "one", redaction[0])
    final = run_stage(
        collector,
        "traces",
        [json.dumps(first).encode()],
        "application/json",
        local_tmp / "two",
        delete,
    )
    whole = run_pipeline(
        collector, "traces", messages, kind, local_tmp / "whole", [*redaction, delete]
    )
    assert not differences(view(final), view(whole))
    span = view(whole)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert "ignored_secret" not in span["attributes"]
    assert span["attributes"]["card"]["stringValue"] == "****"


@pytest.mark.parametrize("signal", ["logs", "metrics"])
def test_actual_ottl_scrubs_all_fixture_markers_preserving_controls(
    collector, root, local_tmp, signal
):
    code = main(
        [
            "run",
            "--collector",
            str(collector),
            "--lock",
            str(root / ".tools/collector.lock.json"),
            "--config",
            str(root / "examples/privacy/collector-transform.yaml"),
            "--sample",
            str(root / f"examples/privacy/{signal}.pb"),
            "--pipeline",
            signal,
            "--expect",
            str(root / f"examples/privacy/{signal}-expectations.yaml"),
            "--out",
            str(local_tmp / signal),
        ]
    )
    assert code == 0
    report = json.loads((local_tmp / signal / "report.json").read_text())
    assert report["privacy_verdict"] == "pass"
    assert report["chain_verification"]["matched"]
    assert report["input_records"] == report["output_records"] == 2


def test_accepted_ottl_links_assignment_is_observed_as_noop(collector, root, local_tmp):
    messages, kind = sample_messages(root / "examples/privacy/traces.pb", "traces")
    _, chain = selected_pipeline(load_yaml(root / "examples/privacy/collector-noop-links.yaml"), "traces")
    result = run_stage(collector, "traces", messages, kind, local_tmp / "noop", chain[0])
    links = view(result)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["links"]
    assert links[0]["attributes"]["link.secret"]["stringValue"] == "SYNTHETIC-SECRET"
