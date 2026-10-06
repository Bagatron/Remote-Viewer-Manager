# Changelog

## 0.4.1
- The trend graph on the main page now includes EEG recorded inside RV trials alongside uploaded sessions, on one timeline with a legend.

## 0.4.0
- Settings page: AI server, image keys, judging, tracing endpoint, with test buttons and live component status.
  Saved values override environment variables and persist on the data volume.
- Version in every page footer.
- Docker Compose profiles: `ai` (Ollama) and `observability` (Prometheus, Grafana with a starter dashboard, Tempo).

## 0.3.3
- OpenTelemetry tracing to Tempo / any OTLP backend (off unless an endpoint is set); `X-Trace-ID` header and
  `trace_id` in logs. Query strings and the hidden target never appear on spans.
- Judging uses 2 images by default (`RV_JUDGING_OPTIONS`, 2 to 4), with exact statistics for mixed histories.
- Bluetooth capture reconnects automatically after a drop and reports why it dropped.

## 0.3.2
- Logging (JSON in Kubernetes) and Prometheus metrics, including `rv_component_up` for every dependency.
