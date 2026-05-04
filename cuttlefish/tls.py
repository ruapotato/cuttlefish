"""TLS provisioning via Let's Encrypt + ACME DNS-01 (certbot wrapper).

The architecture is documented in docs/tls.md. This module is the thin
glue that:

  - tells whether an existing cert needs renewal (via openssl)
  - shells out to certbot with the right DNS plugin
  - composes the two into ensure_cert(), which is what serve() calls

Auto-renewal: a daemon thread can call ensure_cert() periodically (every
day or so) to keep the cert fresh. Uvicorn picks up reissued certs on
SIGHUP.

We deliberately do NOT implement ACME ourselves — certbot is mature,
audited, and supports basically every DNS provider via plugins.
"""
from __future__ import annotations

import datetime
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class TLSConfig:
    domain: str
    email: str
    dns_provider: str            # "cloudflare", "route53", "digitalocean", etc
    dns_credentials_file: Path   # Path to the credentials INI for the plugin
    cert_dir: Path               # /etc/letsencrypt/live/<domain> typically
    renewal_window_days: int = 30


def cert_paths(cert_dir: Path) -> tuple[Path, Path]:
    return cert_dir / "fullchain.pem", cert_dir / "privkey.pem"


def cert_needs_renewal(
    cert_path: Path, days: int = 30, openssl: str = "openssl",
    now: Optional[datetime.datetime] = None,
) -> bool:
    """Return True if cert is missing OR expires within `days`."""
    if not cert_path.is_file():
        return True
    try:
        proc = subprocess.run(
            [openssl, "x509", "-in", str(cert_path), "-noout", "-enddate"],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return True
    line = proc.stdout.strip()
    if "=" not in line:
        return True
    date_str = line.split("=", 1)[1].strip()
    try:
        expires = datetime.datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z")
    except ValueError:
        return True
    now = now or datetime.datetime.utcnow()
    return (expires - now).days < days


def run_certbot(cfg: TLSConfig, certbot: str = "certbot") -> None:
    """Provision (or renew) the cert by invoking certbot."""
    cmd = [
        certbot, "certonly", "--non-interactive", "--agree-tos",
        "--email", cfg.email,
        f"--dns-{cfg.dns_provider}",
        f"--dns-{cfg.dns_provider}-credentials", str(cfg.dns_credentials_file),
        "-d", cfg.domain,
    ]
    log.info("running certbot: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"certbot failed (exit {proc.returncode}): {proc.stderr[-2000:]}"
        )


def ensure_cert(cfg: TLSConfig, certbot: str = "certbot",
                openssl: str = "openssl") -> tuple[Path, Path]:
    """Provision the cert if missing or near-expiry. Return (cert, key) paths."""
    cert, key = cert_paths(cfg.cert_dir)
    if cert_needs_renewal(cert, days=cfg.renewal_window_days, openssl=openssl):
        log.info("provisioning/renewing cert for %s", cfg.domain)
        run_certbot(cfg, certbot=certbot)
        if not cert.is_file() or not key.is_file():
            raise RuntimeError(
                f"certbot ran but cert files not found at {cert} / {key}"
            )
    return cert, key


def renewal_loop(cfg: TLSConfig, interval_hours: float = 24.0,
                 certbot: str = "certbot", openssl: str = "openssl") -> None:
    """Daemon-thread target: periodically call ensure_cert."""
    import time
    while True:
        try:
            ensure_cert(cfg, certbot=certbot, openssl=openssl)
        except Exception:
            log.exception("renewal_loop iteration failed; will retry next interval")
        time.sleep(interval_hours * 3600)
