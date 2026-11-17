"""Privacy evidence from real local OpenTelemetry Collector executions."""

__version__ = "0.1.0"

# Listed by `otelwrench privacy --help` even when the `privacy` extra is not installed.
SUBCOMMANDS = {
    "run": "replay one selected local processor pipeline",
    "pin": "record an explicitly chosen binary's version and SHA-256",
    "install-collector": "download the pinned official Collector archive, verify its SHA-256 and pin it",
}
