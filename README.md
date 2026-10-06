# RV Analyzer

A self-hosted web app for remote-viewing practice with a Muse EEG headband:

- **EEG session analysis.** Upload Muse Monitor / Mind Monitor exports and get an annotated session
  graph, "intuitive windows", cross-session trends, and written feedback from **your own local LLM**
  (Open WebUI or Ollama). Nothing leaves your network.
- **RV targets.** A blind, tournament-style trial runner: get a random coordinate, sketch on an
  on-screen whiteboard (pen pressure and palm rejection for tablets and S Pen phones), rate your
  confidence, then see the target and score yourself. Optional 4-way judging gives an objective hit
  rate with a p-value against chance.
- **Everything is saved:** originals, graphs, sketches, scores, notes and stats, in one place.

> **A note on the science.** This is a practice and record-keeping tool. Band power cannot tell you
> whether a viewing was correct, and the app says so: the "top windows" always exist, even in
> noise, and the only accuracy evidence is your own scoring (or, better, the judging hit rate against
> chance). Treat results as a way to notice patterns in your own sessions, not as proof of anything.

New here? **[Explain it like I'm 5](docs/EXPLAIN-LIKE-IM-5.md)**: what this is, how it works, and a step-by-step first game.

The app has a built-in **Guide** page (`/guide`) with a legend of every term, how to read the graph, and what the
numbers can and cannot tell you.

## Quick start (Docker, about two minutes)
```bash
git clone https://github.com/Bagatron/rv-analyzer.git && cd rv-analyzer
cp .env.example .env          # optional: set a login, an AI endpoint, a Pexels key
docker compose up -d
```
Open **http://localhost:8000**. (If the published image isn't available yet, or you want to build it
yourself, use `docker compose up -d --build`.) Data lives in the `rv-data` Docker volume and survives updates:
`docker compose pull && docker compose up -d`.

No Docker? `pip install -r requirements.txt && RV_DATA_DIR=./data uvicorn app.main:app --port 8000`
(Python 3.12).

### Settings page (no config files needed)
Open **Settings** in the header. Everything there is optional:

- **AI coach:** server URL, type (Open WebUI or Ollama), model and API key, with a **Test** button.
- **Image targets:** Pexels key and contact. Without them, Wikimedia Commons is used (no key).
- **Judging:** 2 to 4 images.
- **Tracing:** an OTLP endpoint (for Tempo). Takes effect after a restart.
- **Component status** and the app version (also in every page footer).

Prometheus needs no setting here: point a scrape job at `/metrics`.

A value saved on the page overrides the matching environment variable, and each field shows where its
value comes from (settings, environment, default). Clearing a field returns to the environment/default.
Saved settings live in `settings.json` on the data volume, so they survive restarts and upgrades. API keys
are stored there as plain text.

Who can edit: when a login is set (`RV_AUTH_USER`/`RV_AUTH_PASSWORD`), or when `RV_SETTINGS_EDITABLE=true`.
Otherwise the page is read-only. The compose file sets `RV_SETTINGS_EDITABLE=true` for convenience on a
single machine; turn it off, or set a login, if other people can reach the port.

### Optional extras with Docker profiles
Nothing below is needed to use the app.

```bash
docker compose --profile ai up -d               # a local AI coach (Ollama), pulls RV_OLLAMA_MODEL (default llama3.2:3b)
docker compose --profile observability up -d    # Prometheus, Grafana (http://localhost:3000) and Tempo
```
- **AI coach:** after the model finishes downloading (`docker compose logs -f ollama-pull`), open
  Settings and enter URL `http://ollama:11434`, server type `ollama`, model `llama3.2:3b`. CPU-only is slow
  but works; for an NVIDIA GPU add a `deploy.resources.reservations.devices` block to the `ollama` service.
  Already running Ollama on the host? Use `http://host.docker.internal:11434` and skip the profile.
- **Observability:** Grafana (login `admin` / `admin`, change it) comes with Prometheus and Tempo already
  connected and an **RV Analyzer** dashboard. To send traces, put
  `OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318` in `.env` (or enter it on Settings and restart).
  Ports are bound to `127.0.0.1` only.

