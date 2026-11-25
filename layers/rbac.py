"""
layers/rbac.py - LAYER 5: who is allowed to do what (RBAC) + Google sign-in (OAuth).

ROLES -> PERMISSIONS (one table, checked by code on every admin route and every tool call)

    admin      everything, including managing users
    manager    dashboard, upload/delete documents, internal search, quality/traces, customer links
    sales_rep  dashboard, public search, customer links
    viewer     dashboard and quality numbers only (read-only)
    customer   the voice agent's caller: only the 5 call tools, and only for their own lead

HOW STAFF SIGN IN
  1. "Sign in with Google" (OAuth 2.0 authorization code + PKCE). Google proves the email;
     our users table decides the role. Unknown emails are refused unless they are listed in
     ADMIN_EMAILS (that is how the very first admin is created).
  2. ADMIN_PASSWORD from .env still works as a break-glass login with the admin role
     (for local development, or if Google is down). Leave it empty in production.

Every sign-in, refused sign-in and user change is written to the audit log.
"""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

import config
from layers.observability import _lock as _db_lock, audit, db

# ---------------------------------------------------------------------------
# Roles and permissions
# ---------------------------------------------------------------------------
CALL_TOOLS = ["search_knowledge", "save_lead_info", "check_availability", "book_appointment", "handoff_to_human"]
STAFF_TOOLS = ["get_lead", "list_hot_leads"]

ROLES: dict[str, set[str]] = {
    "viewer": {"dashboard:read", "quality:read"},
    "sales_rep": {"dashboard:read", "docs:read", "search:public", "leads:link",
                  *(f"tool:{t}" for t in CALL_TOOLS + STAFF_TOOLS)},
    "manager": {"dashboard:read", "quality:read", "docs:read", "docs:write", "search:public", "search:internal",
                "leads:link", "audit:read", *(f"tool:{t}" for t in CALL_TOOLS + STAFF_TOOLS + ["search_internal"])},
    "customer": {f"tool:{t}" for t in CALL_TOOLS},
}
ROLES["admin"] = set().union(*ROLES.values()) | {"users:manage"}
STAFF_ROLES = ["admin", "manager", "sales_rep", "viewer"]


def can(role: str, permission: str) -> bool:
    return permission in ROLES.get(role, set())


def can_use_tool(role: str, tool: str) -> bool:
    return can(role, f"tool:{tool}")


# ---------------------------------------------------------------------------
# Users (stored next to the audit log)
# ---------------------------------------------------------------------------
def _users_table():
    with _db_lock:
        db().execute("""CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY, name TEXT, role TEXT, active INTEGER DEFAULT 1,
            created_at TEXT, last_login TEXT)""")
        db().commit()


def get_user(email: str) -> dict | None:
    _users_table()
    row = db().execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    _users_table()
    return [dict(r) for r in db().execute("SELECT * FROM users ORDER BY role, email")]


def upsert_user(email: str, role: str, name: str = "", active: bool = True) -> dict:
    if role not in STAFF_ROLES:
        raise ValueError(f"role must be one of {STAFF_ROLES}")
    _users_table()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _db_lock:
        db().execute("""INSERT INTO users (email, name, role, active, created_at) VALUES (?,?,?,?,?)
                        ON CONFLICT(email) DO UPDATE SET role = excluded.role, active = excluded.active,
                        name = COALESCE(NULLIF(excluded.name, ''), users.name)""",
                     (email.lower(), name, role, int(active), now))
        db().commit()
    return get_user(email)


def _touch_login(email: str):
    with _db_lock:
        db().execute("UPDATE users SET last_login = ? WHERE email = ?", (time.strftime("%Y-%m-%dT%H:%M:%S"), email))
        db().commit()


# ---------------------------------------------------------------------------
# Sessions (bearer tokens, in memory: a restart logs everyone out; Redis in production)
# ---------------------------------------------------------------------------
TOKEN_TTL = 8 * 3600
_sessions: dict[str, dict] = {}
_failed: list[float] = []


def _new_session(email: str, role: str, name: str = "", method: str = "") -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"email": email, "role": role, "name": name, "method": method, "exp": time.time() + TOKEN_TTL}
    return token


def current_user(authorization: str = Header("")) -> dict:
    """The signed-in staff member, or 401."""
    token = authorization.removeprefix("Bearer ").strip()
    s = _sessions.get(token)
    if not token or not s or s["exp"] < time.time():
        _sessions.pop(token, None)
        raise HTTPException(401, "Please log in again")
    # Role changes and deactivation apply immediately, not at the next login.
    if s["method"] == "google":
        u = get_user(s["email"])
        if not u or not u["active"]:
            _sessions.pop(token, None)
            raise HTTPException(401, "Your access was removed")
        s["role"] = u["role"]
    return {**s, "token": token}


def require(permission: str):
    """FastAPI dependency:  Depends(require("docs:write"))  -> 401 if not signed in, 403 if not allowed."""
    def check(user: dict = Depends(current_user)) -> dict:
        if not can(user["role"], permission):
            audit(user["email"], user["role"], "denied", permission)
            raise HTTPException(403, f"Your role ({user['role']}) does not allow {permission}")
        return user
    return check


# ---------------------------------------------------------------------------
# HTTP routes: /api/auth/...
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/auth")


def google_enabled() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


@router.get("/config")
def auth_config():
    """What the login page should show."""
    return {"google": google_enabled(), "password": bool(config.ADMIN_PASSWORD)}


class Login(BaseModel):
    password: str = Field(..., max_length=200)


