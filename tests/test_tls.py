"""TLS scaffold tests — all subprocess calls mocked, no real certbot/openssl."""
from __future__ import annotations

import datetime
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from cuttlefish import config as cfg_mod
from cuttlefish import tls


def _make_cfg(tmp_path: Path) -> tls.TLSConfig:
    return tls.TLSConfig(
        domain="media.example.com",
        email="me@example.com",
        dns_provider="cloudflare",
        dns_credentials_file=tmp_path / "creds.ini",
        cert_dir=tmp_path / "certs",
    )


# --- cert_needs_renewal --------------------------------------------------


def test_needs_renewal_when_cert_missing(tmp_path):
    assert tls.cert_needs_renewal(tmp_path / "missing.pem") is True


def test_needs_renewal_when_close_to_expiry(tmp_path):
    cert = tmp_path / "cert.pem"
    cert.write_text("dummy")
    expires = datetime.datetime(2026, 5, 20, 12, 0, 0)
    fake_proc = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=f"notAfter={expires.strftime('%b %d %H:%M:%S %Y')} GMT\n",
    )
    now = datetime.datetime(2026, 5, 1, 12, 0, 0)  # 19 days before expiry
    with patch.object(subprocess, "run", return_value=fake_proc):
        assert tls.cert_needs_renewal(cert, days=30, now=now) is True


def test_does_not_need_renewal_when_cert_is_fresh(tmp_path):
    cert = tmp_path / "cert.pem"
    cert.write_text("dummy")
    expires = datetime.datetime(2026, 8, 1, 12, 0, 0)
    fake_proc = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=f"notAfter={expires.strftime('%b %d %H:%M:%S %Y')} GMT\n",
    )
    now = datetime.datetime(2026, 5, 1, 12, 0, 0)  # 92 days out
    with patch.object(subprocess, "run", return_value=fake_proc):
        assert tls.cert_needs_renewal(cert, days=30, now=now) is False


def test_needs_renewal_when_openssl_unparseable(tmp_path):
    cert = tmp_path / "cert.pem"
    cert.write_text("dummy")
    fake_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="garbage\n")
    with patch.object(subprocess, "run", return_value=fake_proc):
        assert tls.cert_needs_renewal(cert) is True


def test_needs_renewal_when_openssl_missing(tmp_path):
    cert = tmp_path / "cert.pem"
    cert.write_text("dummy")
    with patch.object(subprocess, "run", side_effect=FileNotFoundError()):
        assert tls.cert_needs_renewal(cert) is True


# --- run_certbot --------------------------------------------------------


def test_run_certbot_invokes_correct_command(tmp_path):
    cfg = _make_cfg(tmp_path)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch.object(subprocess, "run", side_effect=fake_run):
        tls.run_certbot(cfg)
    cmd = captured["cmd"]
    assert "certbot" in cmd[0]
    assert "certonly" in cmd
    assert "--dns-cloudflare" in cmd
    assert "-d" in cmd and "media.example.com" in cmd
    assert "--email" in cmd and "me@example.com" in cmd


def test_run_certbot_raises_on_failure(tmp_path):
    cfg = _make_cfg(tmp_path)
    fake_proc = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
    with patch.object(subprocess, "run", return_value=fake_proc):
        with pytest.raises(RuntimeError, match="certbot failed"):
            tls.run_certbot(cfg)


# --- ensure_cert --------------------------------------------------------


def test_ensure_cert_skips_when_fresh(tmp_path):
    cfg = _make_cfg(tmp_path)
    cfg.cert_dir.mkdir()
    (cfg.cert_dir / "fullchain.pem").write_text("c")
    (cfg.cert_dir / "privkey.pem").write_text("k")
    expires = datetime.datetime.utcnow() + datetime.timedelta(days=60)
    fake_proc = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=f"notAfter={expires.strftime('%b %d %H:%M:%S %Y')} GMT\n",
    )
    with patch.object(subprocess, "run", return_value=fake_proc) as m:
        cert, key = tls.ensure_cert(cfg)
    # Only the openssl probe was called, not certbot
    assert m.call_count == 1
    assert cert.is_file() and key.is_file()


def test_ensure_cert_runs_certbot_when_missing(tmp_path):
    cfg = _make_cfg(tmp_path)

    def fake_run(cmd, **kwargs):
        # Simulate certbot creating the files
        if cmd[0].endswith("certbot"):
            cfg.cert_dir.mkdir(exist_ok=True)
            (cfg.cert_dir / "fullchain.pem").write_text("c")
            (cfg.cert_dir / "privkey.pem").write_text("k")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        # openssl path — won't be hit because cert was missing
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="")

    with patch.object(subprocess, "run", side_effect=fake_run):
        cert, key = tls.ensure_cert(cfg)
    assert cert.is_file() and key.is_file()


def test_ensure_cert_raises_if_certbot_does_not_create_files(tmp_path):
    cfg = _make_cfg(tmp_path)
    fake_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(subprocess, "run", return_value=fake_proc):
        with pytest.raises(RuntimeError, match="cert files not found"):
            tls.ensure_cert(cfg)


# --- config.tls --------------------------------------------------------


def test_config_tls_disabled_by_default(tmp_path):
    c = tmp_path / "c.toml"
    c.write_text('db = "/tmp/cf.db"\n')
    cfg = cfg_mod.Config.load(c)
    assert cfg.tls.enabled is False


def test_config_tls_full_settings(tmp_path):
    creds = tmp_path / "creds.ini"; creds.write_text("")
    c = tmp_path / "c.toml"
    c.write_text(f"""
[tls]
enabled = true
domain = "media.example.com"
email = "me@example.com"
dns_provider = "cloudflare"
dns_credentials_file = "{creds}"
cert_dir = "{tmp_path}/certs"
renewal_window_days = 14
""")
    cfg = cfg_mod.Config.load(c)
    assert cfg.tls.enabled is True
    assert cfg.tls.domain == "media.example.com"
    assert cfg.tls.dns_provider == "cloudflare"
    assert cfg.tls.renewal_window_days == 14


def test_config_tls_enabled_missing_field_raises(tmp_path):
    c = tmp_path / "c.toml"
    c.write_text("""
[tls]
enabled = true
domain = "x"
""")
    with pytest.raises(ValueError, match="enabled but missing"):
        cfg_mod.Config.load(c)
