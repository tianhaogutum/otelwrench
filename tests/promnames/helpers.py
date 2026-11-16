# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
CLI = [sys.executable, "-m", "otelwrench", "prom-names"]


def attr(key, value, kind="stringValue"):
    return {"key": key, "value": {kind: value}}


def gauge(name="temperature", unit="", value="42", attrs=None, **extra):
    return {"name": name, "unit": unit, "gauge": {"dataPoints": [
        {"asInt": value, "attributes": attrs or []}]}, **extra}


def sum_metric(name="requests", unit="1", monotonic=True, value="7", **extra):
    return {"name": name, "unit": unit, "sum": {"aggregationTemporality": 2,
            "isMonotonic": monotonic, "dataPoints": [{"asInt": value}]}, **extra}


def document(*metrics, resource=None):
    return {"resourceMetrics": [{"resource": resource or {}, "scopeMetrics": [{"metrics": list(metrics)}]}]}
