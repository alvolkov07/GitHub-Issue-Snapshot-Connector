"""GitHub issue snapshot connector.

Imports open issues from a public GitHub repository into a local SQLite
database and reads them back without calling GitHub.

Public functions (both return a JSON-compatible dict):

    import_issues(repo, db_path) -> dict
    read_issues(repo, db_path)   -> dict

Success:  {"ok": True, "repository": "owner/name", "count": N, ...}
Failure:  {"ok": False, "error": {"code": "...", "message": "..."}}

Error codes:
    invalid_repository  input is not a valid "owner/name"
    not_found           GitHub returned 404
    rate_limited        GitHub returned 403 or 429
    api_error           any other bad status, or an unexpected response
    network_error       could not reach GitHub, or the connection broke mid-response
    timeout             GitHub did not answer in time
    database_error      SQLite could not open or write the file

Only standard-library modules are used, so there is nothing to install.
"""

import http.client
import json
import os
import re
import socket
import sqlite3
import urllib.error
import urllib.request
from contextlib import closing

# Database file used when the caller does not pass one. Can be overridden with
# the GITHUB_ISSUES_DB environment variable or by passing db_path directly.
DEFAULT_DB_PATH = os.environ.get("GITHUB_ISSUES_DB", "issues.db")

# One request only: the first page, open issues, up to 100 items.
API_URL = "https://api.github.com/repos/{owner}/{name}/issues?state=open&per_page=100"
REQUEST_TIMEOUT_SECONDS = 10

# GitHub naming rules (simplified): owners are letters, digits and hyphens;
# repository names also allow dots and underscores. Used with fullmatch(): a "$"
# anchor would also accept a trailing newline, so "octocat\n" would slip through.
_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")

# Largest value SQLite's INTEGER column can hold.
_MAX_ISSUE_NUMBER = 2 ** 63 - 1

# The SQL table will consist of columns containing the repo name, issue number, title, URL. The combination of repository and number will be the database's primary key.  

_SCHEMA = """ 
CREATE TABLE IF NOT EXISTS issues (
    repository TEXT    NOT NULL,
    number     INTEGER NOT NULL,
    title      TEXT    NOT NULL,
    url        TEXT    NOT NULL,
    PRIMARY KEY (repository, number)
)
"""

# Insert a new row, or update title and url if (repo, number) exists. Based on the database's primary key condition. 
_UPSERT = """
INSERT INTO issues (repository, number, title, url)
VALUES (?, ?, ?, ?) 
ON CONFLICT(repository, number)
DO UPDATE SET title = excluded.title, url = excluded.url
"""
#VALUES (?, ?, ?, ?) -> question marks are placeholders

class ConnectorError(Exception):
    """An expected failure, carrying a stable code and a readable message."""

    def __init__(self, code, message):
        super().__init__(message) # call the Exception constructor and initialize members
        self.code = code 
        self.message = message


# Validate repo name and owner 

def parse_repo(repo):
    """Validate "owner/name" and return (owner, name), lowercased.

    GitHub treats repository names as case-insensitive, so lowercasing stops
    "Octocat/Hello-World" and "octocat/hello-world" being stored as two repos.
    Raises ConnectorError("invalid_repository") for bad input.
    """
    bad = ConnectorError(
        "invalid_repository",
        f"{repo!r} is not a valid repository. Use the form owner/name, "
        "for example octocat/Hello-World.",
    )
    if not isinstance(repo, str): # the passed parameter is not a string detailing the repository
        raise bad
    parts = repo.strip().split("/") 
    if len(parts) != 2: # must be exactly "owner/name"
        raise bad
    owner, name = parts # isolate owner and name from the repository string
    if not _OWNER_RE.fullmatch(owner) or not _NAME_RE.fullmatch(name) or name in (".", ".."): #compare owner and name against the allowed regex rules. Then, block "." and ".." to reject invalid names
        raise bad
    return owner.lower(), name.lower() #return the lowecase strings for owner and name. Helps with case-insensitive repository handling.


# GitHub API issue fetcher and reader

