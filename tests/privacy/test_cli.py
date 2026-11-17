"""`otelwrench privacy` command surface: program names, subcommands and the installer entry."""
import subprocess
import sys

import pytest

import otelwrench
from otelwrench import cli as top
from otelwrench.privacy import SUBCOMMANDS
from otelwrench.privacy import cli


def _run(*args):
    return subprocess.run([sys.executable, "-m", "otelwrench", "privacy", *args],
                          capture_output=True, text=True, timeout=60)


def test_help_lists_every_subcommand():
    result = _run("--help")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: otelwrench privacy ")
    for name in SUBCOMMANDS:
        assert name in result.stdout


@pytest.mark.parametrize("sub", ["run", "pin"])
def test_subcommand_help_uses_new_program_name(sub, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([sub, "--help"])
    assert exc.value.code == 0
    assert f"usage: otelwrench privacy {sub}" in capsys.readouterr().out


def test_install_collector_help_is_routed_to_the_installer(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["install-collector", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: otelwrench privacy install-collector")
    assert "--dest" in out and "--force" in out


def test_installer_keeps_the_release_pin():
    from otelwrench.privacy import install

    assert install.LIMIT == 50 * 1024 * 1024
    assert install.KNOWN_ARCHIVES == {
        ("0.72.0", "darwin", "arm64"): "77777ea9aaa73b5ffb0cf7fef689fdf9826dca1ccaed9cf9666135b38ad06e70",
    }


def test_version_names_the_subcommand():
    result = _run("--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"otelwrench privacy {otelwrench.__version__}\n"


def test_missing_lock_is_an_input_error(root, tmp_path, capsys):
    binary = tmp_path / "otelcol-contrib"
    binary.write_text("#!/bin/sh\necho 'otelcol-contrib version 0.72.0'\n")
    binary.chmod(0o755)
    code = cli.main(["run", "--config", str(root / "examples/privacy/collector.yaml"),
                     "--sample", str(root / "examples/privacy/traces.pb"),
                     "--pipeline", "traces", "--collector", str(binary), "--lock", str(tmp_path / "missing.json"),
                     "--out", str(tmp_path / "out")])
    assert code == 2
    assert capsys.readouterr().err.startswith("error: ")
    assert not (tmp_path / "out").exists()


def test_dispatcher_declares_the_privacy_extra():
    command = top.BY_NAME["privacy"]
    assert command.extra == "privacy" and command.group == "Collector"
    assert {"deepdiff", "jsonpointer", "jsonpath_ng"} <= set(command.requires)
    assert top.LEGACY["otel-privacy-replay"] == "privacy"
