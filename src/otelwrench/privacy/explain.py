"""Optional real-runtime counterfactuals, kept separate from actual policy output."""

from copy import deepcopy
import json
from pathlib import Path

from .collector import run_stage
from .evidence import diagnostics, differences, inventory, view


def explain_ignored(
    binary: Path,
    signal: str,
    input_document: dict,
    actual_view: dict,
    processor: tuple[str, dict],
    workdir: Path,
    timeout: float,
) -> dict:
    """Disable ignored settings as a group; let the actual upstream engine decide."""
    name, original_config = processor
    settings = {
        key: original_config[key]
        for key in ("ignored_keys", "ignored_key_patterns")
        if original_config.get(key)
    }
    if not settings:
        return {"status": "not_applicable", "reason": "no nonempty ignored settings on this stage"}
    config = deepcopy(original_config)
    for key in settings:
        config[key] = []
    alternative = run_stage(
        binary,
        signal,
        [json.dumps(input_document).encode()],
        "application/json",
        workdir,
        (name, config),
        timeout,
    )
    alternative_view = view(alternative)
    changed = [
        row for row in inventory(actual_view, alternative_view) if row["status"] != "retained"
    ]
    namespace_paths = {
        row["pointer"] for row in diagnostics({}, actual_view) + diagnostics({}, alternative_view)
    }
    data_changes = [
        row
        for row in changed
        if row["pointer"] not in namespace_paths
        and any(not isinstance(row[key], (dict, list)) for key in ("before", "after") if key in row)
    ]
    return {
        "status": "completed",
        "disabled_settings": settings,
        "configuration_change": "the listed ignored settings were set to empty lists",
        "source": "separate actual Collector replay on the same stage input and binary pin",
        "interpretation": "counterfactual output, not the configured pipeline output or an internal "
        "per-rule trace; attribution is to the disabled settings as a group",
        "changes_from_actual_stage": differences(actual_view, alternative_view),
        "affected_locations": changed,
        "non_diagnostic_affected_locations": data_changes,
        "has_non_diagnostic_effect": bool(data_changes),
        "diagnostic_effect_note": "redaction.* attribute changes and empty container changes are "
        "kept in affected_locations, but do not alone imply data changed",
    }
