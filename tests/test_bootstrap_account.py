"""Tests for the auto-bootstrap admin and self-service password change."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import auth, db
from cuttlefish.server import create_app


# --- bootstrap_admin_if_empty -------------------------------------------


def test_bootstrap_creates_admin_when_empty(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    creds = auth.bootstrap_admin_if_empty(conn)
    assert creds is not None
    username, password = creds
    assert username == "admin"
    assert len(password) >= 16          # token_urlsafe(16) yields 22+ chars
    assert auth.user_count(conn) == 1
    # The user is admin and authenticatable with the returned password
    user_id = auth.authenticate(conn, "admin", password)
    assert user_id is not None
    row = auth.find_user_by_username(conn, "admin")
    assert row["is_admin"] == 1


def test_bootstrap_is_noop_when_users_exist(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    auth.create_user(conn, "alice", "secret123", is_admin=True)
    creds = auth.bootstrap_admin_if_empty(conn)
    assert creds is None
    # No new users were added
    assert auth.user_count(conn) == 1


def test_bootstrap_passwords_are_different_each_call(tmp_path):
    """Each fresh DB should get its own random password — the password isn't
    derived from anything stable."""
    a_conn = db.connect(tmp_path / "a.db"); db.init_schema(a_conn)
    b_conn = db.connect(tmp_path / "b.db"); db.init_schema(b_conn)
    a = auth.bootstrap_admin_if_empty(a_conn)
    b = auth.bootstrap_admin_if_empty(b_conn)
    assert a is not None and b is not None
    assert a[1] != b[1]


# --- self-service password change (API) --------------------------------


def _admin(tmp_path):
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register",
                data={"username": "alice", "password": "secret123"})
    client.post("/api/auth/login",
                data={"username": "alice", "password": "secret123"})
    return client, db_path


def test_change_password_requires_login(tmp_path):
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    r = client.put("/api/me/password",
                   json={"current_password": "x", "new_password": "newpass"})
    assert r.status_code == 401


def test_change_password_rejects_wrong_current(tmp_path):
    client, _ = _admin(tmp_path)
    r = client.put("/api/me/password",
                   json={"current_password": "WRONG", "new_password": "newpass1"})
    assert r.status_code == 401


def test_change_password_rejects_short_new(tmp_path):
    client, _ = _admin(tmp_path)
    r = client.put("/api/me/password",
                   json={"current_password": "secret123", "new_password": "abc"})
    assert r.status_code == 400


def test_change_password_succeeds_and_lets_user_relogin(tmp_path):
    client, db_path = _admin(tmp_path)
    r = client.put("/api/me/password",
                   json={"current_password": "secret123", "new_password": "newer123"})
    assert r.status_code == 200
    fresh = TestClient(create_app(db_path=db_path))
    assert fresh.post("/api/auth/login",
                      data={"username": "alice", "password": "newer123"}).status_code == 200
    assert fresh.post("/api/auth/login",
                      data={"username": "alice", "password": "secret123"}).status_code == 401


# --- /account HTML page ------------------------------------------------


def test_account_page_requires_login(tmp_path):
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    assert client.get("/account").status_code == 401


def test_account_page_renders_with_form(tmp_path):
    client, _ = _admin(tmp_path)
    r = client.get("/account")
    assert r.status_code == 200
    assert "Change password" in r.text
    assert "current_password" in r.text


def test_account_form_password_change_succeeds(tmp_path):
    client, db_path = _admin(tmp_path)
    r = client.post(
        "/account/password",
        data={
            "current_password": "secret123",
            "new_password": "newer1234",
            "confirm_password": "newer1234",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/account?ok=" in r.headers["location"]


def test_account_form_password_mismatch(tmp_path):
    client, _ = _admin(tmp_path)
    r = client.post(
        "/account/password",
        data={
            "current_password": "secret123",
            "new_password": "newer1234",
            "confirm_password": "different1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "do+not+match" in r.headers["location"] or "do%20not%20match" in r.headers["location"]


def test_account_form_wrong_current_password(tmp_path):
    client, _ = _admin(tmp_path)
    r = client.post(
        "/account/password",
        data={
            "current_password": "WRONG",
            "new_password": "newer1234",
            "confirm_password": "newer1234",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "incorrect" in r.headers["location"].lower()


# --- /register page after first user (admin-only redirect) ---------------


def test_register_page_for_anonymous_after_first_user(tmp_path):
    """After bootstrap creates the first admin, /register should NOT show a
    sign-up form to an anonymous visitor — should explain admin gating."""
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    auth.bootstrap_admin_if_empty(conn)
    client = TestClient(create_app(db_path=db_path))
    r = client.get("/register")
    assert r.status_code == 200
    assert "<form" not in r.text or "/register" not in r.text.split("<form", 1)[1].split(">", 1)[0]
    assert "admin" in r.text.lower()


# --- empty libraries page hint --------------------------------------------


def test_empty_libraries_page_links_to_admin_for_admin(tmp_path):
    client, _ = _admin(tmp_path)
    r = client.get("/")
    assert "/admin/libraries" in r.text


def test_empty_libraries_page_message_for_anonymous(tmp_path):
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    r = client.get("/")
    # Anonymous user sees a different message
    assert "log in" in r.text.lower() or "/login" in r.text
