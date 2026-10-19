"""Runtime configuration for cuadra-core.

All values have operational defaults and can be overridden via environment
variables. Versions: always latest stable of each dependency (no pins).
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
DATA_DIR = Path(os.environ.get("CUADRA_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "cuadra.db"
PHOTOS_DIR = DATA_DIR / "fotos"
AUDIOS_DIR = DATA_DIR / "audios"
IMAGES_DIR = DATA_DIR / "images"
LOGS_DIR = DATA_DIR / "logs"
LOG_FILE = LOGS_DIR / "cuadra.log"
MIGRATIONS_DIR = BASE_DIR / "migrations"

# Dashboard: shared PIN on the LAN. Empty = disabled (development only).
PIN = os.environ.get("CUADRA_PIN", "0000")

# Development mode: enables debug endpoints (POST /api/debug/mensaje, etc.)
DEBUG = os.environ.get("CUADRA_DEBUG", "1") == "1"

# Service port (dashboard API + static files + Phase 2 webhooks)
PORT = int(os.environ.get("CUADRA_PORT", "8321"))

# Telegram. Empty token => dummy mode (development without a real bot).
TELEGRAM_TOKEN = os.environ.get("CUADRA_TELEGRAM_TOKEN", "")

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

# Prompt budget (doc 01 §6.1): ~2-2.5K tokens per call
LLM_MAX_PROMPT_TOKENS = int(os.environ.get("CUADRA_LLM_MAX_PROMPT_TOKENS", "2500"))
HISTORY_WINDOW = int(os.environ.get("CUADRA_HISTORY_WINDOW", "12"))
SUMMARY_EVERY = int(os.environ.get("CUADRA_SUMMARY_EVERY", "10"))

# Whisper (speech-to-text). Empty language = auto-detect ("es" in production).
WHISPER_MODEL = os.environ.get("CUADRA_WHISPER_MODEL", "small")
WHISPER_THREADS = int(os.environ.get("CUADRA_WHISPER_THREADS", "4"))
WHISPER_LANGUAGE = os.environ.get("CUADRA_WHISPER_LANGUAGE", "")

# Job queue backoff / priorities (lower number runs first)
QUEUE_BACKOFF_BASE_S = float(os.environ.get("CUADRA_QUEUE_BACKOFF_BASE_S", "5"))
QUEUE_BACKOFF_MAX_S = float(os.environ.get("CUADRA_QUEUE_BACKOFF_MAX_S", "300"))
PRIORITY_CUSTOMER = 10
PRIORITY_TCP = 50
PRIORITY_BROADCAST = 100

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
    for d in (DATA_DIR, PHOTOS_DIR, AUDIOS_DIR, IMAGES_DIR, LOGS_DIR, LLM_MODELS_DIR):
        d.mkdir(parents=True, exist_ok=True)