### Try it without a headband
```bash
python tests/make_sample.py tests/sample    # writes a synthetic Muse CSV and an Excel file
```
Upload those on the home page. For RV targets, just click *RV targets > Start new target*.

### Capturing EEG data
Any Muse Monitor or Mind Monitor CSV export works. If the Play Store Muse Monitor app no longer runs on
your phone, see "Recording without Muse Monitor" below for an experimental alternative.

## Security: read this before exposing it
The app has **no accounts** and stores personal data (brainwave recordings, your notes, your sketches).
- Keep it on your home network or behind a VPN. Do not put it on the open internet as is.
- To require a login, set `RV_AUTH_USER` and `RV_AUTH_PASSWORD` (HTTP Basic auth). Use HTTPS (a
  reverse proxy such as Caddy, Traefik or nginx) whenever it leaves a trusted network, because Basic
  auth sends the password in every request.
- `/healthz` and `/readyz` are always open for probes; `/metrics` is open unless
  `RV_AUTH_PROTECT_METRICS=true`.

## Configuration
Every value below can also be set on the **Settings** page (where marked there); a saved value overrides the environment.
Docker reads these from `.env`; Kubernetes from `k8s/configmap.yaml` and Secrets.

| Variable | Default | Purpose |
|---|---|---|
| `RV_DATA_DIR` | `/data` | Where sessions, sketches and the database live |
| `RV_MAX_UPLOAD_MB` | `200` | Upload size limit |
| `DATABASE_URL` | SQLite in the data dir | Set to Postgres to use it instead |
| `RV_AUTH_USER` / `RV_AUTH_PASSWORD` | off | Require HTTP Basic auth when both are set |
| `RV_SETTINGS_EDITABLE` | `false` | Let the Settings page save changes without a login (editing is always allowed when a login is set) |
| `RV_AI_BACKEND` | `openwebui` | `openwebui` or `ollama` |
| `RV_AI_BASE_URL`, `RV_AI_MODEL` | empty | AI coach turns on when both are set |
| `RV_AI_API_KEY` | empty | Open WebUI API key (Ollama needs none) |
| `RV_AI_AUTO` | `true` | Generate feedback automatically after each analysis |
| `RV_AI_TIMEOUT_S`, `RV_AI_TEMPERATURE`, `RV_AI_NUM_CTX`, `RV_AI_HISTORY_SESSIONS` | 600, 0.3, 8192, 5 | Tuning |
| `RV_TARGET_PROVIDER` | `auto` | `auto`, `wikimedia` or `pexels` |
| `RV_PEXELS_API_KEY` | empty | Free key for stock photos |
| `RV_CONTACT` | empty | Email or URL sent to Wikimedia in the User-Agent |
| `RV_JUDGING_OPTIONS` | `2` | Images to choose between in tournament judging (2 to 4) |
| `RV_LOG_LEVEL`, `RV_LOG_FORMAT` | `INFO`, `auto` | `json`, `text`, or `auto` (json inside Kubernetes) |
| `RV_PROBES`, `RV_PROBE_INTERVAL_S`, `RV_PROBE_AI_INTERVAL_S`, `RV_PROBE_TARGET_INTERVAL_S` | `true`, 30, 60, 900 | Background health checks behind `rv_component_up` |

## Kubernetes
```bash
git clone https://github.com/Bagatron/rv-analyzer.git && cd rv-analyzer
# optional: edit k8s/pvc.yaml (storageClassName) and k8s/configmap.yaml (AI endpoint)
kubectl apply -k k8s
kubectl -n rv-analyzer port-forward svc/rv-analyzer 8080:80      # then open http://localhost:8080
```
The image comes from `ghcr.io/bagatron/rv-analyzer`; pin `newTag` in `k8s/kustomization.yaml` to a
release for repeatable installs. Optional Secrets (all `optional: true`, so none are required):
```bash
kubectl -n rv-analyzer create secret generic rv-ai     --from-literal=api-key=sk-...
kubectl -n rv-analyzer create secret generic rv-pexels --from-literal=api-key=...
kubectl -n rv-analyzer create secret generic rv-auth   --from-literal=user=me --from-literal=password=...
```
An Ingress example and a Prometheus `ServiceMonitor` are in `k8s/optional/` (copy what you need into
`kustomization.yaml`). To build your own image: `docker build -t <registry>/rv-analyzer:<tag> .`.

