"""
layers/prompts.py - LAYER 2: the system prompt.

The prompt lives in prompts/aria_system.md, not inside the Python code, so:
  - it can be edited and reviewed like a document,
  - every change gets a new VERSION (a short hash of the text), and
  - every trace and every eval result records which version produced it.
    "The score dropped after prompt 3f2a91c0" is then a one-line answer.
"""
import hashlib
from pathlib import Path

import config

PROMPT_FILE = config.BASE_DIR / "prompts" / "aria_system.md"

_cache: dict = {"mtime": None, "text": "", "version": ""}


def _load() -> tuple[str, str]:
    """Read the prompt file (again only if it changed on disk) -> (text, version)."""
    mtime = PROMPT_FILE.stat().st_mtime
    if _cache["mtime"] != mtime:
        text = PROMPT_FILE.read_text(encoding="utf-8")
        _cache.update(mtime=mtime, text=text, version="aria-" + hashlib.sha256(text.encode()).hexdigest()[:8])
    return _cache["text"], _cache["version"]


def version() -> str:
    return _load()[1]


def render(**values) -> tuple[str, str]:
    """Fill in the {placeholders} -> (system prompt, prompt version)."""
    text, ver = _load()
    return text.format(**values), ver


if __name__ == "__main__":
    print(version(), Path(PROMPT_FILE).name)
