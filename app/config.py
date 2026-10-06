import os
from pathlib import Path

DATA_DIR = Path(os.getenv("RV_DATA_DIR", "/data"))
# SQLite on the PVC by default; set DATABASE_URL to use Postgres, e.g.
# postgresql+psycopg://user:pass@host:5432/rv
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR}/rv.db")
MAX_UPLOAD_MB = int(os.getenv("RV_MAX_UPLOAD_MB", "200"))
SESSIONS_DIR = DATA_DIR / "sessions"
TRIALS_DIR = DATA_DIR / "trials"

# ---- RV target generation -----------------------------------------------------------------
# Provider: "wikimedia" (Commons featured/quality pictures, no key needed), "pexels" (free API
# key, real stock photos), or "auto" (pexels when RV_PEXELS_API_KEY is set, else wikimedia).
TARGET_PROVIDER = os.getenv("RV_TARGET_PROVIDER", "auto").lower()
PEXELS_API_KEY = os.getenv("RV_PEXELS_API_KEY", "")
WIKIMEDIA_API = os.getenv("RV_WIKIMEDIA_API", "https://commons.wikimedia.org/w/api.php")
PEXELS_API = os.getenv("RV_PEXELS_API", "https://api.pexels.com/v1/search")
# Wikimedia asks API clients to identify themselves; put a contact (email or URL) here.
TARGET_CONTACT = os.getenv("RV_CONTACT", "")   # empty -> the project URL is sent instead
TARGET_TIMEOUT_S = int(os.getenv("RV_TARGET_TIMEOUT_S", "20"))
# Tournament judging: how many images you choose between (the target plus decoys). 2 = a coin flip, up to 4.
JUDGING_OPTIONS = max(2, min(4, int(os.getenv("RV_JUDGING_OPTIONS", "2"))))
# ---- AI feedback (Open WebUI / Ollama on the LLM laptop) -------------------------------
# AI feedback is enabled when both RV_AI_BASE_URL and RV_AI_MODEL are set.
#   RV_AI_BACKEND=openwebui -> POST {base}/api/chat/completions (needs RV_AI_API_KEY)
#   RV_AI_BACKEND=ollama    -> POST {base}/api/chat (talks to Ollama directly, key optional)
AI_BACKEND = os.getenv("RV_AI_BACKEND", "openwebui").lower()
AI_BASE_URL = os.getenv("RV_AI_BASE_URL", "").rstrip("/")
AI_MODEL = os.getenv("RV_AI_MODEL", "")
AI_API_KEY = os.getenv("RV_AI_API_KEY", "")
AI_TIMEOUT_S = int(os.getenv("RV_AI_TIMEOUT_S", "600"))   # a 1060 can take minutes on first load
AI_TEMPERATURE = float(os.getenv("RV_AI_TEMPERATURE", "0.3"))
AI_NUM_CTX = int(os.getenv("RV_AI_NUM_CTX", "8192"))      # ollama backend only
AI_AUTO = os.getenv("RV_AI_AUTO", "true").lower() in ("1", "true", "yes")  # run after each analysis
AI_HISTORY_SESSIONS = int(os.getenv("RV_AI_HISTORY_SESSIONS", "5"))

# ---- Optional login ---------------------------------------------------------------------
# The app has no accounts. Set both to require HTTP Basic auth on everything except /healthz and
# /readyz (and /metrics unless RV_AUTH_PROTECT_METRICS=true). Use HTTPS if it leaves your LAN.
AUTH_USER = os.getenv("RV_AUTH_USER", "")
AUTH_PASSWORD = os.getenv("RV_AUTH_PASSWORD", "")
AUTH_PROTECT_METRICS = os.getenv("RV_AUTH_PROTECT_METRICS", "false").lower() in ("1", "true", "yes")

# ---- Logging and health probes ----------------------------------------------------------
# RV_LOG_FORMAT: "json" (one object per line, good for Loki), "text", or "auto" (json inside Kubernetes).
LOG_LEVEL = os.getenv("RV_LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("RV_LOG_FORMAT", "auto").lower()
# Background checks that feed the rv_component_up metric. Seconds between checks of each kind.
PROBES_ENABLED = os.getenv("RV_PROBES", "true").lower() in ("1", "true", "yes")
PROBE_INTERVAL_S = int(os.getenv("RV_PROBE_INTERVAL_S", "30"))           # database, data volume, workers
PROBE_AI_INTERVAL_S = int(os.getenv("RV_PROBE_AI_INTERVAL_S", "60"))      # AI backend reachable + model present
PROBE_TARGET_INTERVAL_S = int(os.getenv("RV_PROBE_TARGET_INTERVAL_S", "900"))  # downloads one test image