**Single replica, `Recreate` strategy** is deliberate: the default SQLite DB and files sit on a
ReadWriteOnce volume, and analyses run on an in-process thread pool. Jobs left `pending` when a pod
restarts are re-queued on startup. With a `local-path` storage class the volume is tied to one node, so
the pod can only run there (and the image must be present on that node).

## AI coach (feedback written by a model on your Open WebUI machine)
The old coaching text was the same four canned tips under every window. Now the analysis
code computes a compact **digest** of each session (baselines, a coarse timeline, context
around every peak and marker, artifact checks, feedback agreement) and a model on your
own Open WebUI / Ollama machine writes the feedback from it. The numbers stay deterministic; the
model only interprets them. It also sees your earlier sessions and your notes, and you can
ask it to focus on something specific. It never sees the raw EEG.

The system prompt is deliberately honest about the limits: band power cannot show whether an
impression was correct, "top windows" always exist even in noise, and the only accuracy
evidence is your own scored feedback.

### Setup
1. On the LLM machine pull an instruction-tuned model that fits your GPU, e.g. `ollama pull llama3.1:8b`
   or `qwen2.5:7b-instruct` (a 3-4B model is faster but less insightful). Avoid code-only and
   role-play/persona models: they ignore the coach's instructions. Give it room for the
   prompt: `OLLAMA_CONTEXT_LENGTH=8192` on the Ollama service (the digest is ~1k tokens).
2. Open WebUI backend (default): create an API key under *Settings > Account > API keys*, then
   `kubectl -n rv-analyzer create secret generic rv-ai --from-literal=api-key=sk-...` (Docker: `RV_AI_API_KEY` in `.env`).
   If the API Keys section is missing, an admin must enable *Admin Panel > Settings > General > Enable API Key*.
   Ollama backend: set `RV_AI_BACKEND=ollama`, base URL `http://<laptop>:11434`, no key needed.
3. Set `RV_AI_BASE_URL` and `RV_AI_MODEL` (`.env` for Docker, `k8s/configmap.yaml` for Kubernetes), restart, and check
   `GET /api/ai/health` (reachable? is the model installed?).
4. Each session page has an **AI coach** panel: it runs automatically after analysis
   (`RV_AI_AUTO=false` to make it manual), or press *Generate / Regenerate*.
   `ai_prompt.txt` in the session files shows exactly what the model was given.

Feedback is generated one session at a time (single worker), survives pod restarts, and
failures (laptop asleep, model missing, timeout) are shown on the page without affecting the
analysis. Metrics: `rv_ai_feedback_total{status}`, `rv_ai_feedback_duration_seconds`.

## RV targets (blind remote-viewing trials)
Open **RV targets** in the header (`/rv`). *Start new target* picks a random photo and gives you a
coordinate like `4829-1736` (a random tag that says nothing about the target). Then:

1. Sketch on the whiteboard (pen with S Pen pressure, eraser, colours, undo, up to 6 pages, full
   screen) and write your impressions. Your draft auto-saves in the browser.
2. Enter a confidence score (0-100) and submit. The session is saved.
3. The target is revealed next to your sketches, with source, licence and attribution.
   Score how well it matched (0-100) and add reflections. Everything is stored per trial.

**Tournament judging** (checkbox on the start form): after you submit, you pick which of 2 images
(the target plus 1 decoy from a different category) best matches your session before the
reveal. Chance is 50%, so the hub reports your hit rate with an exact p-value. `RV_JUDGING_OPTIONS` (2 to 4,
default 2) sets how many images you choose between; trials judged with a different number are each compared
with their own chance level, so changing it never invalidates older results.

**The protocol is blind.** The target is chosen and fetched when the trial starts, but the server
refuses to serve it (or any detail about it) until you have submitted, and in judging mode until
you have judged. The pages and `/api/rv/trials` never contain it earlier, and its SHA-256 is stored
at creation so it cannot be swapped afterwards.

