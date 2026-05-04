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
                "INSERT INTO libraries (name, kind, root_path) VALUES (?, ?, ?)",
                (args.name, args.kind, str(root)),
            )
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"Added library {args.name!r} ({args.kind}) at {root}")
    return 0


def cmd_list_libraries(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_schema(conn)
    rows = conn.execute(
        "SELECT id, name, kind, root_path FROM libraries ORDER BY id"
    ).fetchall()
    if not rows:
        print("No libraries.")
        return 0
    for r in rows:
        print(f"{r['id']:>3}  {r['kind']:<10}  {r['name']:<20}  {r['root_path']}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    db.init_schema(conn)
    if args.name:
        rows = conn.execute(
            "SELECT id, name, kind, root_path FROM libraries WHERE name = ?",
            (args.name,),
        ).fetchall()
        if not rows:
            print(f"No library named {args.name!r}.", file=sys.stderr)
            return 1
    else:
        rows = conn.execute(
            "SELECT id, name, kind, root_path FROM libraries ORDER BY id"
        ).fetchall()
        if not rows:
            print("No libraries to scan. Use `add-library` first.", file=sys.stderr)
            return 1
    total = scanner.ScanResult()
    for r in rows:
        print(f"Scanning {r['name']} ({r['kind']}) at {r['root_path']} ...")
        result = scanner.scan_library(conn, r["id"], Path(r["root_path"]), r["kind"])
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


def cmd_serve(args: argparse.Namespace) -> int:
    print("cuttlefish serve: not yet implemented", file=sys.stderr)
    return 1


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

    p = sub.add_parser("add-library", help="Register a library root.")
    p.add_argument("name")
    p.add_argument("root")
    p.add_argument("--kind", required=True, choices=("movies", "tv", "audiobooks"))
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
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
