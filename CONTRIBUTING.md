# Contributing

Bug reports, fixes and ideas are welcome.

- **Run the tests:** `pip install -r requirements-dev.txt && python -m pytest -q`. They use fake LLM and
  image servers, so no network, GPU or headband is needed.
- **Whiteboard changes:** also run `python tests/browser_smoke.py` (needs `playwright install chromium`).
- **Keep the blind protocol blind.** The server must never send a trial's target (image, title, source
  or option mapping) before the viewer has submitted. Tests in `tests/test_rv_trials.py` cover this; add one
  for any new endpoint that touches a trial.
- **Keep the AI honest.** The coach only interprets a deterministic digest (`app/digest.py`); numbers
  are computed in code, and the system prompt states what EEG band power cannot show.
- **No secrets or personal data** in commits, issues or screenshots: API keys, session recordings, hostnames.
- Releases: tag `vX.Y.Z` on `main`; CI publishes `ghcr.io/<owner>/rv-analyzer` for amd64 and arm64.
