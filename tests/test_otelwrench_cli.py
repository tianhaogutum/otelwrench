"""Top-level `otelwrench` dispatcher: overview, version, routing and error paths."""
import importlib.util
import subprocess
import sys

import pytest

import otelwrench
from otelwrench import cli

# Python ports of the former Node tools; their suites live beside the others. They use
# hand-written argument parsers: tracediff's help starts "usage: otelwrench tracediff",
# tracepack's and alert-flapsim's start "otelwrench <cmd> — ..." followed by "Usage:", and
# `help <cmd>` returns 0 instead of raising SystemExit. Their --version output is their
# own (tracediff prints its original version; see tests/tracediff), so it is not checked here.
PORTED = {"tracepack", "tracediff", "alert-flapsim"}
# argparse commands. preflight is new in otelwrench (no former tool name).
CORE = ["preflight", "sentry", "loki", "prom-names", "spanmetrics", "promdiff", "tailsim", "cardinality", "privacy"]
NEW = {"preflight"}


def run(*args, module=("-m", "otelwrench")):
    return subprocess.run([sys.executable, *module, *map(str, args)], capture_output=True, text=True, timeout=60)


def available(command):
    return cli._installed(cli.BY_NAME[command])


def skip_unless_available(command):
    if not available(command):
        pytest.skip(f"otelwrench {command} is not part of this build")


@pytest.mark.parametrize("args", [[], ["--help"], ["-h"], ["help"]])
def test_overview_lists_every_group_and_command(args, capsys):
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"otelwrench {otelwrench.__version__} ")
    for group in cli.GROUPS:
        assert f"\n{group}:\n" in out
    for command in cli.COMMANDS:
        assert f"\n  {command.name} " in out


def test_command_table_shape():
    assert cli.GROUPS == ("Metrics", "Traces", "Logs", "Alerts", "Collector", "Migration")
    # The hero command is listed first, in the first group of the overview.
    assert cli.COMMANDS[0].name == "preflight" and cli.COMMANDS[0].group == cli.GROUPS[0]
    assert [c.name for c in cli.COMMANDS if c.group == "Traces"] == ["tracepack", "tracediff", "spanmetrics", "tailsim"]
    assert [c.name for c in cli.COMMANDS if c.group == "Metrics"] == ["preflight", "prom-names", "promdiff", "cardinality"]
    assert {c.name: c.module for c in cli.COMMANDS} == {
        "preflight": "otelwrench.preflight.cli",
        "tracepack": "otelwrench.tracepack.cli", "tracediff": "otelwrench.tracediff.cli",
        "spanmetrics": "otelwrench.spanmetrics.cli", "tailsim": "otelwrench.tailsim.cli",
        "prom-names": "otelwrench.promnames.cli", "promdiff": "otelwrench.promdiff.cli",
        "cardinality": "otelwrench.cardinality.cli", "loki": "otelwrench.loki.cli",
        "alert-flapsim": "otelwrench.flapsim.cli", "sentry": "otelwrench.sentry.cli",
        "privacy": "otelwrench.privacy.cli",
    }
    # Only privacy needs an extra (DeepDiff, jsonpointer, jsonpath-ng, opentelemetry-proto).
    assert {c.name: c.extra for c in cli.COMMANDS if c.extra or c.requires} == {"privacy": "privacy"}
    assert [c.name for c in cli.COMMANDS if c.group == "Collector"] == ["privacy"]
    assert all(c.group in cli.GROUPS and c.summary for c in cli.COMMANDS)
    assert set(cli.LEGACY.values()) == set(cli.BY_NAME) - NEW
    assert not NEW & set(cli.LEGACY)


def test_overview_lists_preflight_first():
    out = cli.overview()
    assert out.index("\nMetrics:\n") < out.index("\nTraces:\n")
    assert out.split("\nMetrics:\n", 1)[1].startswith("  preflight ")


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version(flag, capsys):
    assert cli.main([flag]) == 0
    assert capsys.readouterr().out == f"otelwrench {otelwrench.__version__}\n"


@pytest.mark.parametrize("tool", CORE)
def test_tool_help_uses_new_program_name(tool):
    result = run(tool, "--help")
    assert result.returncode == 0, result.stderr
    assert f"usage: otelwrench {tool}" in result.stdout


@pytest.mark.parametrize("tool", CORE)
def test_help_subcommand_routes_to_tool_help(tool, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["help", tool])
    assert exc.value.code == 0
    assert f"usage: otelwrench {tool}" in capsys.readouterr().out


