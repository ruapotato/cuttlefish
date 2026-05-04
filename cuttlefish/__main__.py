import argparse
import sys

from cuttlefish import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cuttlefish",
        description="Self-hosted media server (work in progress).",
    )
    parser.add_argument("--version", action="version", version=f"cuttlefish {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.add_parser("serve", help="Run the cuttlefish web server (not yet implemented).")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "serve":
        print("cuttlefish serve: not yet implemented", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
