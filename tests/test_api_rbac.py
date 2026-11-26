"""
RBAC enforced by the SERVER (not just hidden in the page), checked route by route:
    .venv\\Scripts\\python -m unittest discover tests -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["CRM_DB_PATH"] = str(Path(_tmp) / "crm.db")
os.environ["OBS_DB_PATH"] = str(Path(_tmp) / "obs.db")
os.environ["ADMIN_PASSWORD"] = "unit-test-password"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import warnings                                   # noqa: E402
warnings.simplefilter("ignore")
from fastapi.testclient import TestClient         # noqa: E402

import main                                       # noqa: E402
from layers import rbac                           # noqa: E402

client = TestClient(main.app)


def as_role(role: str) -> dict:
    token = rbac._new_session(f"{role}@test.local", role, role, "test")
    return {"Authorization": f"Bearer {token}"}


class ServerSideRBAC(unittest.TestCase):
    def test_no_token_is_401(self):
        for path in ["/api/admin/dashboard", "/api/admin/quality", "/api/admin/users", "/api/admin/documents"]:
            self.assertEqual(client.get(path).status_code, 401, path)

    def test_matrix(self):
        cases = [  # (method, path, body, {role: expected status})
            ("get", "/api/admin/dashboard", None, {"viewer": 200, "sales_rep": 200, "manager": 200, "admin": 200}),
            ("get", "/api/admin/quality", None, {"viewer": 200, "sales_rep": 403, "manager": 200, "admin": 200}),
            ("get", "/api/admin/users", None, {"viewer": 403, "sales_rep": 403, "manager": 403, "admin": 200}),
            ("get", "/api/admin/audit", None, {"viewer": 403, "sales_rep": 403, "manager": 200, "admin": 200}),
            ("post", "/api/admin/search", {"query": "price", "scope": "internal"},
             {"viewer": 403, "sales_rep": 403, "manager": 200, "admin": 200}),
            ("post", "/api/admin/search", {"query": "price", "scope": "public"},
             {"viewer": 403, "sales_rep": 200, "manager": 200, "admin": 200}),
            ("delete", "/api/admin/documents/public/nope.md", None,
             {"viewer": 403, "sales_rep": 403, "manager": 404, "admin": 404}),
        ]
        for method, path, body, expected in cases:
            for role, status in expected.items():
                kwargs = {"headers": as_role(role)} | ({"json": body} if body else {})
                got = getattr(client, method)(path, **kwargs).status_code
                self.assertEqual(got, status, f"{role} {method.upper()} {path}")

    def test_sales_rep_does_not_see_internal_docs(self):
        r = client.get("/api/admin/documents", headers=as_role("sales_rep")).json()
        self.assertEqual(r["documents"]["internal"], [])

    def test_password_login_strips_and_gives_admin(self):
        r = client.post("/api/auth/login", json={"password": "unit-test-password \n"})
        self.assertEqual(r.status_code, 200)
        me = client.get("/api/auth/me", headers={"Authorization": "Bearer " + r.json()["token"]}).json()
        self.assertEqual(me["role"], "admin")

    def test_deactivated_google_user_loses_access_immediately(self):
        rbac.upsert_user("gone@test.local", "manager")
        token = rbac._new_session("gone@test.local", "manager", "", "google")
        h = {"Authorization": f"Bearer {token}"}
        self.assertEqual(client.get("/api/admin/quality", headers=h).status_code, 200)
        rbac.upsert_user("gone@test.local", "manager", active=False)
        self.assertEqual(client.get("/api/admin/quality", headers=h).status_code, 401)


class UploadSizeLimit(unittest.TestCase):
    """Uploads follow MAX_UPLOAD_MB (default 50 MB). Uses a temp folder and skips re-indexing (no network)."""

    def setUp(self):
        import admin
        import rag
        self.admin, self.rag = admin, rag
        self._scope, self._reload = rag.SCOPES["internal"], admin.reload_kb
        rag.SCOPES["internal"] = Path(tempfile.mkdtemp())
        from types import SimpleNamespace
        admin.reload_kb = lambda scope: SimpleNamespace(chunks=[], mode="keyword")

    def tearDown(self):
        self.rag.SCOPES["internal"], self.admin.reload_kb = self._scope, self._reload

    def upload(self, size_bytes: int):
        import base64
        raw = (b"Pricing notes for the sales team.\n\n" * (size_bytes // 36 + 1))[:size_bytes]
        return client.post("/api/admin/documents", headers=as_role("manager"),
                           json={"filename": "big.txt", "scope": "internal",
                                 "content_b64": base64.b64encode(raw).decode()})

    def test_file_over_old_10mb_limit_is_accepted(self):
        self.assertEqual(self.admin.MAX_UPLOAD_BYTES, main.config.MAX_UPLOAD_MB * 1024 * 1024)
        r = self.upload(12 * 1024 * 1024)
        self.assertEqual(r.status_code, 200, r.text[:200])
        # >= because Windows writes "\n" as "\r\n"
        self.assertGreaterEqual((self.rag.SCOPES["internal"] / "big.txt").stat().st_size, 12 * 1024 * 1024)

    def test_file_over_the_limit_is_refused(self):
        old = self.admin.MAX_UPLOAD_BYTES
        self.admin.MAX_UPLOAD_BYTES = 1024 * 1024
        try:
            r = self.upload(2 * 1024 * 1024)
            self.assertEqual(r.status_code, 413)
            self.assertFalse((self.rag.SCOPES["internal"] / "big.txt").exists())
        finally:
            self.admin.MAX_UPLOAD_BYTES = old


class PhoneNumberIsNotIdentity(unittest.TestCase):
    """Typing someone's phone number on the call page must not reveal their data."""

    def setUp(self):
        async def no_voice(text, language="en"):
            return {"audio": None, "voice": "browser", "tts_ms": 0}
        self._speak, main.tts.speak = main.tts.speak, no_voice
        # No turns are sent and the end-call summary is stubbed, so this test needs no LLM or network.
        self._end, main.end_call = main.end_call, lambda s: {"summary": "test", "call_id": 0}

    def tearDown(self):
        main.tts.speak, main.end_call = self._speak, self._end

    def test_returning_number_gets_no_stored_details(self):
        from crm import get_crm
        crm = get_crm()
        first = client.post("/api/call/start", json={"phone": "9000077777"}).json()
        self.assertFalse(first["returning"])
        lead_id = crm.find_lead_by_phone("9000077777")["id"]
        crm.update_lead(lead_id, {"name": "Ravi", "email": "ravi@clinic.in", "budget": "2 lakh", "score": 80})
        crm.save_call(lead_id, [{"who": "customer", "text": "secret"}], {"summary": "internal notes"})
        end1 = client.post("/api/call/end", json={"session_id": first["session_id"]}).json()
        self.assertTrue(end1["portal_url"])                      # first call: the link is theirs

        r = client.post("/api/call/start", json={"phone": "9000077777"}).json()
        self.assertTrue(r["returning"])
        self.assertNotIn("previous", r)
        self.assertEqual(set(r["lead"]), {"name", "phone"})      # no email, budget, score...
        live = client.get(f"/api/call/{r['session_id']}/live").json()
        self.assertEqual(set(live["lead"]), {"name", "phone"})
        end = client.post("/api/call/end", json={"session_id": r["session_id"]}).json()
        self.assertIsNone(end["portal_url"])                     # no link to every earlier call
        self.assertNotIn("email", end["lead"])