@pytest.mark.parametrize("tool", ["preflight", "sentry", "loki", "promdiff", "tailsim", "cardinality", "privacy"])
def test_tool_version_names_the_subcommand(tool):
    result = run(tool, "--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"otelwrench {tool} {otelwrench.__version__}\n"


def test_tool_exit_codes_pass_through():
    bad = run("tailsim", "no-such-subcommand")
    assert bad.returncode == 2 and "usage: otelwrench tailsim" in bad.stderr


def test_unknown_command_suggests_close_match(capsys):
    assert cli.main(["promdif"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'promdif'" in err and "did you mean 'promdiff'?" in err


@pytest.mark.parametrize("old,new", sorted((old, new) for old, new in cli.LEGACY.items() if old not in cli.BY_NAME))
def test_unknown_legacy_name_points_to_new_command(old, new, capsys):
    assert cli.main([old]) == 2
    assert f"{old} is now 'otelwrench {new}'" in capsys.readouterr().err


def test_missing_extra_prints_install_hint(monkeypatch, capsys):
    command = cli.BY_NAME["privacy"]
    real = cli._present
    monkeypatch.setattr(cli, "_present", lambda name: False if name in command.requires else real(name))
    assert "(needs otelwrench[privacy])" in cli.overview()
    assert cli.main(["privacy", "run"]) == 2
    assert "pip install 'otelwrench[privacy]'" in capsys.readouterr().err
    assert cli.main(["privacy", "--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: otelwrench privacy ") and "install-collector" in out
    assert "pip install 'otelwrench[privacy]'" in out


def test_unknown_command_without_match(capsys):
    assert cli.main(["zzzzzz"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'zzzzzz'" in err and "did you mean" not in err


def test_unknown_option(capsys):
    assert cli.main(["--frobnicate"]) == 2
    assert "unknown option '--frobnicate'" in capsys.readouterr().err


def test_missing_module_reports_not_available(monkeypatch, capsys):
    ghost = cli.Command("ghost", "otelwrench.ghost_tool.cli", "not shipped", "Migration")
    monkeypatch.setitem(cli.BY_NAME, "ghost", ghost)
    monkeypatch.setattr(cli, "COMMANDS", (*cli.COMMANDS, ghost))
    assert "ghost" in cli.overview() and "(coming soon)" in cli.overview()
    assert cli.main(["ghost", "--help"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("otelwrench: command 'ghost' is not available") and "Traceback" not in err


@pytest.mark.parametrize("module,command", [
    ("otelwrench.preflight", "preflight"), ("otelwrench.sentry", "sentry"), ("otelwrench.loki", "loki"), ("otelwrench.promnames", "prom-names"),
    ("otelwrench.spanmetrics", "spanmetrics"), ("otelwrench.promdiff", "promdiff"),
    ("otelwrench.tailsim", "tailsim"), ("otelwrench.cardinality", "cardinality"),
    ("otelwrench.privacy", "privacy"),
])
def test_subpackage_main_routes_through_dispatcher(module, command):
    result = run("--help", module=("-m", module))
    assert result.returncode == 0 and f"usage: otelwrench {command}" in result.stdout


@pytest.mark.parametrize("module,command", [
    ("otelwrench.tracepack", "tracepack"), ("otelwrench.tracediff", "tracediff"), ("otelwrench.flapsim", "alert-flapsim"),
])
def test_ported_subpackage_main_routes_through_dispatcher(module, command):
    result = run("--help", module=("-m", module))
    assert result.returncode == 0, result.stderr
    assert "usage: " in result.stdout.lower() and f"otelwrench {command}" in result.stdout


SUBCOMMANDS = {
    "preflight": [],
    "tracepack": ["convert", "bundle", "verify", "extract"],
    "tracediff": [],
    "spanmetrics": ["compare", "validate-dimensions"],
    "tailsim": ["replay", "verify-replay", "verify-replay-evidence", "replay-arrivals", "verify-arrivals",
                "verify-arrivals-evidence", "compare-arrivals", "verify-compare-arrivals",
                "verify-compare-arrivals-evidence", "compare", "verify-compare", "verify-compare-evidence"],
    "prom-names": ["convert", "verify", "inspect"],
    "promdiff": [],
    "cardinality": [],
    "loki": ["preview", "explain", "compare"],
    "alert-flapsim": ["replay", "checkpoint", "compare", "compare-reports", "compare-report-set", "grid", "join"],
    "sentry": ["convert"],
    "privacy": ["run", "pin", "install-collector"],
}
ALL_HELP = [(name, None) for name in SUBCOMMANDS] + [(name, sub) for name, subs in SUBCOMMANDS.items() for sub in subs]


def _main(argv, capsys):
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_subcommand_table_covers_every_command():
    assert set(SUBCOMMANDS) == set(cli.BY_NAME)
    assert set(CORE) | PORTED == set(cli.BY_NAME)


@pytest.mark.parametrize("flag", ["--help", "-h"])
@pytest.mark.parametrize("command,sub", ALL_HELP, ids=lambda v: v or "-")
def test_every_command_and_subcommand_help_exits_0(command, sub, flag, capsys):
    skip_unless_available(command)
    argv = [command, *([sub] if sub else []), flag]
    code, out, err = _main(argv, capsys)
    assert code in (0, None), (argv, err)
    assert "usage" in out.lower(), (argv, out)
    assert f"otelwrench {command}" in out, (argv, out)
    if sub:
        assert f"otelwrench {command} {sub}" in out, (argv, out)


@pytest.mark.parametrize("command", sorted(PORTED))
def test_ported_help_subcommand_routes_to_tool_help(command, capsys):
    skip_unless_available(command)
    code, out, err = _main(["help", command], capsys)
    assert code in (0, None), err
    assert "usage" in out.lower() and f"otelwrench {command}" in out


def test_ported_help_first_line():
    assert run("tracediff", "--help").stdout.startswith("usage: otelwrench tracediff ")
    for command in ("tracepack", "alert-flapsim"):
        out = run(command, "--help").stdout
        assert out.startswith(f"otelwrench {command} \u2014 ") and "\nUsage:" in out


@pytest.mark.parametrize("command", sorted(PORTED))
def test_ported_commands_route_through_dispatcher(command):
    skip_unless_available(command)
    result = run(command, "--help")
    assert result.returncode == 0, result.stderr
    assert f"otelwrench {command}" in result.stdout


def test_core_commands_are_installed():
    assert all(available(command) for command in CORE)
    assert all(importlib.util.find_spec(cli.BY_NAME[command].module) for command in CORE)