Targets come from curated themes with a single strong gestalt (water, towers, dunes, ice, bridges,
ruins, industry, skylines, sky/energy) and a filter that drops people, text, graphics and
distressing content. Two sources:
- **Wikimedia Commons** (default, no key): featured/quality pictures with open licences.
- **Pexels** (real stock photos): create a free API key at pexels.com/api (`RV_PEXELS_API_KEY`, or
  `kubectl -n rv-analyzer create secret generic rv-pexels --from-literal=api-key=...`); with the key present the app prefers Pexels and
  falls back to Commons.

The container/pod needs outbound HTTPS to `commons.wikimedia.org` and `upload.wikimedia.org` (and
`api.pexels.com` / `images.pexels.com`). Check it from the cluster with `GET /api/rv/targets/probe`.
Set `RV_CONTACT` to your email or URL; Wikimedia asks API clients to identify themselves.
Images keep their original licences (shown at the reveal, with attribution); they are used here only as
private practice targets, not republished.

### Tracing (Tempo)

Set `OTEL_EXPORTER_OTLP_ENDPOINT` (for Tempo, the OTLP receiver: port 4318 for HTTP, or 4317 with
`OTEL_EXPORTER_OTLP_PROTOCOL=grpc`) and the app exports traces. Unset, tracing is fully off.
Traces cover every HTTP request (not `/healthz`, `/readyz`, `/metrics`, static files), outgoing calls (AI backend,
image providers), database queries, and spans for `analysis.run`, `ai.feedback`, `trial_eeg.analysis`,
`targets.prepare` and the external probes. Work done on background threads stays attached to the request that started it.
Every response carries `X-Trace-ID`, and JSON logs carry `trace_id`/`span_id`, so a log line links to its trace.

Blind protocol: spans never carry the target or its theme, and query strings are stripped from every URL on every span.
Only traces are exported (Prometheus already has the metrics).

### Brainwaves during a trial (optional)
On the trial page, *Brainwave capture* connects a Muse over Bluetooth, records while you view and sketch
(with markers such as *begin viewing*), and attaches the recording when you submit. After the reveal the
trial page draws an interactive graph (calm-focus meter, five bands, phases between markers, a table view)
with a plain-language key, plus optional AI feedback. A recording from Muse Monitor / Mind Monitor can also
be attached after the reveal. The recording and its results are locked until the reveal, so the blind
protocol is unchanged.

- **Web Bluetooth needs a secure page.** Serve the app over HTTPS, or open it via `localhost`. On a LAN
  address over plain HTTP, Chrome hides Bluetooth; for one device you can enable
  `chrome://flags/#unsafely-treat-insecure-origin-as-secure` and add your address. Chrome on Android or desktop only
  (not iOS, not Firefox). Keep the screen on and the page open while capturing.
- **Experimental.** Tested against a simulated headband, not yet against many real devices. Values are written as
  log10(uV^2) like Muse Monitor, but are not guaranteed identical, so do not compare absolute levels between
  captures made here and Muse Monitor exports.
- Stored in the trial folder: `eeg_source.csv|xlsx`, `eeg_status.json`, `eeg_out/` (processed series, graphs, AI notes).

Data: table `rv_trials` (created automatically) and `/data/trials/<id>/` with `target.jpg`,
`decoy_*.jpg` and `sketch_N.png`. Metrics: `rv_trials_total{event}`, `rv_target_fetch_total{status}`.

## Logging and monitoring
**Logs** go to stdout, one JSON object per line in Kubernetes (plain text elsewhere): `ts`, `level`, `logger`, `msg`, a
`request_id`, and fields such as `trial_id`, `status` and `duration_ms`. Every response carries an `X-Request-ID` header
(send your own to trace a call). Useful Loki queries: `{namespace="rv-analyzer"} | json | level="WARNING"`,
`... | json | logger="rv.client"` (Bluetooth capture outcomes reported by the browser),
`... | json | msg=~"component DOWN"`.
Logs never contain request bodies, query strings, passwords, or anything that reveals a trial's target before the reveal.

**Metrics** at `/metrics`. The ones that say whether everything needed is working:

