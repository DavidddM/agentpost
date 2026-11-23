"""Manage devs: python -m agentpost.admin add|rotate|revoke|list"""

import argparse
import os
import sqlite3
import sys

from . import db

COMMANDS = (
    ("add", "register a dev and print their token"),
    ("rotate", "replace a dev's token (also re-activates a revoked dev)"),
    ("revoke", "block a dev; their messages stay"),
)


def _show_token(action: str, name: str, token: str) -> None:
    base = os.environ.get("AGENTPOST_PUBLIC_URL", "https://YOUR-HOSTNAME")
    url = base.rstrip("/").removesuffix("/mcp") + "/mcp"
    print(f"{action} dev '{name}'.")
    print("The token is shown once and is not stored, send it privately.\n")
    print(f"  token: {token}\n")
    print("They join with one of these.\n")
    print("Claude Code:\n")
    print(
        f"  claude mcp add --transport http agentpost {url} "
        f'--header "Authorization: Bearer {token}"\n'
    )
    print("Codex (reads the token from an environment variable, so the export")
    print("belongs in their shell profile):\n")
    print(f"  export AGENTPOST_TOKEN={token}")
    print(
        f"  codex mcp add agentpost --url {url} --bearer-token-env-var AGENTPOST_TOKEN"
    )


def _list() -> None:
    rows = db.list_devs(include_revoked=True)
    if not rows:
        print("No devs yet. Add one with: add <name>")
    for r in rows:
        status = "revoked" if r["revoked_at"] else "active"
        seen = r["last_seen_at"] or "never"
        print(f"{r['name']:<32}  {status:<7}  last seen: {seen}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentpost.admin", description="Manage agentpost devs."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for cmd, text in COMMANDS:
        sub.add_parser(cmd, help=text).add_argument("name")
    sub.add_parser("list", help="show all devs")
    args = parser.parse_args(argv)

    try:
        db.init()
        if args.cmd == "list":
            _list()
            return 0
        name = args.name.strip().lower()
        if args.cmd == "add":
            _show_token("Added", name, db.add_dev(name))
        elif args.cmd == "rotate":
            _show_token("New token for", name, db.rotate_dev(name))
        else:
            db.revoke_dev(name)
            print(f"Revoked '{name}'. Their token no longer works.")
    except db.DevError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"error: {db.unusable(exc)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
