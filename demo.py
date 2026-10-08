"""Demo for the GitHub issue snapshot connector (real GitHub API calls).

Runs the main calls printing each result as JSON:

    1. import a repository          (real request to GitHub)
    2. read it back from SQLite     (no GitHub call)
    3. import it again              (real request) -> row count must not change
    4. import a repository that does not exist -> real 404 -> "not_found"

usage: 
    python3 demo.py                              # default repo
    python3 demo.py owner/name                   # any public repo
    python3 demo.py owner/name --pause           # wait for Enter between steps
    python3 demo.py --reset-db                   # delete the old database first
    python3 cli.py read owner/name --db PATH    # read the saved database to see if it survived program restart.

Uses 3 of GitHub's 60 unauthenticated requests per hour on each run.
"""

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing

import connector

DEFAULT_REPO = "octocat/Hello-World"
MISSING_REPO = "octocat/this-repo-does-not-exist-12345"


def show(title, value, pause):
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2))
    if pause:
        input("\n[press Enter for the next step] ")


def row_counts(db_path, repo):
    """Count rows straight from SQLite, bypassing the connector."""
    repo = repo.lower()
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT number) FROM issues WHERE repository = ?",
            (repo,),
        ).fetchone()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Connector demo")
    parser.add_argument("repo", nargs="?", default=DEFAULT_REPO)
    parser.add_argument("--db", default="demo.db", help="database file (kept between runs; add --reset-db to start fresh)")
    parser.add_argument("--pause", action="store_true", help="wait for Enter between steps")
    parser.add_argument("--reset-db", action="store_true", help="delete the database file before starting") #option to clear db from past demo
    args = parser.parse_args(argv)

    # If prompted, from an empty database.
    if args.reset_db and os.path.exists(args.db):
        os.remove(args.db)

    first = connector.import_issues(args.repo, args.db)
    show(f"1. Import {args.repo} (real GitHub API call)", first, args.pause)
    if not first["ok"]:
        print("\nThe first import failed, so the rest of the demo cannot continue.")
        print("If the code is rate_limited, wait for the reset (about an hour) and retry.")
        return 1

    saved = connector.read_issues(args.repo, args.db)
    show(f"2. Read from SQLite: no GitHub call ({saved['count']})", saved, args.pause)

    rows_before, _ = row_counts(args.db, args.repo)
    second = connector.import_issues(args.repo, args.db)
    show("3a. Import the same repository again (real GitHub API call)", second, args.pause)
    rows, distinct = row_counts(args.db, args.repo)
    print(f"\nrows before 2nd import: {rows_before}   rows after: {rows}   "
          f"distinct issue numbers: {distinct}")
    # Rows left from an earlier run (issues since closed) are expected, so the
    # duplicate check compares rows to distinct numbers, not to this import's count.
    print("-> NO DUPLICATES" if rows == distinct else "-> DUPLICATES FOUND")
    if args.pause:
        input("\n[press Enter for the next step] ")

    missing = connector.import_issues(MISSING_REPO, args.db)
    show(f"4. Error case: {MISSING_REPO} (real 404 from GitHub)", missing, False)
    return 0


if __name__ == "__main__":
    sys.exit(main())