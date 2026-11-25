"""
admin.py - the admin API behind /admin.html: upload, list, delete and test-search
knowledge files.

Who sees what (RBAC, layers/rbac.py):
  client page (index.html) -> talks to the voice agent, which reads ONLY the public
                              knowledge base. No /api/admin/... call works without a token.
  admin page  (admin.html) -> staff sign in with Google (or the break-glass ADMIN_PASSWORD).
                              Each route checks a permission: sales reps can't open confidential
                              files, only managers/admins can upload or delete, only admins manage users.
  Every change is written to the audit log (layers/observability.py).

Confidential files live in knowledge_internal/, which is not inside frontend/, so the
web server never serves them as files, and the agent never loads them into its prompt.
"""
import base64
import io
import json
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

import config
from layers import cache
from layers.observability import audit, get_trace, list_audit, list_traces, stats
from layers.rbac import can, require
from rag import SCOPES, get_kb, list_documents, load_chunks, reload_kb

router = APIRouter(prefix="/api/admin")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _check_scope(user: dict, scope: str):
    """RBAC: confidential documents need the search:internal permission (manager / admin)."""
    if scope == "internal" and not can(user["role"], "search:internal"):
        raise HTTPException(403, f"Your role ({user['role']}) can't open confidential documents")


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
def _scope(scope: str) -> str:
    if scope not in SCOPES:
        raise HTTPException(400, "scope must be 'public' or 'internal'")
    return scope


@router.get("/documents")
def documents(user: dict = Depends(require("docs:read"))):
    out = {}
    for scope in SCOPES:
        if scope == "internal" and not can(user["role"], "search:internal"):
            out[scope] = []
            continue
        counts = {}
        for c in get_kb(scope).chunks:
            name = c["source"].split(" > ")[0]
            counts[name] = counts.get(name, 0) + 1
        out[scope] = [{"name": p.name, "size": p.stat().st_size, "chunks": counts.get(p.name, 0),
                       "modified": int(p.stat().st_mtime)} for p in list_documents(scope)]
    return {"documents": out, "search_mode": {s: get_kb(s).mode for s in SCOPES}}


class Upload(BaseModel):
    filename: str = Field(..., min_length=1, max_length=200)
    content_b64: str = Field(..., max_length=MAX_UPLOAD_BYTES * 4 // 3 + 16)
    scope: str = "internal"     # safe default: a forgotten choice never exposes a file to customers


def _safe_name(filename: str) -> tuple[str, str]:
    """Strip folders and odd characters, so a name like '../../main.py' can't escape the folder."""
    stem, _, ext = filename.replace("\\", "/").split("/")[-1].rpartition(".")
    ext = ext.lower()
    if ext not in ("md", "txt", "pdf"):
        raise HTTPException(400, "Only .md, .txt and .pdf files are supported")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:80] or "document"
    return stem, ext


def _to_text(raw: bytes, ext: str) -> str:
    if ext == "pdf":
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(raw))
            text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as e:
            raise HTTPException(400, f"Could not read this PDF: {e}")
        if not text.strip():
            raise HTTPException(400, "This PDF has no text layer (it is probably a scan). OCR it first.")
        return text
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


@router.post("/documents")
async def upload(body: Upload, user: dict = Depends(require("docs:write"))):
    scope = _scope(body.scope)
    stem, ext = _safe_name(body.filename)
    try:
        raw = base64.b64decode(body.content_b64, validate=True)
    except ValueError:
        raise HTTPException(400, "File content is not valid base64")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 10 MB")
    text = (await run_in_threadpool(_to_text, raw, ext)).strip()
    if not text:
        raise HTTPException(400, "File is empty")

    # PDFs are stored as their extracted text; the search only needs text.
    target = SCOPES[scope] / f"{stem}.{'txt' if ext == 'pdf' else ext}"
    # The same name in the OTHER scope would be confusing ("which one does the agent read?").
    other = [s for s in SCOPES if s != scope and (SCOPES[s] / target.name).exists()]
    if other:
        raise HTTPException(409, f"'{target.name}' already exists in {other[0]}. Delete it there first.")
    replaced = target.exists()
    target.write_text(text, encoding="utf-8")
    kb = await run_in_threadpool(reload_kb, scope)
    audit(user["email"], user["role"], "doc_upload", f"{scope}/{target.name} replaced={replaced}")
    chunks = sum(1 for c in kb.chunks if c["source"].startswith(target.name + " > "))
    return {"name": target.name, "scope": scope, "chunks": chunks, "replaced": replaced, "search_mode": kb.mode}


