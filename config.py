"""
config.py - ONE place for all settings.

Every other file imports settings from here, so if we change a model name
or a key, we change it in the .env file only. No code changes needed.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Windows terminals can't print Hindi/Kannada by default. Force UTF-8.
sys.stdout.reconfigure(encoding="utf-8")

# Folder where this project lives (works on any computer).
BASE_DIR = Path(__file__).resolve().parent

# Read the .env file and put its values into environment variables.
load_dotenv(BASE_DIR / ".env")

# --- API keys (secret, never written in code) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

# --- Which LLM to try first ("gemini" or "groq") ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").strip().lower()

# --- Model names (comma-separated = backups, tried in order) ---
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite,gemini-3.8-flash,gemini-flash-latest")
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b,openai/gpt-oss-120b,openai/gpt-oss-20b")
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")

# --- Folders ---
KNOWLEDGE_DIR = BASE_DIR / "knowledge_base"   # documents the agent can read (RAG)
DATA_DIR = BASE_DIR / "data"                  # database + search index
DATA_DIR.mkdir(exist_ok=True)

# Confidential documents: only the admin page can search them, the voice agent never sees them.
# Kept OUTSIDE knowledge_base/ and outside frontend/, and git-ignored.
INTERNAL_KNOWLEDGE_DIR = BASE_DIR / "knowledge_internal"
INTERNAL_KNOWLEDGE_DIR.mkdir(exist_ok=True)

# --- Voice OUT: Gemini TTS for these languages (smoother, more natural accent), edge-tts for the rest ---
GEMINI_TTS_LANGS = {x.strip() for x in os.getenv("GEMINI_TTS_LANGS", "te").split(",") if x.strip()}
# Free tier = 3 requests/minute PER MODEL, so several models are rotated (comma-separated, in order).
GEMINI_TTS_MODELS = os.getenv("GEMINI_TTS_MODELS", "gemini-3.8-flash-lite-tts,gemini-3.8-flash-tts,"
                              "gemini-3.1-flash-tts-preview,gemini-2.5-flash-preview-tts")
GEMINI_TTS_VOICE = os.getenv("GEMINI_TTS_VOICE", "Sulafat").strip()   # Sulafat = warm; also Aoede, Leda

# --- Voice IN: server speech-to-text. "auto" = Gemini for Telugu/Kannada/Hinglish, Groq Whisper otherwise ---
STT_PROVIDER = os.getenv("STT_PROVIDER", "auto").strip().lower()
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3").strip()
GEMINI_STT_MODEL = os.getenv("GEMINI_STT_MODEL", "gemini-flash-lite-latest,gemini-3.5-flash-lite,gemini-3.5-flash")
MAX_AUDIO_MB = 4     # one spoken turn; 16 kHz WAV = ~2 minutes

# --- Admin login on the main page (empty = admin password sign-in disabled) ---
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

# --- Largest document the admin page accepts (MB). PDFs are stored as their extracted text. ---
MAX_UPLOAD_MB = max(1, int(os.getenv("MAX_UPLOAD_MB", "50")))

# ===========================================================================
# The 7-layer stack (see layers/__init__.py). Every setting has a safe default.
# ===========================================================================
def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --- Databases (tests and evals point these at throw-away files) ---
CRM_DB_PATH = Path(os.getenv("CRM_DB_PATH") or DATA_DIR / "crm.db")
OBS_DB_PATH = Path(os.getenv("OBS_DB_PATH") or DATA_DIR / "observability.db")   # traces + audit log + users

# --- Layer: router. Simple turns go to the FAST models, tool/booking turns to the main list above ---
ROUTER_ENABLED = _flag("ROUTER_ENABLED", True)
GROQ_FAST_MODEL = os.getenv("GROQ_FAST_MODEL", "openai/gpt-oss-20b")
GEMINI_FAST_MODEL = os.getenv("GEMINI_FAST_MODEL", "gemini-3.5-flash-lite")

# --- Layer: cache. Same FAQ question again -> answer from memory, no LLM call ---
CACHE_ENABLED = _flag("CACHE_ENABLED", True)
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))

# --- Layer: guardrails ---
GUARDRAILS_ENABLED = _flag("GUARDRAILS_ENABLED", True)
MAX_TURNS_PER_MINUTE = int(os.getenv("MAX_TURNS_PER_MINUTE", "20"))     # per call
MAX_CALLS_PER_HOUR_PER_IP = int(os.getenv("MAX_CALLS_PER_HOUR_PER_IP", "30"))

# --- Layer: RBAC + OAuth (Google sign-in for the admin page) ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").strip().rstrip("/")
# Emails that become "admin" the first time they sign in with Google (bootstrap the first user).
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}

# --- Layer: MCP tool server (mcp_server.py) ---
MCP_ROLE = os.getenv("MCP_ROLE", "sales_rep").strip()   # which tools an MCP client may use

# --- Layer: observability. Optional prices to show cost: "model=input/output" in USD per 1M tokens ---
MODEL_PRICES = {}
for _item in os.getenv("MODEL_PRICES", "").split(","):
    if "=" in _item and "/" in _item:
        _m, _p = _item.rsplit("=", 1)
        try:
            MODEL_PRICES[_m.strip()] = tuple(float(x) for x in _p.split("/"))
        except ValueError:
            pass