class CustomerCallPage(unittest.TestCase):
    """call.html starts calls in mode "customer": the server must never send internal data to it."""

    def setUp(self):
        async def no_voice(text, language="en"):
            return {"audio": None, "voice": "browser", "tts_ms": 0}
        fake_turn = {"reply": "Hi!", "reply_en": "Hi!", "customer_en": "hello", "language": "en", "handoff": False,
                     "sources": ["02_services_pricing.md > 1"], "thoughts": ["secret reasoning"], "provider": "x",
                     "route": "faq", "trace_id": "t1", "tokens": {"in": 1, "out": 1, "cost_usd": 0},
                     "latency": {"rag_ms": 1, "llm_ms": 1},
                     "events": [{"type": "tool", "detail": "book_appointment", "args": {},
                                 "result": {"booked": True, "slot": "2030-01-02 11:00", "calendar_link": "https://calendar.google.com/x"}}]}
        self._saved = (main.tts.speak, main.handle_turn, main.end_call, main.analyze_turn)
        main.tts.speak = no_voice
        main.handle_turn = lambda session, text, lang=None: dict(fake_turn, latency=dict(fake_turn["latency"]))
        main.analyze_turn = lambda session: {}
        main.end_call = lambda s: {"summary": "INTERNAL", "objections": ["price"], "lead_score": 90,
                                   "next_steps": ["push hard"], "customer_summary": "Thanks!",
                                   "customer_next_steps": ["We'll call you"], "language": "en", "call_id": 0}

    def tearDown(self):
        main.tts.speak, main.handle_turn, main.end_call, main.analyze_turn = self._saved

    def test_customer_mode_gets_only_customer_fields(self):
        start = client.post("/api/call/start", json={"phone": "9000088888", "mode": "customer"}).json()
        self.assertNotIn("lead", start)
        sid = start["session_id"]
        turn = client.post("/api/call/turn", json={"session_id": sid, "text": "hello", "natural_voice": False}).json()
        self.assertEqual(set(turn), set(main.CUSTOMER_TURN_FIELDS))
        self.assertEqual(turn["booking"]["slot"], "2030-01-02 11:00")
        self.assertEqual(client.get(f"/api/call/{sid}/live").status_code, 403)
        end = client.post("/api/call/end", json={"session_id": sid}).json()
        self.assertEqual(set(end["summary"]), {"customer_summary", "customer_next_steps", "language"})
        self.assertNotIn("INTERNAL", str(end))
        self.assertNotIn("push hard", str(end))
        self.assertTrue(end["portal_url"])

    def test_demo_mode_still_gets_the_cockpit_data(self):
        sid = client.post("/api/call/start", json={"phone": "9000099999"}).json()["session_id"]
        turn = client.post("/api/call/turn", json={"session_id": sid, "text": "hello", "natural_voice": False}).json()
        self.assertIn("thoughts", turn)
        self.assertIn("booking", turn)
        self.assertEqual(client.get(f"/api/call/{sid}/live").status_code, 200)

    def test_rate_limit_asks_to_repeat_without_handoff(self):
        sid = client.post("/api/call/start", json={"phone": "9000088855"}).json()["session_id"]
        session = main.SESSIONS[sid]
        before = (len(session.history), len(session.transcript))

        def all_busy(session, text, lang=None):
            session.history.append({"role": "user", "content": text})        # half-finished turn
            session.transcript.append({"who": "customer", "text": text})
            raise main.llm.RateLimited("All LLM providers failed -> groq/x: 429 Rate limit reached")

        main.handle_turn = all_busy
        turn = client.post("/api/call/turn", json={"session_id": sid, "text": "hello", "natural_voice": False}).json()
        self.assertFalse(turn["handoff"])
        self.assertFalse(session.handoff)
        self.assertIn("once more", turn["reply"])
        self.assertEqual((len(session.history), len(session.transcript)), before)   # turn undone, no duplicate
        self.assertNotEqual(main.get_crm().get_lead(session.lead_id).get("status"), "needs_human")

    def test_real_error_still_hands_off(self):
        sid = client.post("/api/call/start", json={"phone": "9000088844"}).json()["session_id"]
        main.handle_turn = lambda session, text, lang=None: (_ for _ in ()).throw(RuntimeError("bad key"))
        turn = client.post("/api/call/turn", json={"session_id": sid, "text": "hello", "natural_voice": False}).json()
        self.assertTrue(turn["handoff"])

    def test_live_recap_has_only_customer_safe_fields(self):
        sid = client.post("/api/call/start", json={"phone": "9000088877", "mode": "customer"}).json()["session_id"]
        main.SESSIONS[sid].analysis = {"recap": "You asked about prices.", "recap_points": ["You run a clinic"],
                                       "score": 90, "objections": ["price"], "next_best_action": "push hard"}
        r = client.get(f"/api/call/{sid}/recap").json()
        self.assertEqual(r, {"recap": "You asked about prices.", "points": ["You run a clinic"]})

    def test_speech_to_text_endpoint(self):
        sid = client.post("/api/call/start", json={"phone": "9000088866", "mode": "customer"}).json()["session_id"]
        saved = main.stt.transcribe
        main.stt.transcribe = lambda audio, mime, lang: {"text": f"heard {len(audio)} bytes in {lang}",
                                                          "provider": "groq", "stt_ms": 5}
        try:
            url = f"/api/call/stt?session_id={sid}&language=te"
            r = client.post(url, content=b"RIFF" + b"\0" * 100, headers={"Content-Type": "audio/wav"})
            self.assertEqual(r.json(), {"text": "heard 104 bytes in te"})       # customer: text only
            self.assertEqual(client.post(url, content=b"x", headers={"Content-Type": "text/plain"}).status_code, 415)
            self.assertEqual(client.post(url, content=b"", headers={"Content-Type": "audio/wav"}).status_code, 400)
            self.assertEqual(client.post("/api/call/stt?session_id=nope", content=b"x",
                                         headers={"Content-Type": "audio/wav"}).status_code, 404)
        finally:
            main.stt.transcribe = saved

    def test_old_page_links_redirect_to_the_single_page(self):
        self.assertEqual(client.get("/call.html", follow_redirects=False).headers["location"], "/#customer")
        self.assertEqual(client.get("/admin.html", follow_redirects=False).headers["location"], "/#admin")

    def test_unknown_mode_rejected(self):
        self.assertEqual(client.post("/api/call/start", json={"phone": "9000099998", "mode": "admin"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
