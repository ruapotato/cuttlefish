"""User management admin endpoints + page tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import auth, db
from cuttlefish.server import create_app


def _client(tmp_path: Path):
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    return TestClient(create_app(db_path=db_path)), db_path


def _admin(client):
    client.post("/api/auth/register", data={"username": "admin", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "admin", "password": "secret123"})


def _add_user(client, username: str, is_admin: bool = False) -> int:
    """Use admin to add a user via the existing auth API. Returns user id."""
    r = client.post("/api/auth/register",
                    data={"username": username, "password": "secret123"})
    assert r.status_code == 200, r.text
    # If is_admin requested, promote via PATCH after creation
    if is_admin:
        # Find the new user's id
        users = client.get("/api/admin/users").json()
        u = next(u for u in users if u["username"] == username)
        r = client.patch(f"/api/admin/users/{u['id']}", json={"is_admin": True})
        assert r.status_code == 200
        return u["id"]
    users = client.get("/api/admin/users").json()
    return next(u for u in users if u["username"] == username)["id"]


# --- list / require admin -----------------------------------------------


def test_users_endpoints_require_admin(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/api/admin/users").status_code == 401
    assert client.get("/admin/users").status_code == 401


def test_list_users_includes_admin(tmp_path):
    client, _ = _client(tmp_path)
    _admin(client)
    rows = client.get("/api/admin/users").json()
    assert len(rows) == 1
    assert rows[0]["username"] == "admin"
    assert rows[0]["is_admin"] is True


def test_list_users_includes_others(tmp_path):
    client, _ = _client(tmp_path)
    _admin(client)
    _add_user(client, "bob")
    rows = client.get("/api/admin/users").json()
    names = sorted(u["username"] for u in rows)
    assert names == ["admin", "bob"]


# --- delete --------------------------------------------------------------


def test_delete_user(tmp_path):
    client, _ = _client(tmp_path)
    _admin(client)
    bob_id = _add_user(client, "bob")
    assert client.delete(f"/api/admin/users/{bob_id}").status_code == 200
    rows = client.get("/api/admin/users").json()
    assert all(u["username"] != "bob" for u in rows)


def test_delete_self_blocked(tmp_path):
    client, _ = _client(tmp_path)
    _admin(client)
    me = client.get("/api/admin/users").json()[0]
    r = client.delete(f"/api/admin/users/{me['id']}")
    assert r.status_code == 409


def test_delete_last_admin_blocked(tmp_path):
    """Can't delete the only admin even when targeting a different account if
    it's the only admin (i.e. self) — covered above. Also test that demoting
    the only admin is blocked, then deleting it should succeed only after a
    second admin exists."""
    client, _ = _client(tmp_path)
    _admin(client)
    bob_id = _add_user(client, "bob", is_admin=True)
    me = client.get("/api/admin/users").json()
    me_id = next(u["id"] for u in me if u["username"] == "admin")
    # Now we have two admins. Demote bob → still allowed (admin count was 2)
    assert client.patch(f"/api/admin/users/{bob_id}", json={"is_admin": False}).status_code == 200
    # Now only admin is "admin". Demoting admin via PATCH should be 409.
    assert client.patch(f"/api/admin/users/{me_id}", json={"is_admin": False}).status_code == 409
    # Re-promote bob to admin so we can delete admin without losing all admins
    client.patch(f"/api/admin/users/{bob_id}", json={"is_admin": True})
    # bob can now be deleted (other admin remains)
    # but admin (self) still can't delete self even though another admin exists
    assert client.delete(f"/api/admin/users/{me_id}").status_code == 409
    assert client.delete(f"/api/admin/users/{bob_id}").status_code == 200


# --- patch ---------------------------------------------------------------


def test_patch_promote_demote(tmp_path):
    client, _ = _client(tmp_path)
    _admin(client)
    bob_id = _add_user(client, "bob")
    r = client.patch(f"/api/admin/users/{bob_id}", json={"is_admin": True})
    assert r.status_code == 200
    assert next(u for u in client.get("/api/admin/users").json() if u["id"] == bob_id)["is_admin"] is True


def test_patch_set_password(tmp_path):
    client, db_path = _client(tmp_path)
    _admin(client)
    bob_id = _add_user(client, "bob")
    r = client.patch(f"/api/admin/users/{bob_id}", json={"password": "newpass99"})
    assert r.status_code == 200
    # verify auth with new password works
    fresh = TestClient(create_app(db_path=db_path))
    assert fresh.post("/api/auth/login", data={"username": "bob", "password": "newpass99"}).status_code == 200


def test_patch_short_password_rejected(tmp_path):
    client, _ = _client(tmp_path)
    _admin(client)
    bob_id = _add_user(client, "bob")
    assert client.patch(f"/api/admin/users/{bob_id}", json={"password": "abc"}).status_code == 400


def test_patch_unknown_user_404(tmp_path):
    client, _ = _client(tmp_path)
    _admin(client)
    assert client.patch("/api/admin/users/9999", json={"is_admin": False}).status_code == 404


# --- HTML page + form ---------------------------------------------------


def test_admin_users_page_renders(tmp_path):
    client, _ = _client(tmp_path)
    _admin(client)
    r = client.get("/admin/users")
    assert r.status_code == 200
    assert "Add a user" in r.text
    assert "admin" in r.text


def test_admin_users_form_create(tmp_path):
    client, db_path = _client(tmp_path)
    _admin(client)
    r = client.post("/admin/users",
                    data={"username": "carol", "password": "secret123"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/admin/users").json()  # carol exists
    assert any(u["username"] == "carol" for u in client.get("/api/admin/users").json())


def test_admin_users_form_create_admin(tmp_path):
    client, _ = _client(tmp_path)
    _admin(client)
    r = client.post("/admin/users",
                    data={"username": "carol", "password": "secret123", "is_admin": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    carol = next(u for u in client.get("/api/admin/users").json() if u["username"] == "carol")
    assert carol["is_admin"] is True


def test_admin_users_form_toggle_admin(tmp_path):
    client, _ = _client(tmp_path)
    _admin(client)
    bob_id = _add_user(client, "bob")
    r = client.post(f"/admin/users/{bob_id}/toggle-admin", follow_redirects=False)
    assert r.status_code == 303
    bob = next(u for u in client.get("/api/admin/users").json() if u["id"] == bob_id)
    assert bob["is_admin"] is True


def test_admin_users_form_delete(tmp_path):
    client, _ = _client(tmp_path)
    _admin(client)
    bob_id = _add_user(client, "bob")
    r = client.post(f"/admin/users/{bob_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert all(u["id"] != bob_id for u in client.get("/api/admin/users").json())


def test_admin_users_page_hides_self_actions(tmp_path):
    client, _ = _client(tmp_path)
    _admin(client)
    r = client.get("/admin/users")
    assert "(you)" in r.text  # self row marked, no delete/promote button