| Metric | Meaning |
|---|---|
| `rv_component_up{component=...}` | 1 working, 0 broken. Components: `database`, `data_volume` (writable, not just mounted), `workers`, `ai_backend` and `ai_model` (only when the AI coach is configured), `image_provider` (can a new target be fetched) |
| `rv_ready` | 1 when `database`, `data_volume` and `workers` are all up |
| `rv_component_last_success_timestamp_seconds`, `..._last_check_timestamp_seconds`, `rv_component_check_duration_seconds` | Freshness and cost of each check |
| `rv_data_volume_free_bytes`, `rv_data_volume_total_bytes` | Capacity |
| `rv_http_requests_total{method,route,status}`, `rv_http_request_duration_seconds`, `rv_http_requests_in_progress` | Traffic, by route template |
| `rv_analyses_in_progress`, `rv_ai_feedback_in_progress`, `rv_worker_queue_depth{pool}` | Work in flight and waiting |
| `rv_sessions{status}`, `rv_trials_stored{status}`, `rv_trials_total{event}`, `rv_trial_eeg_total{status}` | Content and outcomes |
| `rv_client_events_total{event}` | Bluetooth capture events from the browser (`bt_connected`, `bt_connect_failed`, `bt_disconnected`, ...) |
| `rv_info{version}`, `rv_ai_enabled` | Build and configuration |

The headband itself is a browser-side dependency, so it cannot be probed from the server; `rv_client_events_total` and the
`rv.client` log lines are how connection problems become visible. `GET /api/status` returns the same component data as JSON,
including why a component is down. `/healthz` and `/readyz` are unchanged (readiness depends only on the database and data
directory, so an AI laptop that is switched off never takes the app out of service).

Kubernetes: `k8s/optional/servicemonitor.yaml` scrapes it and `k8s/optional/prometheusrule.yaml` adds alerts (down, a required
component down, data volume under 15% free, AI coach or image provider down, 5xx errors, stale probes).

## Storage layout (on the PVC mounted at /data)
```
/data/rv.db                        SQLite: sessions, params, metrics, notes, status
/data/trend_rvindex.png            last generated trend graph
/data/sessions/<id>/original.csv|xlsx
/data/sessions/<id>/annotated_session.png
/data/sessions/<id>/rvindex_feedback_scatter.png   (if feedback present)
/data/sessions/<id>/intuitive_windows.csv
/data/sessions/<id>/recommended_moments.csv        (if feedback present)
/data/sessions/<id>/processed_timeseries.csv
/data/sessions/<id>/coaching_summary.txt, metrics.json, digest_base.json
/data/sessions/<id>/ai_feedback.md, ai_status.json, ai_prompt.txt   (AI coach)
```

## Recording without Muse Monitor
**Experimental.** `tools/muse-recorder.html` is a single-file Web Bluetooth recorder (Chrome on Android or
desktop, served over HTTPS or `localhost`). It is written from the public Muse BLE protocol and aims to
connect to a Muse 2 / S, show live band powers, and save a raw EEG CSV plus a 1 Hz band-power CSV you can
upload here. It has not been validated against many headband models or firmware versions, so reports
and fixes are welcome.

## Endpoints
`/` EEG sessions, `/rv` RV targets, `/api/sessions`, `/api/rv/trials`, `/api/ai/health`,
`/api/rv/targets/probe`, `/api/status`, `/healthz`, `/readyz`, `/metrics` (`rv_uploads_total`, `rv_analyses_total{status}`,
`rv_analysis_duration_seconds`, `rv_sessions{status}`, `rv_ai_feedback_total{status}`, `rv_trials_total{event}`).

## Development and tests
```bash
pip install -r requirements-dev.txt
python -m pytest -q                       # fake LLM and image servers; no network needed
python tests/browser_smoke.py             # drives the whiteboard in headless Chromium (playwright install chromium)
python tests/browser_eeg.py               # simulated Muse over mocked Bluetooth, end to end
```
CI runs the tests on every push and publishes the multi-arch image (amd64 and arm64) on `main` and on
version tags (`git tag v0.3.0 && git push --tags`). See `CONTRIBUTING.md`.

## Credits and licence
MIT licensed (see `LICENSE`). Target photos come from Wikimedia Commons (open licences, attribution shown
on reveal) or Pexels (Pexels License). Not affiliated with Interaxon (Muse), Open WebUI or Ollama.
