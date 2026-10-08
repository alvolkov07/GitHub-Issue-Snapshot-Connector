"""Command-line wrapper for the GitHub issue snapshot connector.

Usage:
    python cli.py import owner/name [--db PATH]
    python cli.py read owner/name   [--db PATH]

Prints the connector's result as JSON. Exit code is 0 on success and 1 on
failure. All logic lives in connector.py.
"""

import argparse
import json
import sys

import connector


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("repo", help="repository as owner/name, e.g. octocat/Hello-World")
    common.add_argument(
        "--db",
        default=connector.DEFAULT_DB_PATH,
        help="path to the SQLite file (default: %(default)s)",
    )

    parser = argparse.ArgumentParser(description="GitHub issue snapshot connector")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "import", parents=[common], help="fetch open issues from GitHub and save them"
    )
    subcommands.add_parser(
        "read", parents=[common], help="print saved issues (no GitHub call)"
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "import":
        result = connector.import_issues(args.repo, args.db)
    else:
        result = connector.read_issues(args.repo, args.db)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())