@router.post("/login")
def password_login(body: Login):
    """Break-glass login with ADMIN_PASSWORD -> admin role."""
    if not config.ADMIN_PASSWORD:
        raise HTTPException(503, "Password login is disabled. Use Google sign-in, or set ADMIN_PASSWORD in .env.")
    global _failed
    _failed = [t for t in _failed if t > time.time() - 300]
    if len(_failed) >= 10:
        raise HTTPException(429, "Too many wrong passwords. Try again in 5 minutes.")
    # Strip: ADMIN_PASSWORD is stripped in config, and a pasted trailing space/newline must not fail.
    if not secrets.compare_digest(body.password.strip().encode(), config.ADMIN_PASSWORD.encode()):
        _failed.append(time.time())
        audit("password", "-", "login_failed")
        raise HTTPException(401, "Wrong password")
    audit("password", "admin", "login", "break-glass password login")
    return {"token": _new_session("admin (password)", "admin", "Admin", "password"), "expires_in": TOKEN_TTL}


@router.get("/me")
def me(user: dict = Depends(current_user)):
    perms = sorted(ROLES.get(user["role"], set()))
    return {"email": user["email"], "name": user["name"], "role": user["role"], "method": user["method"],
            "permissions": [p for p in perms if not p.startswith("tool:")],
            "tools": [p[5:] for p in perms if p.startswith("tool:")]}


@router.post("/logout")
def logout(user: dict = Depends(current_user)):
    _sessions.pop(user["token"], None)
    audit(user["email"], user["role"], "logout")
    return {"ok": True}


# --- Google OAuth 2.0 (authorization code flow with PKCE) ---
_pending: dict[str, dict] = {}     # state -> {verifier, expires}
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


def _redirect_uri() -> str:
    return f"{config.PUBLIC_BASE_URL}/api/auth/google/callback"


@router.get("/google/start")
def google_start():
    if not google_enabled():
        raise HTTPException(503, "Google sign-in is not configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET).")
    for s in [s for s, p in _pending.items() if p["expires"] < time.time()]:
        _pending.pop(s, None)
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    _pending[state] = {"verifier": verifier, "expires": time.time() + 600}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"client_id": config.GOOGLE_CLIENT_ID, "redirect_uri": _redirect_uri(), "response_type": "code",
              "scope": "openid email profile", "state": state, "code_challenge": challenge,
              "code_challenge_method": "S256", "prompt": "select_account"}
    return RedirectResponse(f"{GOOGLE_AUTH}?{urlencode(params)}")


def _fail(msg: str) -> RedirectResponse:
    # The fragment (#...) is never sent to servers or logged.
    return RedirectResponse("/admin.html#" + urlencode({"error": msg}))


@router.get("/google/callback")
def google_callback(code: str = "", state: str = "", error: str = ""):
    pending = _pending.pop(state, None)
    if error:
        return _fail(f"Google sign-in cancelled ({error})")
    if not pending or pending["expires"] < time.time() or not code:
        return _fail("Sign-in expired, please try again")
    try:
        r = httpx.post(GOOGLE_TOKEN, timeout=10, data={
            "code": code, "client_id": config.GOOGLE_CLIENT_ID, "client_secret": config.GOOGLE_CLIENT_SECRET,
            "redirect_uri": _redirect_uri(), "grant_type": "authorization_code", "code_verifier": pending["verifier"]})
        r.raise_for_status()
        id_token = r.json()["id_token"]
        # The ID token came straight from Google's token endpoint over TLS (authenticated with our
        # client secret), so Google's docs allow reading its claims without re-checking the signature.
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception as e:
        print(f"[auth] Google token exchange failed: {e}")
        return _fail("Google sign-in failed, please try again")

    if (claims.get("aud") != config.GOOGLE_CLIENT_ID
            or claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com")
            or claims.get("exp", 0) < time.time() or not claims.get("email_verified")):
        return _fail("Google account could not be verified")

    email, name = claims["email"].lower(), claims.get("name", "")
    user = get_user(email)
    if user is None and email in config.ADMIN_EMAILS:
        user = upsert_user(email, "admin", name)
        audit(email, "admin", "user_bootstrapped", "listed in ADMIN_EMAILS")
    if not user or not user["active"]:
        audit(email, "-", "login_refused", "not in users table or deactivated")
        return _fail(f"{email} has no access. Ask an admin to add you.")
    _touch_login(email)
    audit(email, user["role"], "login", "google")
    token = _new_session(email, user["role"], name or user.get("name") or "", "google")
    return RedirectResponse("/admin.html#" + urlencode({"token": token}))


# --- User management (admin only) ---
class UserIn(BaseModel):
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)
    role: str = Field(..., pattern="^(admin|manager|sales_rep|viewer)$")
    name: str = Field("", max_length=100)
    active: bool = True


users_router = APIRouter(prefix="/api/admin/users")


@users_router.get("")
def users(_: dict = Depends(require("users:manage"))):
    return {"users": list_users(), "roles": {r: sorted(p for p in ROLES[r] if not p.startswith("tool:"))
                                              for r in STAFF_ROLES}}


@users_router.post("")
def save_user(body: UserIn, actor: dict = Depends(require("users:manage"))):
    if body.email.lower() == actor["email"] and (body.role != "admin" or not body.active):
        raise HTTPException(400, "You can't remove your own admin access")
    user = upsert_user(body.email, body.role, body.name, body.active)
    audit(actor["email"], actor["role"], "user_saved", f"{user['email']} -> {user['role']} active={user['active']}")
    return user
