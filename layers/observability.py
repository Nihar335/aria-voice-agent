"""
layers/observability.py - LAYER 8 (part 1): see what the AI actually did.

Every customer turn produces ONE trace, saved in data/observability.db:

    trace = who (session, lead) + what (input, reply) + how (route, model, prompt version,
            cache hit, tools, guardrail hits) + cost (tokens, USD) + time (one span per layer)

With traces you can answer questions like:
    "Why did Aria say that?"          -> open the trace: facts used, tools, prompt version
    "Is the router saving money?"     -> % of turns on the fast model, tokens per turn
    "Did the new prompt make it slower?" -> p95 latency per prompt version

The same database also keeps the AUDIT LOG (who logged in, who uploaded/deleted what,
which MCP tool was called by whom). The audit log is append-only: nothing in the app deletes from it.
"""
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime

import config

_lock = threading.RLock()   # re-entrant: save()/audit() hold it while db() may open the connection
_db = None


def db() -> sqlite3.Connection:
    """One shared connection (SQLite + a lock is plenty for one server; Postgres in production)."""
    global _db
    if _db is None:
        with _lock:
            if _db is None:
                conn = sqlite3.connect(config.OBS_DB_PATH, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS traces (
                        id TEXT PRIMARY KEY, created_at TEXT, session_id TEXT, lead_id INTEGER,
                        kind TEXT, route TEXT, model TEXT, prompt_version TEXT,
                        cache_hit INTEGER, guard_flags TEXT, total_ms INTEGER,
                        input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
                        error TEXT, data TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_traces_time ON traces(created_at);
                    CREATE TABLE IF NOT EXISTS audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT,
                        actor TEXT, role TEXT, action TEXT, detail TEXT
                    );
                """)
                conn.commit()
                _db = conn
    return _db


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def cost_usd(provider: str, input_tokens: int, output_tokens: int) -> float:
    """USD for one call, if a price is set in MODEL_PRICES (.env). Unknown price -> 0."""
    model = provider.split("/", 1)[-1]          # "groq/openai/gpt-oss-20b" -> "openai/gpt-oss-20b"
    price = config.MODEL_PRICES.get(model) or config.MODEL_PRICES.get(provider)
    if not price:
        return 0.0
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 6)


class Trace:
    """Collects everything about one turn. Call save() at the end (it never raises)."""

    def __init__(self, session_id: str = "", lead_id: int | None = None, kind: str = "turn"):
        self.id = uuid.uuid4().hex[:16]
        self.t0 = time.perf_counter()
        self.data = {
            "id": self.id, "session_id": session_id, "lead_id": lead_id, "kind": kind,
            "created_at": _now(), "spans": [], "llm_calls": [], "tools": [], "guard": [],
            "route": None, "cache_hit": False, "prompt_version": None,
            "input": "", "output": "", "sources": [], "error": None,
        }

    @contextmanager
    def span(self, name: str, **info):
        """Time one layer:   with trace.span("rag"): ..."""
        start = time.perf_counter()
        record = {"name": name, **info}
        try:
            yield record
        finally:
            record["ms"] = int((time.perf_counter() - start) * 1000)
            self.data["spans"].append(record)

    def llm(self, result, purpose: str = "reply", parsed: dict | None = None):
        """Record one LLM call (an llm.LLMResult). `parsed` = the JSON decision, kept for debugging."""
        self.data["llm_calls"].append({
            "raw": result.text[:600], "parsed_keys": sorted(parsed) if isinstance(parsed, dict) else None,
            "purpose": purpose, "provider": result.provider, "tier": result.tier, "ms": result.latency_ms,
            "in": result.input_tokens, "out": result.output_tokens, "failed": list(result.failed),
            "cost_usd": cost_usd(result.provider, result.input_tokens, result.output_tokens),
        })

    def guard(self, stage: str, rule: str, action: str, detail: str = ""):
        """Record a guardrail hit, e.g. ("output", "unsupported_price", "regenerated")."""
        self.data["guard"].append({"stage": stage, "rule": rule, "action": action, "detail": detail[:200]})

    def set(self, **fields):
        self.data.update(fields)

    def save(self) -> dict:
        d = self.data
        d["total_ms"] = int((time.perf_counter() - self.t0) * 1000)
        calls = d["llm_calls"]
        d["input_tokens"] = sum(c["in"] for c in calls)
        d["output_tokens"] = sum(c["out"] for c in calls)
        d["cost_usd"] = round(sum(c["cost_usd"] for c in calls), 6)
        d["model"] = calls[-1]["provider"] if calls else ("cache" if d["cache_hit"] else
                                                          "guardrail" if d["route"] == "guarded" else None)
        try:
            with _lock:
                db().execute(
                    "INSERT INTO traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (d["id"], d["created_at"], d["session_id"], d["lead_id"], d["kind"], d["route"],
                     d["model"], d["prompt_version"], int(d["cache_hit"]),
                     ",".join(sorted({g["rule"] for g in d["guard"]})), d["total_ms"],
                     d["input_tokens"], d["output_tokens"], d["cost_usd"], d["error"],
                     json.dumps(d, ensure_ascii=False, default=str)),
                )
                db().commit()
        except Exception as e:          # observability must never break a live call
            print(f"[obs] could not save trace: {e}")
        # One structured log line per turn (ship these to Grafana/Datadog/etc. in production).
        print("[trace]", json.dumps({k: d[k] for k in ("id", "route", "model", "cache_hit", "total_ms",
                                                       "input_tokens", "output_tokens")}
                                    | {"guard": [g["rule"] for g in d["guard"]]}, default=str))
        return d


# ---------------------------------------------------------------------------
# Audit log (append-only)
# ---------------------------------------------------------------------------
def audit(actor: str, role: str, action: str, detail: str = ""):
    try:
        with _lock:
            db().execute("INSERT INTO audit (created_at, actor, role, action, detail) VALUES (?,?,?,?,?)",
                         (_now(), actor, role, action, detail[:500]))
            db().commit()
    except Exception as e:
        print(f"[obs] could not write audit log: {e}")


def list_audit(limit: int = 100) -> list[dict]:
    rows = db().execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Reading traces back (admin "Quality" tab)
# ---------------------------------------------------------------------------
def list_traces(limit: int = 50, session_id: str | None = None) -> list[dict]:
    sql, args = "SELECT data FROM traces", []
    if session_id:
        sql, args = sql + " WHERE session_id = ?", [session_id]
    rows = db().execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, limit)).fetchall()
    return [json.loads(r["data"]) for r in rows]


def get_trace(trace_id: str) -> dict | None:
    row = db().execute("SELECT data FROM traces WHERE id = ?", (trace_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def _pct(values: list[int], p: float) -> int:
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p * (len(values) - 1))))]


def stats(last: int = 500) -> dict:
    """Summary of the last N turns: speed, cost, routing, cache, guardrails, errors."""
    rows = [dict(r) for r in db().execute(
        "SELECT route, model, prompt_version, cache_hit, guard_flags, total_ms, input_tokens, output_tokens, "
        "cost_usd, error FROM traces WHERE kind = 'turn' ORDER BY created_at DESC LIMIT ?", (last,))]
    n = len(rows)
    count = lambda key: {k: sum(1 for r in rows if (r[key] or "none") == k) for k in {r[key] or "none" for r in rows}}
    guard_hits: dict[str, int] = {}
    for r in rows:
        for rule in filter(None, (r["guard_flags"] or "").split(",")):
            guard_hits[rule] = guard_hits.get(rule, 0) + 1
    ms = [r["total_ms"] for r in rows]
    return {
        "turns": n,
        "p50_ms": _pct(ms, 0.5), "p95_ms": _pct(ms, 0.95),
        "cache_hit_rate": round(sum(r["cache_hit"] for r in rows) / n, 3) if n else 0,
        "avg_tokens": round(sum(r["input_tokens"] + r["output_tokens"] for r in rows) / n) if n else 0,
        "total_cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4),
        "errors": sum(1 for r in rows if r["error"]),
        "routes": count("route"), "models": count("model"), "prompt_versions": count("prompt_version"),
        "guard_hits": guard_hits,
    }
