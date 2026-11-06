"""Runtime configuration for cuadra-core.

All values have operational defaults and can be overridden via environment
variables. Versions: always latest stable of each dependency (no pins).
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/


def _load_env_file() -> None:
    """Load backend/.env (if present) into os.environ without overwriting.

    Keeps secrets (Telegram token, API keys) out of the shell history and out
    of git. Real environment variables always win.
    """
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env_file()

DATA_DIR = Path(os.environ.get("CUADRA_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "cuadra.db"
PHOTOS_DIR = DATA_DIR / "fotos"
AUDIOS_DIR = DATA_DIR / "audios"
IMAGES_DIR = DATA_DIR / "images"
LOGS_DIR = DATA_DIR / "logs"
LOG_FILE = LOGS_DIR / "cuadra.log"
LOCK_FILE = DATA_DIR / "cuadra.lock"
MIGRATIONS_DIR = BASE_DIR / "migrations"

# Built dashboard (Vite output). Served as a static SPA at "/" when present.
WEB_DIR = Path(
    os.environ.get("CUADRA_WEB_DIR", BASE_DIR / "cuadra" / "web_dashboard")
)

# Dashboard: shared PIN on the LAN. Empty = disabled (development only).
PIN = os.environ.get("CUADRA_PIN", "0000")

# Development mode: enables debug endpoints (POST /api/debug/mensaje, etc.)
DEBUG = os.environ.get("CUADRA_DEBUG", "1") == "1"

# Service port (dashboard API + static files + Phase 2 webhooks)
PORT = int(os.environ.get("CUADRA_PORT", "8321"))

# Telegram. Empty token or enabled=0 => dummy mode (development/tests).
TELEGRAM_ENABLED = os.environ.get("CUADRA_TELEGRAM_ENABLED", "1") == "1"
TELEGRAM_TOKEN = os.environ.get("CUADRA_TELEGRAM_TOKEN", "")

# Telegram userbot (Telethon), default identity for broadcast groups and
# private conversations. The Bot API account remains available as a fallback.
# Requires api_id/api_hash from https://my.telegram.org and a session created
# once with scripts/telegram_login.py. Empty => disabled.
TELETHON_API_ID = int(os.environ.get("CUADRA_TELETHON_API_ID", "0") or 0)
TELETHON_API_HASH = os.environ.get("CUADRA_TELETHON_API_HASH", "")
TELETHON_PHONE = os.environ.get("CUADRA_TELETHON_PHONE", "")
TELETHON_SESSION = Path(
    os.environ.get("CUADRA_TELETHON_SESSION", DATA_DIR / "telegram_user")
)

# Userbot anti-flood safety. Telegram rate limits are per account: after a
# FloodWait every userbot action must wait, not just the failing job.
USERBOT_MIN_SEND_GAP_S = float(os.environ.get("CUADRA_USERBOT_MIN_SEND_GAP_S", "8"))
TELEGRAM_PEER_FLOOD_COOLDOWN_S = float(
    os.environ.get("CUADRA_TELEGRAM_PEER_FLOOD_COOLDOWN_S", "3600")
)

# LLM transport. "openai" = any OpenAI-compatible endpoint (Cuadra's own
# llama.cpp server by default, also LM Studio/vLLM); "ollama" = native /api/chat.
# For "openai" the base URL ends in /v1; for "ollama" it is the server root.
LLM_PROVIDER = os.environ.get("CUADRA_LLM_PROVIDER", "openai").strip().lower()
LLM_BASE_URL = os.environ.get(
    "CUADRA_LLM_BASE_URL",
    "http://127.0.0.1:11434" if LLM_PROVIDER == "ollama" else "http://127.0.0.1:8080/v1",
).rstrip("/")
LLM_API_KEY = os.environ.get("CUADRA_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("CUADRA_LLM_MODEL", "Bonsai-8B-Q1_0.gguf")
LLM_TIMEOUT_S = float(os.environ.get("CUADRA_LLM_TIMEOUT_S", "120"))
LLM_TEMPERATURE = float(os.environ.get("CUADRA_LLM_TEMPERATURE", "0.4"))

# Managed local llama.cpp server (see cuadra/ai/llm_server.py). When enabled,
# Cuadra starts/stops its own server so the bot needs no external LLM app.
LLM_MANAGE = os.environ.get("CUADRA_LLM_MANAGE", "1") == "1"
LLM_AUTOSTART = os.environ.get("CUADRA_LLM_AUTOSTART", "1") == "1"
LLM_BIN = Path(
    os.environ.get("CUADRA_LLM_BIN", BASE_DIR / "tools" / "llamacpp" / "llama-server.exe")
)
LLM_MODELS_DIR = Path(os.environ.get("CUADRA_LLM_MODELS_DIR", BASE_DIR / "models"))
LLM_MODEL_PATH = os.environ.get("CUADRA_LLM_MODEL_PATH", "")
LLM_HOST = os.environ.get("CUADRA_LLM_HOST", "127.0.0.1")
LLM_PORT = int(os.environ.get("CUADRA_LLM_PORT", "8080"))
LLM_GPU_LAYERS = int(os.environ.get("CUADRA_LLM_GPU_LAYERS", "99"))
LLM_CTX = int(os.environ.get("CUADRA_LLM_CTX", "8192"))
LLM_EXTRA_ARGS = os.environ.get("CUADRA_LLM_EXTRA_ARGS", "")
LLM_PID_FILE = DATA_DIR / "llm_server.pid"
LLM_LOG_FILE = LOGS_DIR / "llm.log"

# Prompt budget (doc 01 §5): derived from the context window and validated
# against the explicit cap. Generation and safety margins are configurable.
LLM_MAX_PROMPT_TOKENS = int(os.environ.get("CUADRA_LLM_MAX_PROMPT_TOKENS", "2500"))
LLM_MAX_OUTPUT_TOKENS = int(os.environ.get("CUADRA_LLM_MAX_OUTPUT_TOKENS", "600"))
LLM_CONTEXT_SAFETY_TOKENS = int(
    os.environ.get("CUADRA_LLM_CONTEXT_SAFETY_TOKENS", "400")
)
HISTORY_WINDOW = int(os.environ.get("CUADRA_HISTORY_WINDOW", "12"))
SUMMARY_EVERY = int(os.environ.get("CUADRA_SUMMARY_EVERY", "10"))

# Memory/retrieval (doc 01 §3-4). Embeddings are optional: when `fastembed` or
# the vendored model is missing, retrieval degrades to FTS5 + fuzzy and the
# service still boots. The model is never downloaded at startup.
EMBEDDING_MODEL = os.environ.get(
    "CUADRA_EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)
EMBEDDING_CACHE_DIR = Path(
    os.environ.get("CUADRA_FASTEMBED_DIR", BASE_DIR / "models" / "fastembed")
)
EMBEDDING_THREADS = int(os.environ.get("CUADRA_EMBEDDING_THREADS", "4"))
RETRIEVAL_CANDIDATES = int(os.environ.get("CUADRA_RETRIEVAL_CANDIDATES", "20"))
RETRIEVAL_TOP_K = int(os.environ.get("CUADRA_RETRIEVAL_TOP_K", "4"))
RRF_K = int(os.environ.get("CUADRA_RRF_K", "60"))

# Conversations without messages for this many hours are closed by the daily
# scheduler; a returning customer gets a fresh conversation (profile survives).
CONVERSATION_IDLE_HOURS = float(
    os.environ.get("CUADRA_CONVERSATION_IDLE_HOURS", "72")
)

# Turn assembly: one reply per burst. The debounce is re-armed by every new
# message; max_wait prevents an endless wait while the customer keeps typing.
TURN_DEBOUNCE_S = float(os.environ.get("CUADRA_TURN_DEBOUNCE_S", "2.5"))
TURN_MAX_WAIT_S = float(os.environ.get("CUADRA_TURN_MAX_WAIT_S", "8"))

# Whisper (speech-to-text). Empty language = auto-detect ("es" in production).
WHISPER_MODEL = os.environ.get("CUADRA_WHISPER_MODEL", "small")
WHISPER_THREADS = int(os.environ.get("CUADRA_WHISPER_THREADS", "4"))
WHISPER_LANGUAGE = os.environ.get("CUADRA_WHISPER_LANGUAGE", "")

# Job queue backoff / priorities (lower number runs first)
QUEUE_BACKOFF_BASE_S = float(os.environ.get("CUADRA_QUEUE_BACKOFF_BASE_S", "5"))
QUEUE_BACKOFF_MAX_S = float(os.environ.get("CUADRA_QUEUE_BACKOFF_MAX_S", "300"))
# Rate limits are not ordinary failures: park the job for the time Telegram
# asks for, do not consume attempts, and give up only after repeated hits.
QUEUE_RATE_LIMIT_MAX_WAIT_S = float(
    os.environ.get("CUADRA_QUEUE_RATE_LIMIT_MAX_WAIT_S", "86400")
)
QUEUE_RATE_LIMIT_MAX_HITS = int(os.environ.get("CUADRA_QUEUE_RATE_LIMIT_MAX_HITS", "20"))
QUEUE_RATE_LIMIT_JITTER_S = float(
    os.environ.get("CUADRA_QUEUE_RATE_LIMIT_JITTER_S", "30")
)
PRIORITY_CUSTOMER = 10
PRIORITY_TCP = 50
PRIORITY_BROADCAST = 100
PRIORITY_MAINTENANCE = 200

# Broadcast scheduler (Hito 7): how often due campaigns are dispatched.
BROADCAST_TICK_S = int(os.environ.get("CUADRA_BROADCAST_TICK_S", "5"))

# Persona / human-like delays (seconds)
TYPING_DELAY_MIN = float(os.environ.get("CUADRA_TYPING_DELAY_MIN", "1"))
TYPING_DELAY_MAX = float(os.environ.get("CUADRA_TYPING_DELAY_MAX", "4"))

# Simulated business hours (America/Havana). Outside them the bot answers slower
# (as a human would) — never with an "I'm offline" excuse (doc 03 §Persona).
BUSINESS_START_HOUR = int(os.environ.get("CUADRA_BUSINESS_START_HOUR", "8"))
BUSINESS_END_HOUR = int(os.environ.get("CUADRA_BUSINESS_END_HOUR", "21"))
OUT_OF_HOURS_DELAY_MIN = float(os.environ.get("CUADRA_OUT_OF_HOURS_DELAY_MIN", "5"))
OUT_OF_HOURS_DELAY_MAX = float(os.environ.get("CUADRA_OUT_OF_HOURS_DELAY_MAX", "15"))

# TCP escalation
ESCALATION_TIMEOUT_MIN = int(os.environ.get("CUADRA_ESCALATION_TIMEOUT_MIN", "30"))
ESCALATION_MAX_RETRIES = int(os.environ.get("CUADRA_ESCALATION_MAX_RETRIES", "1"))


def ensure_dirs() -> None:
    for d in (
        DATA_DIR,
        PHOTOS_DIR,
        AUDIOS_DIR,
        IMAGES_DIR,
        LOGS_DIR,
        LLM_MODELS_DIR,
        EMBEDDING_CACHE_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)

