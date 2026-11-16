from pathlib import Path

from otelwrench.loki.config import parse_profile

ROOT = Path(__file__).resolve().parents[2]


def attr(key, value=None):
    return {"key": key, "value": {"stringValue": "v"} if value is None else value}


def logs(resource=None, scope=None, log=None, **fields):
    record = {"timeUnixNano": "1700000000000000001", "body": {"stringValue": "hello"},
              "attributes": [] if log is None else log}
    record.update(fields)
    return {"resourceLogs": [{"resource": {"attributes": [attr("service.name", {"stringValue": "checkout"})] if resource is None else resource},
                              "scopeLogs": [{"scope": {"attributes": [] if scope is None else scope}, "logRecords": [record]}]}]}


def profile(**otlp):
    return parse_profile({"limits_config": {"otlp_config": otlp}})
