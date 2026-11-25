"""
crm.py - where leads, calls and appointments are saved.

ADAPTER PATTERN:
  The agent never talks to a database directly. It talks to a "CRM adapter"
  with simple methods like upsert_lead() and book_appointment().
  Today the adapter is SQLiteCRM (a small database file on this computer).
  To plug into Fortivex CRM, Zoho or HubSpot, we write ONE new class with the
  same methods and change ONE line at the bottom (get_crm). Nothing else changes.
"""
import json
import sqlite3
from datetime import datetime

import config

DB_FILE = config.DATA_DIR / "crm.db"


# ---------------------------------------------------------------------------
# The "contract": every CRM adapter must have these methods.
# ---------------------------------------------------------------------------
class CRMAdapter:
    def find_lead_by_phone(self, phone: str) -> dict | None: raise NotImplementedError
    def get_lead(self, lead_id: int) -> dict | None: raise NotImplementedError
    def create_lead(self, phone: str) -> dict: raise NotImplementedError
    def update_lead(self, lead_id: int, fields: dict) -> dict: raise NotImplementedError
    def list_leads(self) -> list[dict]: raise NotImplementedError
    def save_call(self, lead_id: int, transcript: list, summary: dict) -> int: raise NotImplementedError
    def last_call_summary(self, lead_id: int) -> dict | None: raise NotImplementedError
    def list_calls(self) -> list[dict]: raise NotImplementedError
    def book_appointment(self, lead_id: int, start: str, topic: str, link: str) -> dict: raise NotImplementedError
    def booked_times(self) -> set[str]: raise NotImplementedError
    def list_appointments(self) -> list[dict]: raise NotImplementedError


# Columns a lead has. The agent and the analysis step fill these in during the call.
LEAD_FIELDS = [
    "name", "email", "company", "language", "need",
    "budget", "authority", "timeline",          # BANT (Stage 4)
    "score", "status", "sentiment",
]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Adapter 1: SQLite (built in, zero setup, perfect for the MVP)
# ---------------------------------------------------------------------------
class SQLiteCRM(CRMAdapter):
    def __init__(self, path=DB_FILE):
        # check_same_thread=False lets the web server use it from different threads.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row      # rows behave like dicts
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT UNIQUE,
                name TEXT, email TEXT, company TEXT, language TEXT, need TEXT,
                budget TEXT, authority TEXT, timeline TEXT,
                score INTEGER DEFAULT 0, status TEXT DEFAULT 'new', sentiment TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER, transcript TEXT, summary TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER, start TEXT, topic TEXT, link TEXT, created_at TEXT
            );
        """)
        self.db.commit()

    # --- leads ---
    def find_lead_by_phone(self, phone):
        row = self.db.execute("SELECT * FROM leads WHERE phone = ?", (phone,)).fetchone()
        return dict(row) if row else None

    def get_lead(self, lead_id):
        row = self.db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(row) if row else None

    def create_lead(self, phone):
        self.db.execute(
            "INSERT INTO leads (phone, created_at, updated_at) VALUES (?, ?, ?)",
            (phone, _now(), _now()),
        )
        self.db.commit()
        return self.find_lead_by_phone(phone)

    def update_lead(self, lead_id, fields):
        # Only allow known columns (protects against bad data from the LLM).
        # The LLM sometimes writes the WORD "null" or "unknown" instead of leaving a field empty.
        empty = (None, "", "null", "None", "none", "unknown", "n/a")
        clean = {k: v for k, v in fields.items() if k in LEAD_FIELDS and v not in empty}
        if clean:
            # "?" placeholders = values are never pasted into SQL text (no SQL injection).
            sets = ", ".join(f"{k} = ?" for k in clean)
            self.db.execute(
                f"UPDATE leads SET {sets}, updated_at = ? WHERE id = ?",
                (*clean.values(), _now(), lead_id),
            )
            self.db.commit()
        return self.get_lead(lead_id)

    def list_leads(self):
        rows = self.db.execute("SELECT * FROM leads ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]

    # --- calls ---
    def save_call(self, lead_id, transcript, summary):
        cur = self.db.execute(
            "INSERT INTO calls (lead_id, transcript, summary, created_at) VALUES (?, ?, ?, ?)",
            (lead_id, json.dumps(transcript, ensure_ascii=False),
             json.dumps(summary, ensure_ascii=False), _now()),
        )
        self.db.commit()
        return cur.lastrowid

    def last_call_summary(self, lead_id):
        row = self.db.execute(
            "SELECT summary, created_at FROM calls WHERE lead_id = ? ORDER BY id DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
        if not row:
            return None
        return {**json.loads(row["summary"]), "date": row["created_at"]}

    def list_calls(self):
        rows = self.db.execute("""
            SELECT calls.*, leads.name, leads.phone FROM calls
            LEFT JOIN leads ON leads.id = calls.lead_id ORDER BY calls.id DESC
        """).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["transcript"] = json.loads(d["transcript"])
            d["summary"] = json.loads(d["summary"])
            out.append(d)
        return out

    # --- appointments ---
    def book_appointment(self, lead_id, start, topic, link):
        cur = self.db.execute(
            "INSERT INTO appointments (lead_id, start, topic, link, created_at) VALUES (?, ?, ?, ?, ?)",
            (lead_id, start, topic, link, _now()),
        )
        self.db.commit()
        self.update_lead(lead_id, {"status": "demo_booked"})
        return {"id": cur.lastrowid, "start": start, "topic": topic, "link": link}

    def booked_times(self):
        return {r["start"] for r in self.db.execute("SELECT start FROM appointments")}

    def list_appointments(self):
        rows = self.db.execute("""
            SELECT appointments.*, leads.name, leads.phone FROM appointments
            LEFT JOIN leads ON leads.id = appointments.lead_id ORDER BY start
        """).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Adapter 2 (example): Fortivex CRM over its API.
# Shows the interviewer that switching CRM is one class, not a rewrite.
# ---------------------------------------------------------------------------
class FortivexCRMAdapter(CRMAdapter):
    """
    Example only - the real API endpoints would come from the Fortivex CRM team.
        def update_lead(self, lead_id, fields):
            requests.patch(f"{BASE_URL}/leads/{lead_id}", json=fields,
                           headers={"Authorization": f"Bearer {API_KEY}"})
    Each method maps to one API call. The agent code stays exactly the same.
    """


# ---------------------------------------------------------------------------
# The ONE line that decides which CRM the whole app uses.
# ---------------------------------------------------------------------------
_crm = None


def get_crm() -> CRMAdapter:
    global _crm
    if _crm is None:
        _crm = SQLiteCRM()      # <- change to FortivexCRMAdapter() to go live
    return _crm
