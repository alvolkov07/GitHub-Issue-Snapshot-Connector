## Main components
```
connector.py        all core logic: validation, GitHub fetch, SQLite storage importing / reading issues, case-checking. 
cli.py              thin command-line wrapper for printing JSON files
demo.py             File for recorded demo using calls from a real GitHub repository
test_connector.py   automated tests (GitHub API mocked)
```

## Interface
```
import_issues(repo: str, db_path: str = DEFAULT_DB_PATH) -> dict
read_issues(repo: str, db_path: str = DEFAULT_DB_PATH) -> dict
Success: {"ok": true, "repository": "owner/name", "count": N} (read also returns "issues": [...]). Failure: {"ok": false, "error": {"code": "...", "message": "..."}}.
```
import_issues and read_issues are exposed as reusable functions. They both return dictionaries so we can instantly convert them into JSON files, since the data is already in key-value pairs. 

cli.py is a thin wrapper around the connector's public functions to print the result as a JSON file. Because the core layout of cli.py focuses on command line parsing, string formatting and exit code returns. It is contained in a separate file because it doesn't contribute to the connector's core programming logic


## Data flow (API to database)
```
import_issues("owner/name", db_path)
parse_repo -> validate and lowercase
fetch_open_issues -> GET /repos/{owner}/{name}/issues?state=open&per_page=100 
extract_issues -> drop items with a "pull_request" key; keep number, title, html_url
save_issues -> upsert into SQLite
read_issues("owner/name", db_path)
```
parse_repo -> SELECT from SQLite -> result dict (no GitHub call).

## Database schema
Imported issues are stored in a database capturing the data of the issue's repository name, number, title, and url. The primary key uses (repository, number) because the same issue numbers could exist in two different repositories. 
```
CREATE TABLE IF NOT EXISTS issues (
    repository TEXT    NOT NULL,
    number     INTEGER NOT NULL,
    title      TEXT    NOT NULL,
    url        TEXT    NOT NULL,
    PRIMARY KEY (repository, number)
);
```

## Configuration
- Database path: the db_path argument (CLI: --db). Default issues.db, or the GITHUB_ISSUES_DB environment variable. Empty/invalid database paths will result in a database_error.
- Authentication: none. Public repositories only. Unauthenticated limit: 60 requests per hour.
- Request timeout: 10 seconds. Page size: max value of per_page=100 

## Error handling
```
invalid_repository -> The repository argument isn't a valid owner/name format.
not_found -> GitHub returns 404
rate_limited -> GitHub returns 403 or 429. For simplicity, we treat all 403 errors as possible rate limits, even though 403 could be raised by other causes. 
api_error -> GitHub returns an unsuccessful status, malformed issues, or an invalid JSON file. 
network_error -> The request did not reach GitHub, or connection failed while fetching.
timeout	-> Request exceeded the timeout limit. 
database_error -> Database path is invalid
```

In the case of a failed import, the previously saved issue data remains unchanged, since the connector needs to validate the repo, database path, and correctly fetch from GitHub before opening the database. In the case of a database fail, the import will be rolled back. 


## Design Tradeoffs

I chose to use the urllib instead of the requests package to keep the setup process more simple. While requests is an external dependency and also works well for HTTP requests, this connector requires a very simple API request without pagination that made urllib a more favorable choice for me. This choice comes with a tradeoff because using Python's standard libraries means that we will need to manually write API requests and handlers. Additionally, if we wanted to expand the connector to deal with more complex requests external dependencies would become more useful because they internally handle HTTP requests.
