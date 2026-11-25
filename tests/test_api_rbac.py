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


if __name__ == "__main__":
    unittest.main()
