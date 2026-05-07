import os
# When CUTTLEFISH_ASR_CPU=1, hide GPUs from torch BEFORE any other module
# imports torch. Lets users with a mismatched CUDA driver/runtime fall back
# to CPU-only ASR without rebuilding PyTorch.
if os.environ.get("CUTTLEFISH_ASR_CPU"):
    os.environ["CUDA_VISIBLE_DEVICES"] = ""

import argparse
import sys
from pathlib import Path

from cuttlefish import __version__, db, scanner


def cmd_init_db(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_schema(conn)
    print(f"Initialized DB at {args.db or db.default_db_path()}")
    return 0


def cmd_add_library(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_schema(conn)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    try:
        with conn:
            conn.execute(
                "INSERT INTO libraries (name, root_path) VALUES (?, ?)",
                (args.name, str(root)),
            )
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"Added library {args.name!r} at {root}")
    return 0


def cmd_list_libraries(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_schema(conn)
    rows = conn.execute(
        "SELECT id, name, root_path FROM libraries ORDER BY id"
    ).fetchall()
    if not rows:
        print("No libraries.")
        return 0
    for r in rows:
        print(f"{r['id']:>3}  {r['name']:<20}  {r['root_path']}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_schema(conn)
    if args.name:
        rows = conn.execute(
            "SELECT id, name, root_path FROM libraries WHERE name = ?",
            (args.name,),
        ).fetchall()
        if not rows:
            print(f"No library named {args.name!r}.", file=sys.stderr)
            return 1
    else:
        rows = conn.execute(
            "SELECT id, name, root_path FROM libraries ORDER BY id"
        ).fetchall()
        if not rows:
            print("No libraries to scan. Use `add-library` first.", file=sys.stderr)
            return 1
    total = scanner.ScanResult()
    for r in rows:
        print(f"Scanning {r['name']} at {r['root_path']} ...")
        result = scanner.scan_library(conn, r["id"], Path(r["root_path"]))
        print(
            f"  movies={result.movies_added} shows={result.shows_added} "
            f"episodes={result.episodes_added} audiobooks={result.audiobooks_added} "
            f"tracks={result.tracks_added} skipped={result.skipped}"
        )
        total.merge(result)
    print(
        f"Done. movies={total.movies_added} shows={total.shows_added} "
        f"episodes={total.episodes_added} audiobooks={total.audiobooks_added} "
        f"tracks={total.tracks_added} skipped={total.skipped}"
    )
    return 0


def cmd_list_media(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_schema(conn)
    sql = """
        SELECT m.id, m.kind, m.title_guess, m.source_path, l.name AS library
        FROM media m
        JOIN libraries l ON l.id = m.library_id
    """
    params: tuple = ()
    if args.library:
        sql += " WHERE l.name = ?"
        params = (args.library,)
    sql += " ORDER BY m.kind, m.title_guess"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("No media.")
        return 0
    for r in rows:
        print(f"{r['id']:>4}  {r['kind']:<10}  {r['library']:<20}  {r['title_guess']}")
    return 0


def _print_bootstrap_banner(host: str, port: int, username: str, password: str) -> None:
    # Stylized so the operator can't miss it scrolling through uvicorn output.
    bar = "=" * 70
    display_host = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    url = f"http://{display_host}:{port}/login"
    print(file=sys.stderr)
    print(bar, file=sys.stderr)
    print("  CUTTLEFISH FIRST-TIME SETUP", file=sys.stderr)
    print("", file=sys.stderr)
    print("  An admin user has been created for you. Save this password —", file=sys.stderr)
    print("  it will not be shown again. You can change it after logging", file=sys.stderr)
    print("  in at /account.", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"    URL:      {url}", file=sys.stderr)
    print(f"    Username: {username}", file=sys.stderr)
    print(f"    Password: {password}", file=sys.stderr)
    print(bar, file=sys.stderr)
    print(file=sys.stderr)


def cmd_serve(args: argparse.Namespace) -> int:
    import logging
    import threading

    import uvicorn

    from cuttlefish import config as cfg_mod
    from cuttlefish.server import create_app
    from cuttlefish.workers import asr, encoder

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Config file is layered *under* CLI defaults: CLI flags win because
    # argparse already filled them. We only adopt config values when the
    # CLI value matches the parser's default (i.e. user didn't pass it).
    if args.config:
        cfg = cfg_mod.load_or_die(args.config)
        if cfg.db and args.db is None:
            args.db = str(cfg.db)
        # Sentinels matching parser defaults
        if args.host == "127.0.0.1":
            args.host = cfg.server.host
        if args.port == 8000:
            args.port = cfg.server.port
        if not args.with_worker:
            args.with_worker = cfg.server.with_worker
        if args.ffmpeg == "ffmpeg":
            args.ffmpeg = cfg.server.ffmpeg
        if cfg.libraries:
            from cuttlefish import db as _db
            conn = _db.connect(args.db)
            _db.init_schema(conn)
            added, updated = cfg.apply_libraries(conn)
            print(
                f"config: applied libraries (added={added}, updated={updated})",
                file=sys.stderr,
            )

    # Always ensure the schema exists — makes a fresh install work end-to-end
    # without having to remember `init-db` first.
    from cuttlefish import auth as _auth
    conn = db.connect(args.db)
    db.init_schema(conn)
    # If the previous server died mid-job, the job is stuck in 'running'
    # and no worker will ever pick it back up (the queue lives on
    # status='queued'). Reset stale running jobs so they get retried.
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'queued', started_at = NULL "
            "WHERE status = 'running'"
        )
    if cur.rowcount and cur.rowcount > 0:
        print(
            f"Reset {cur.rowcount} stale 'running' job(s) → 'queued' "
            "(orphaned by a previous worker death).",
            file=sys.stderr,
        )
    creds = _auth.bootstrap_admin_if_empty(conn)
    if creds:
        _print_bootstrap_banner(args.host, args.port, creds[0], creds[1])

    if args.with_worker:
        t = threading.Thread(
            target=encoder.run_worker,
            kwargs={"db_path": args.db, "poll_interval": 5.0, "ffmpeg": args.ffmpeg},
            daemon=True,
            name="encode-worker",
        )
        t.start()
        print(f"started embedded encode worker (thread={t.name})", file=sys.stderr)
    # ASR worker is always-on as of cuttlefish 0.0.0+: it's a core part
    # of the system, not an opt-in. If the deps somehow aren't importable
    # (broken venv, partial install) we warn and keep going so the rest
    # of the server still works — but normally this branch is taken.
    if asr.is_available():
        asr.mark_worker_started()
        t = threading.Thread(
            target=asr.run_worker,
            kwargs={"db_path": args.db, "poll_interval": 5.0, "ffmpeg": args.ffmpeg},
            daemon=True,
            name="asr-worker",
        )
        t.start()
        print(f"started embedded ASR worker (thread={t.name})", file=sys.stderr)
    else:
        print(
            "warning: ASR dependencies (torch + nemo_toolkit) aren't importable; "
            "subtitle generation will be unavailable until you re-run `uv sync`.",
            file=sys.stderr,
        )

    # TLS provisioning if enabled in config
    ssl_kwargs: dict = {}
    if args.config:
        cfg = cfg_mod.load_or_die(args.config)
        if cfg.tls.enabled:
            from cuttlefish import tls as tls_mod
            tls_cfg = tls_mod.TLSConfig(
                domain=cfg.tls.domain,
                email=cfg.tls.email,
                dns_provider=cfg.tls.dns_provider,
                dns_credentials_file=cfg.tls.dns_credentials_file,
                cert_dir=cfg.tls.cert_dir,
                renewal_window_days=cfg.tls.renewal_window_days,
            )
            cert, key = tls_mod.ensure_cert(tls_cfg)
            ssl_kwargs = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
            # Background renewal loop
            renewal_t = threading.Thread(
                target=tls_mod.renewal_loop,
                args=(tls_cfg,),
                daemon=True, name="tls-renewal",
            )
            renewal_t.start()

    app = create_app(db_path=args.db)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", **ssl_kwargs)
    return 0


def cmd_encode_worker(args: argparse.Namespace) -> int:
    import logging

    from cuttlefish.workers import encoder

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    n = encoder.run_worker(
        db_path=args.db, once=args.once, poll_interval=args.poll, ffmpeg=args.ffmpeg
    )
    print(f"processed {n} job(s)")
    return 0


def cmd_asr_worker(args: argparse.Namespace) -> int:
    import logging

    from cuttlefish.workers import asr

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not asr.is_available():
        print(
            "ASR dependencies not installed. Run: uv sync --extra asr",
            file=sys.stderr,
        )
        return 2
    asr.mark_worker_started()
    n = asr.run_worker(
        db_path=args.db, once=args.once, poll_interval=args.poll, ffmpeg=args.ffmpeg
    )
    print(f"processed {n} job(s)")
    return 0


def cmd_encode_now(args: argparse.Namespace) -> int:
    """Encode a single media item synchronously (no queue)."""
    import logging

    from cuttlefish.workers import encoder

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = db.connect(args.db)
    db.init_schema(conn)
    try:
        result = encoder.encode_media(
            conn, args.media_id, ffmpeg=args.ffmpeg, overwrite=args.overwrite
        )
    except encoder.EncodeError as e:
        print(f"encode failed: {e}", file=sys.stderr)
        return 1
    print(f"encoded {result.video_path} ({result.size_bytes} bytes)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cuttlefish", description="Self-hosted media server."
    )
    parser.add_argument(
        "--version", action="version", version=f"cuttlefish {__version__}"
    )
    parser.add_argument(
        "--db",
        help="SQLite DB path (default: $XDG_DATA_HOME/cuttlefish/cuttlefish.db)",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("init-db", help="Create the SQLite schema.")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("add-library", help="Register a library root (folder of media).")
    p.add_argument("name")
    p.add_argument("root")
    p.set_defaults(func=cmd_add_library)

    p = sub.add_parser("list-libraries", help="List registered libraries.")
    p.set_defaults(func=cmd_list_libraries)

    p = sub.add_parser("scan", help="Scan one or all libraries.")
    p.add_argument("name", nargs="?", help="Library name. If omitted, scans all.")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("list-media", help="List discovered media.")
    p.add_argument("--library", help="Restrict to a single library by name.")
    p.set_defaults(func=cmd_list_media)

    p = sub.add_parser("serve", help="Run the cuttlefish web server.")
    # Also accept --db here so it can come AFTER 'serve' on the command line.
    # The top-level --db on the main parser still works for backward compat.
    p.add_argument("--db", help="SQLite DB path (overrides top-level --db).")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    p.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")
    p.add_argument("--with-worker", action="store_true",
                   help="Also run the encode worker in a background thread.")
    # The ASR worker now always starts (deps are required). The flag is
    # accepted silently for backward compatibility with older invocations.
    p.add_argument("--with-asr-worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg binary path for embedded workers.")
    p.add_argument("--config", help="Path to a TOML config file (see docs/configuration.md).")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("encode-worker", help="Run the encode worker loop.")
    p.add_argument("--once", action="store_true", help="Process one job and exit.")
    p.add_argument("--poll", type=float, default=5.0, help="Seconds between polls when idle.")
    p.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg binary path.")
    p.set_defaults(func=cmd_encode_worker)

    p = sub.add_parser("encode-now", help="Encode one media item synchronously (bypassing the queue).")
    p.add_argument("media_id", type=int)
    p.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg binary path.")
    p.add_argument("--overwrite", action="store_true", help="Re-encode even if output exists.")
    p.set_defaults(func=cmd_encode_now)

    p = sub.add_parser("asr-worker", help="Run the ASR (Parakeet) worker loop. Requires the [asr] extra.")
    p.add_argument("--once", action="store_true", help="Process one job and exit.")
    p.add_argument("--poll", type=float, default=5.0, help="Seconds between polls when idle.")
    p.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg binary path.")
    p.set_defaults(func=cmd_asr_worker)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
