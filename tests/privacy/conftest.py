from pathlib import Path

import pytest

from otelwrench.privacy.collector import identify


# Repository root: examples live in examples/privacy/, the local Collector pin in .tools/.
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def collector():
    binary = ROOT / ".tools/otelcol-contrib"
    if not binary.is_file():
        pytest.skip("install the local Collector to run genuine integration tests")
    if identify(binary)["version"] != "0.72.0":
        pytest.skip("these version-specific compatibility expectations require Collector 0.72.0")
    return binary


@pytest.fixture
def root():
    return ROOT


@pytest.fixture
def local_tmp(tmp_path_factory):
    # All test writes remain inside this worker's project, including temp files.
    return tmp_path_factory.mktemp("case")
