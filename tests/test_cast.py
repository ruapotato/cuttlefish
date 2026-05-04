"""Tests for the casting WebSocket bus."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db
from cuttlefish.server import create_app


@pytest.fixture
def client(tmp_path: Path):
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    return TestClient(create_app(db_path=db_path))


def _login(client, username="alice"):
    client.post("/api/auth/register", data={"username": username, "password": "secret123"})
    client.post("/api/auth/login", data={"username": username, "password": "secret123"})


# --- websocket auth -----------------------------------------------------


def test_ws_rejects_unauthenticated(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/api/cast/channel") as ws:
            ws.receive_json()


# --- registration -------------------------------------------------------


def test_register_target(client):
    _login(client)
    with client.websocket_connect("/api/cast/channel") as ws:
        ws.send_json({"type": "identify", "role": "target", "label": "TV"})
        msg = ws.receive_json()
        assert msg["type"] == "registered"
        assert msg["role"] == "target"
        assert msg["client_id"]
        assert msg["targets"] == []  # only one device, itself


def test_register_invalid_role_closes(client):
    _login(client)
    with pytest.raises(Exception):
        with client.websocket_connect("/api/cast/channel") as ws:
            ws.send_json({"type": "identify", "role": "bogus", "label": "X"})
            ws.receive_json()


def test_targets_endpoint_lists_active(client):
    _login(client)
    with client.websocket_connect("/api/cast/channel") as ws:
        ws.send_json({"type": "identify", "role": "target", "label": "TV"})
        ws.receive_json()  # registered
        targets = client.get("/api/cast/targets").json()
        assert len(targets) == 1
        assert targets[0]["label"] == "TV"
        assert targets[0]["role"] == "target"


def test_targets_endpoint_requires_auth(client):
    assert client.get("/api/cast/targets").status_code == 401


# --- bus pub/sub -------------------------------------------------------


def test_target_announcement_reaches_other_clients(client):
    _login(client)
    with client.websocket_connect("/api/cast/channel") as a:
        a.send_json({"type": "identify", "role": "controller", "label": "Phone"})
        a.receive_json()  # registered
        with client.websocket_connect("/api/cast/channel") as b:
            b.send_json({"type": "identify", "role": "target", "label": "TV"})
            b.receive_json()  # registered (self)
            # Controller A should see a 'target_available' for B
            announcement = a.receive_json()
            assert announcement["type"] == "target_available"
            assert announcement["label"] == "TV"


def test_command_routes_to_named_target(client):
    _login(client)
    with client.websocket_connect("/api/cast/channel") as target:
        target.send_json({"type": "identify", "role": "target", "label": "TV"})
        reg = target.receive_json()
        target_id = reg["client_id"]
        with client.websocket_connect("/api/cast/channel") as ctl:
            ctl.send_json({"type": "identify", "role": "controller", "label": "Phone"})
            ctl.receive_json()  # registered
            # Note: only target announcements broadcast, so the target gets no
            # message when a controller joins. We go straight to the command.
            ctl.send_json({"type": "command", "to": target_id, "action": "pause", "payload": {}})
            cmd = target.receive_json()
            assert cmd["type"] == "command"
            assert cmd["action"] == "pause"
            assert cmd["from"]


def test_state_update_fans_out_to_controllers(client):
    _login(client)
    with client.websocket_connect("/api/cast/channel") as target:
        target.send_json({"type": "identify", "role": "target", "label": "TV"})
        target.receive_json()  # registered
        with client.websocket_connect("/api/cast/channel") as ctl:
            ctl.send_json({"type": "identify", "role": "controller", "label": "Phone"})
            ctl.receive_json()  # registered
            # Target emits a state_update; controller should receive it
            target.send_json({
                "type": "state_update",
                "media_id": 7,
                "position_seconds": 42.0,
                "playing": True,
            })
            update = ctl.receive_json()
            assert update["type"] == "state_update"
            assert update["media_id"] == 7
            assert update["position_seconds"] == 42.0


def test_disconnection_announces_target_gone(client):
    _login(client)
    with client.websocket_connect("/api/cast/channel") as ctl:
        ctl.send_json({"type": "identify", "role": "controller", "label": "Phone"})
        ctl.receive_json()
        with client.websocket_connect("/api/cast/channel") as target:
            target.send_json({"type": "identify", "role": "target", "label": "TV"})
            target.receive_json()
            ctl.receive_json()  # target_available
            # Closing the with block will trigger disconnect
        # After target context exits, controller should get a target_gone
        gone = ctl.receive_json()
        assert gone["type"] == "target_gone"


def test_users_are_isolated(tmp_path):
    """User A's targets should NOT be visible to user B (per-user fan-out).

    Both clients must share the same app (same in-process cast_bus) — that's
    what we're verifying isolates correctly.
    """
    db_path = tmp_path / "t2.db"
    db.init_schema(db.connect(db_path))
    app = create_app(db_path=db_path)
    a_client = TestClient(app)
    b_client = TestClient(app)
    # Alice registers and becomes admin
    a_client.post("/api/auth/register", data={"username": "alice", "password": "secret123"})
    a_client.post("/api/auth/login", data={"username": "alice", "password": "secret123"})
    # Alice (admin) creates Bob
    a_client.post("/api/auth/register", data={"username": "bob", "password": "secret123"})
    # Bob logs in on his own client
    b_client.post("/api/auth/login", data={"username": "bob", "password": "secret123"})
    with a_client.websocket_connect("/api/cast/channel") as a_ws:
        a_ws.send_json({"type": "identify", "role": "target", "label": "Alice TV"})
        a_ws.receive_json()
        # Bob shouldn't see Alice's target — different user
        assert b_client.get("/api/cast/targets").json() == []


# --- HTML page ---------------------------------------------------------


def test_cast_page_requires_login(client):
    assert client.get("/cast").status_code == 401


def test_cast_page_renders(client):
    _login(client)
    r = client.get("/cast")
    assert r.status_code == 200
    assert "WebSocket" in r.text
    assert "/api/cast/channel" in r.text


def test_header_includes_cast_link(client):
    _login(client)
    r = client.get("/")
    assert "/cast" in r.text
