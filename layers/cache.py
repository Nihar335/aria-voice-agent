"""
layers/cache.py - LAYER 7 (part 2): don't pay twice for the same answer.

Many callers ask the same things ("what do you charge for a chatbot?"). For those FAQ turns
(the router decides which ones are safe) the reply is stored and reused:

    exact match     same question after normalising case / punctuation      -> 0 LLM calls
    semantic match  a differently worded question with embedding similarity >= 0.95
                    (only when RAG runs in embedding mode; reuses the vector RAG already made)

What makes a cached answer safe to reuse:
  - the key includes the knowledge-base version and the prompt version, so uploading a new
    price list or editing the prompt automatically invalidates old answers;
  - only answers that used no tools, passed the output guardrails and do not contain the
    customer's name are stored;
  - entries expire after CACHE_TTL_SECONDS (default 24 h).
In memory with an LRU limit for the MVP; Redis in production (same get/put interface).
"""
import hashlib
import re
import threading
import time
from collections import OrderedDict

import numpy as np

import config

MAX_ENTRIES = 500
SEMANTIC_MIN = 0.95

_store: "OrderedDict[str, dict]" = OrderedDict()
_lock = threading.Lock()
_stats = {"hits": 0, "semantic_hits": 0, "misses": 0, "stores": 0}


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def kb_version(kb) -> str:
    """Short hash of the knowledge base content (computed once per loaded index)."""
    v = getattr(kb, "_cache_version", None)
    if v is None:
        h = hashlib.sha256()
        for c in kb.chunks:
            h.update(c["source"].encode()); h.update(c["text"].encode())
        v = h.hexdigest()[:10]
        kb._cache_version = v
    return v


def _scope_key(language: str | None, kb_ver: str, prompt_ver: str) -> str:
    return f"{language or 'auto'}|{kb_ver}|{prompt_ver}"


def _vector(kb, text: str):
    if getattr(kb, "mode", "") != "embeddings":
        return None
    try:
        from rag import _embed_query
        return _embed_query(text)
    except Exception:
        return None


def get(text: str, language: str | None, kb, prompt_ver: str) -> dict | None:
    if not config.CACHE_ENABLED:
        return None
    scope = _scope_key(language, kb_version(kb), prompt_ver)
    key = scope + "|" + normalise(text)
    now = time.time()
    with _lock:
        for k in [k for k, e in _store.items() if e["expires"] < now]:
            del _store[k]
        entry = _store.get(key)
        if entry:
            _store.move_to_end(key)
            _stats["hits"] += 1
            return {**entry["value"], "match": "exact"}
        candidates = [(k, e) for k, e in _store.items() if e["scope"] == scope and e["vec"] is not None]
    if candidates:
        q = _vector(kb, text)
        if q is not None:
            best_key, best = max(((k, float(np.dot(q, e["vec"]))) for k, e in candidates), key=lambda x: x[1])
            if best >= SEMANTIC_MIN:
                with _lock:
                    entry = _store.get(best_key)
                    if entry:
                        _stats["hits"] += 1; _stats["semantic_hits"] += 1
                        return {**entry["value"], "match": f"semantic {best:.3f}"}
    _stats["misses"] += 1
    return None


def put(text: str, language: str | None, kb, prompt_ver: str, value: dict):
    if not config.CACHE_ENABLED:
        return
    scope = _scope_key(language, kb_version(kb), prompt_ver)
    vec = _vector(kb, text)
    with _lock:
        _store[scope + "|" + normalise(text)] = {
            "scope": scope, "vec": vec, "value": value, "expires": time.time() + config.CACHE_TTL_SECONDS}
        _store.move_to_end(scope + "|" + normalise(text))
        while len(_store) > MAX_ENTRIES:
            _store.popitem(last=False)
        _stats["stores"] += 1


def clear():
    with _lock:
        _store.clear()


def stats() -> dict:
    total = _stats["hits"] + _stats["misses"]
    return {**_stats, "entries": len(_store), "hit_rate": round(_stats["hits"] / total, 3) if total else 0}
