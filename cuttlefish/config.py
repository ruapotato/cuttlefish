"""TOML configuration loader.

A small Config dataclass with:

  [server]
  host = "0.0.0.0"
  port = 8000
  with_worker = true
  with_asr_worker = false

  db = "/var/lib/cuttlefish/cuttlefish.db"

  [[library]]
  name = "Movies"
  kind = "movies"
  root = "/data/Movies"

apply_libraries() upserts each [[library]] entry into the libraries table
so a `serve --config` boot is enough to bring a fresh install online with
its libraries already registered.
"""
from __future__ import annotations

import sqlite3
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class LibraryEntry:
    name: str
    root: Path


@dataclass
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8000
    with_worker: bool = False
    with_asr_worker: bool = False
    ffmpeg: str = "ffmpeg"


@dataclass
class TLSSettings:
    enabled: bool = False
    domain: Optional[str] = None
    email: Optional[str] = None
    dns_provider: Optional[str] = None
    dns_credentials_file: Optional[Path] = None
    cert_dir: Optional[Path] = None
    renewal_window_days: int = 30


@dataclass
class Config:
    db: Optional[Path] = None
    server: ServerSettings = field(default_factory=ServerSettings)
    tls: TLSSettings = field(default_factory=TLSSettings)
    libraries: list[LibraryEntry] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | str) -> "Config":
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"config file not found: {path}")
        with open(path, "rb") as f:
            data = tomllib.load(f)
        cfg = cls()
        if "db" in data:
            cfg.db = Path(data["db"]).expanduser()
        srv = data.get("server", {}) or {}
        cfg.server.host = str(srv.get("host", cfg.server.host))
        cfg.server.port = int(srv.get("port", cfg.server.port))
        cfg.server.with_worker = bool(srv.get("with_worker", cfg.server.with_worker))
        cfg.server.with_asr_worker = bool(srv.get("with_asr_worker", cfg.server.with_asr_worker))
        cfg.server.ffmpeg = str(srv.get("ffmpeg", cfg.server.ffmpeg))
        tls = data.get("tls", {}) or {}
        cfg.tls.enabled = bool(tls.get("enabled", False))
        if cfg.tls.enabled:
            for required in ("domain", "email", "dns_provider",
                             "dns_credentials_file", "cert_dir"):
                if not tls.get(required):
                    raise ValueError(f"[tls] enabled but missing {required!r}")
            cfg.tls.domain = tls["domain"]
            cfg.tls.email = tls["email"]
            cfg.tls.dns_provider = tls["dns_provider"]
            cfg.tls.dns_credentials_file = Path(tls["dns_credentials_file"]).expanduser()
            cfg.tls.cert_dir = Path(tls["cert_dir"]).expanduser()
            cfg.tls.renewal_window_days = int(tls.get("renewal_window_days", 30))
        for entry in data.get("library", []) or []:
            name = entry.get("name")
            root = entry.get("root")
            if not (name and root):
                raise ValueError(
                    f"[[library]] entry missing name or root: {entry!r}"
                )
            cfg.libraries.append(
                LibraryEntry(name=name, root=Path(root).expanduser())
            )
        return cfg

    def apply_libraries(self, conn: sqlite3.Connection) -> tuple[int, int]:
        """Upsert all configured libraries. Returns (added, updated)."""
        added = updated = 0
        for lib in self.libraries:
            row = conn.execute(
                "SELECT id FROM libraries WHERE name = ?", (lib.name,)
            ).fetchone()
            if row is None:
                with conn:
                    conn.execute(
                        "INSERT INTO libraries (name, root_path) VALUES (?, ?)",
                        (lib.name, str(lib.root.resolve())),
                    )
                added += 1
            else:
                with conn:
                    conn.execute(
                        "UPDATE libraries SET root_path = ? WHERE id = ?",
                        (str(lib.root.resolve()), row["id"]),
                    )
                updated += 1
        return added, updated


def load_or_die(path: Path | str) -> Config:
    """Load a config and exit with a friendly message if it's broken."""
    try:
        return Config.load(path)
    except (tomllib.TOMLDecodeError, FileNotFoundError, ValueError) as e:
        print(f"config error in {path}: {e}", file=sys.stderr)
        raise SystemExit(2)