def fetch_open_issues(owner, name):
    """Fetch one page of open issues. Returns the raw list from GitHub.

    The list can still contain pull requests; extract_issues() removes them.
    Raises ConnectorError for HTTP errors, network problems and bad responses.
    """
    repo_label = f"{owner}/{name}"  
    request = urllib.request.Request( #request a list of open issues from the GitHub API
        API_URL.format(owner=owner, name=name),
        headers={
            "Accept": "application/vnd.github+json", #default acceptance header for GitHub API
            "User-Agent": "github-issue-snapshot-connector", #required by GitHub API
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response: 
            body = response.read()
    except urllib.error.HTTPError as err:
        raise _error_for_status(err.code, repo_label) from err
    except urllib.error.URLError as err:
        # A timeout while connecting arrives wrapped inside a URLError.
        if isinstance(err.reason, (TimeoutError, socket.timeout)): # check if the error is due to a timeout
            raise _timeout_error() from err
        raise ConnectorError(
            "network_error", f"Could not reach GitHub: {err.reason}"
        ) from err
    except (TimeoutError, socket.timeout) as err:
        raise _timeout_error() from err
    except OSError as err:  # connection reset while reading
        raise ConnectorError("network_error", f"Network failure: {err}") from err
    except http.client.HTTPException as err:
        # Not an OSError: truncated body (IncompleteRead), garbled status line, etc.
        raise ConnectorError(
            "network_error",
            f"The connection to GitHub broke ({type(err).__name__}: {err}).",
        ) from err
    except ValueError as err:  # the request itself could not be built or sent
        raise ConnectorError("api_error", f"The request could not be sent: {err}") from err

    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as err:  # RecursionError: absurdly nested JSON
        raise ConnectorError(
            "api_error", "GitHub returned a response that was not valid JSON."
        ) from err
    if not isinstance(payload, list): #must return a list of issues
        raise ConnectorError(
            "api_error", "GitHub returned an unexpected response (expected a list)."
        )
    return payload


def _timeout_error():  # raise a timeout error if the API request went beyond REQUEST_TIMEOUT_SECONDS
    return ConnectorError(
        "timeout",
        f"GitHub did not respond within {REQUEST_TIMEOUT_SECONDS} seconds.",
    )


def _error_for_status(status, repo_label): 
    """Translate an HTTP status code into a ConnectorError."""
    if status == 404: #404 means repository was not found
        return ConnectorError("not_found", f"Repository {repo_label} was not found.")
    if status in (403, 429): #403 and 429 means we have been rate limited or forbidden
        return ConnectorError( 
            "rate_limited",
            f"GitHub refused the request (HTTP {status}). You may have hit the non-token "
            "rate limit (60 requests per hour).",
        )
    if status == 410:
        return ConnectorError(
            "api_error", f"Issues are disabled for repository {repo_label} (HTTP 410)."
        )
    return ConnectorError("api_error", f"GitHub returned HTTP {status}.") #Return a general API error message for other status codes


def extract_issues(raw_items):
    """Drop pull requests and keep only number, title and url.

    GitHub's issues endpoint also returns pull requests; they are the items
    that have a "pull_request" key.
    """
    issues = []
    for item in raw_items:
        if not isinstance(item, dict): #the issues in raw_items must be key-value pairs.
            raise ConnectorError("api_error", "GitHub returned an unexpected item.")
        if "pull_request" in item:
            continue
        number, title, url = item.get("number"), item.get("title"), item.get("html_url")
        # Strict on purpose: int() and str() would quietly turn 2.5 into 2 and
        # None into "None", and save that as if it were real data.
        if not (
            type(number) is int  # excludes True/False
            and 1 <= number <= _MAX_ISSUE_NUMBER
            and _is_storable_text(title)
            and _is_storable_text(url)
        ):
            raise ConnectorError(
                "api_error", "GitHub returned an issue with missing or malformed fields."
            )
        issues.append({"number": number, "title": title, "url": url})
    return issues


def _is_storable_text(value):
    """True for a str that SQLite can store (no unpaired surrogates)."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True

# Database

def _check_db_path(db_path):
    """Reject paths that would fail confusingly or lose data silently.

    sqlite3.connect("") quietly opens a throwaway temporary database, so an
    import would report success and then save nothing.
    """
    if not isinstance(db_path, (str, os.PathLike)) or not str(db_path).strip():
        raise ConnectorError(
            "database_error",
            f"Invalid database path {db_path!r}. Pass a file path such as 'issues.db'.",
        )


def init_db(db_path):
    """Open the database file (creating it if needed) and ensure the table exists.

    Returns an open connection. The caller is responsible for closing it.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_SCHEMA)
        conn.commit()
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def save_issues(conn, repository, issues):
    """Upsert issues for one repository in a single transaction."""
    rows = [(repository, i["number"], i["title"], i["url"]) for i in issues]
    with conn:  # commits on success, rolls back on error
        conn.executemany(_UPSERT, rows)

# Public interface

def import_issues(repo, db_path=DEFAULT_DB_PATH):
    """Fetch open issues for "owner/name" and save them to SQLite.

    Importing the same repository again updates existing rows instead of
    creating duplicates. Returns a JSON-compatible dict.
    """
    try:
        owner, name = parse_repo(repo)
        repository = f"{owner}/{name}"
        _check_db_path(db_path)  # before the network call, so no request is wasted
        issues = extract_issues(fetch_open_issues(owner, name))
        # The database is opened only after a successful fetch, so a failed
        # request does not create or touch the file.
        with closing(init_db(db_path)) as conn:
            save_issues(conn, repository, issues)
        return {"ok": True, "repository": repository, "count": len(issues)}
    except ConnectorError as err:
        return _failure(err.code, err.message)
    except sqlite3.Error as err:
        return _failure("database_error", f"Could not use database {str(db_path)!r}: {err}")


def read_issues(repo, db_path=DEFAULT_DB_PATH):
    """Return saved issues for "owner/name" from SQLite. Never calls GitHub.

    A repository with nothing saved yet returns ok=True with count 0.
    """
    try:
        owner, name = parse_repo(repo)
        repository = f"{owner}/{name}"
        _check_db_path(db_path)
        with closing(init_db(db_path)) as conn:
            rows = conn.execute(
                "SELECT number, title, url FROM issues "
                "WHERE repository = ? ORDER BY number",
                (repository,),
            ).fetchall()
        issues = [{"number": n, "title": t, "url": u} for n, t, u in rows]
        return {
            "ok": True,
            "repository": repository,
            "count": len(issues),
            "issues": issues,
        }
    except ConnectorError as err:
        return _failure(err.code, err.message)
    except sqlite3.Error as err:
        return _failure("database_error", f"Could not use database {str(db_path)!r}: {err}")


def _failure(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}