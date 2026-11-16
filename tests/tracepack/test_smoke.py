# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from otelwrench.tracepack import convert, stringify

from .support import example, jsdumps

ZIPKIN = example("zipkin.json")


def test_zipkin_otlp_jaeger_otlp_is_precise_and_loss_free():
    otlp = convert(ZIPKIN, {"to": "otlp", "strictLoss": True})
    s = otlp["output"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert s["startTimeUnixNano"] == "1700000000000000000"
    assert s["endTimeUnixNano"] == "1700000000001200000"
    jaeger = convert(stringify(otlp["output"]), {"to": "jaeger", "strictLoss": True})
    back = convert(stringify(jaeger["output"]), {"to": "otlp", "strictLoss": True})
    assert back["output"] == otlp["output"]


def test_strict_loss_fails_before_returning_an_output():
    data = json.loads(ZIPKIN)
    data[0]["remoteEndpoint"] = {"serviceName": "db"}
    with pytest.raises(Exception, match="strict-loss"):
        convert(jsdumps(data), {"to": "otlp", "strictLoss": True})


def test_invalid_ids_and_shared_spans_fail_explicitly():
    data = json.loads(ZIPKIN)
    data[0]["id"] = "0000000000000000"
    with pytest.raises(Exception, match="nonzero"):
        convert(jsdumps(data), {"to": "otlp"})
    data[0]["id"] = "2222222222222222"
    data[0]["shared"] = True
    with pytest.raises(Exception, match="shared spans unsupported"):
        convert(jsdumps(data), {"to": "otlp"})
