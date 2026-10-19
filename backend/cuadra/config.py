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

# Ollama
OLLAMA_URL = os.environ.get("CUADRA_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("CUADRA_OLLAMA_MODEL", "qwen2.5:3b-instruct-q4_K_M")

# Whisper (speech-to-text)
WHISPER_MODEL = os.environ.get("CUADRA_WHISPER_MODEL", "small")
WHISPER_THREADS = int(os.environ.get("CUADRA_WHISPER_THREADS", "4"))

# Persona / human-like delays (seconds)
TYPING_DELAY_MIN = float(os.environ.get("CUADRA_TYPING_DELAY_MIN", "1"))
TYPING_DELAY_MAX = float(os.environ.get("CUADRA_TYPING_DELAY_MAX", "4"))

# TCP escalation
ESCALATION_TIMEOUT_MIN = int(os.environ.get("CUADRA_ESCALATION_TIMEOUT_MIN", "30"))
ESCALATION_MAX_RETRIES = int(os.environ.get("CUADRA_ESCALATION_MAX_RETRIES", "1"))


def ensure_dirs() -> None:
    for d in (DATA_DIR, PHOTOS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