@router.delete("/documents/{scope}/{name}")
async def delete(scope: str, name: str, user: dict = Depends(require("docs:write"))):
    scope = _scope(scope)
    # Only delete a file that is actually in the list (no path tricks possible).
    match = next((p for p in list_documents(scope) if p.name == name), None)
    if not match:
        raise HTTPException(404, "Document not found")
    match.unlink()
    await run_in_threadpool(reload_kb, scope)
    audit(user["email"], user["role"], "doc_delete", f"{scope}/{name}")
    return {"deleted": name, "scope": scope}


@router.get("/documents/{scope}/{name}")
def preview(scope: str, name: str, user: dict = Depends(require("docs:read"))):
    scope = _scope(scope)
    _check_scope(user, scope)
    match = next((p for p in list_documents(scope) if p.name == name), None)
    if not match:
        raise HTTPException(404, "Document not found")
    return {"name": name, "scope": scope, "text": match.read_text(encoding="utf-8", errors="replace")[:20000],
            "chunks": [c for c in load_chunks(scope) if c["source"].startswith(name + " > ")][:50]}


class Search(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    scope: str = "public"
    k: int = Field(5, ge=1, le=10)


@router.post("/search")
async def search(body: Search, user: dict = Depends(require("search:public"))):
    """Test what the agent would find (public) or look things up in confidential docs (internal)."""
    scope = _scope(body.scope)
    _check_scope(user, scope)
    hits = await run_in_threadpool(get_kb(scope).search, body.query, body.k)
    return {"scope": scope, "mode": get_kb(scope).mode, "hits": hits}


# ---------------------------------------------------------------------------
# Customer links (to send a customer their private "my calls" page)
# ---------------------------------------------------------------------------
class PortalLink(BaseModel):
    reset: bool = False     # True = make a new link; the old one stops working


@router.post("/leads/{lead_id}/portal-link")
def portal_link(lead_id: int, body: PortalLink, user: dict = Depends(require("leads:link"))):
    from crm import get_crm
    try:
        token = get_crm().portal_token(lead_id, reset=body.reset)
    except KeyError:
        raise HTTPException(404, "Lead not found")
    audit(user["email"], user["role"], "portal_link_reset" if body.reset else "portal_link", f"lead {lead_id}")
    return {"lead_id": lead_id, "path": f"/my-calls.html#{token}", "reset": body.reset}


# ---------------------------------------------------------------------------
# Quality: observability (traces) + evals + audit log
# ---------------------------------------------------------------------------
EVAL_DIR = config.DATA_DIR / "evals"


def latest_eval() -> dict | None:
    reports = sorted(EVAL_DIR.glob("report_*.json")) if EVAL_DIR.exists() else []
    if not reports:
        return None
    r = json.loads(reports[-1].read_text(encoding="utf-8"))
    history = [json.loads(p.read_text(encoding="utf-8"))["summary"] for p in reports[-10:]]
    return {**r, "history": history}


@router.get("/quality")
def quality(_: dict = Depends(require("quality:read"))):
    return {"traces": stats(), "cache": cache.stats(), "eval": latest_eval()}


@router.get("/traces")
def traces(limit: int = 50, session_id: str | None = None, _: dict = Depends(require("quality:read"))):
    return {"traces": list_traces(min(limit, 200), session_id)}


@router.get("/traces/{trace_id}")
def trace(trace_id: str, _: dict = Depends(require("quality:read"))):
    t = get_trace(trace_id)
    if not t:
        raise HTTPException(404, "Trace not found")
    return t


@router.get("/audit")
def audit_log(limit: int = 100, _: dict = Depends(require("audit:read"))):
    return {"audit": list_audit(min(limit, 500))}
