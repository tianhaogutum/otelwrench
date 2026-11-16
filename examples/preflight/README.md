# `otelwrench preflight` example: an exporter upgrade that still scrapes fine

Fictional, synthetic data. `before.prom` and `after.prom` are two `/metrics` captures of an
imaginary `checkout-api` service, before and after an exporter upgrade that turns on
OTLP-to-Prometheus name suffixes and adopts newer attribute names. Both files are valid
Prometheus text, so the scrape keeps working. `checkout-dashboard.json` (Grafana-style
dashboard, 8 panels) and `checkout-alerts.yaml` (Prometheus rule file, 4 alerts and 2
recording rules) are what reads those metrics. `expected.json` is a hand-written oracle:
which of the 14 queries break and why.

```sh
otelwrench preflight before.prom after.prom --queries checkout-dashboard.json --queries checkout-alerts.yaml
```

What the upgrade changes:

| Change | Family | Effect on queries |
| --- | --- | --- |
| `_milliseconds` unit suffix added | `http_server_duration` (histogram) | latency panel and alert read nothing |
| `_total` counter suffix added | `checkout_orders` | orders panel and recording rule read nothing |
| renamed ms to s, values x0.001 | `queue_wait_milliseconds` → `queue_wait_seconds` | backlog alert reads nothing; a renamed query still needs `0.5`, not `500` |
| label renamed | `http_client_requests_total`: `status_code` → `http_response_status_code` | status panel and error-rate alert group by a label that is gone |
| family removed | `runtime_gc_pause_seconds` | GC panel reads nothing |
| type changed | `worker_jobs_active`: counter → gauge | `rate()` over a gauge |
| label added | `rpc_client_calls_total` gains `net_sock_peer_port` | queries keep working; series grow 2 → 24 |

The recorded run is in [`benchmarks/preflight/`](../../benchmarks/preflight/).
