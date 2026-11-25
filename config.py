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
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()

# --- Model names ---
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")

# --- Folders ---
KNOWLEDGE_DIR = BASE_DIR / "knowledge_base"   # documents the agent can read (RAG)
DATA_DIR = BASE_DIR / "data"                  # database + search index
DATA_DIR.mkdir(exist_ok=True)
