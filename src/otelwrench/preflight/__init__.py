"""``otelwrench preflight``: one CI verdict on a before/after Prometheus ``/metrics`` pair.

The checks are the existing commands' library functions: ``promdiff`` (schema
contract and growth gate), ``prom-names`` (OTLP-to-Prometheus naming rules, used to
explain removed+added families as suffix renames) and ``cardinality`` (per-label
distinct values and collision-checked label removal). Nothing is written.
"""
from .. import __version__

SCHEMA = "otelwrench.preflight/v1"

__all__ = ["SCHEMA", "__version__"]
