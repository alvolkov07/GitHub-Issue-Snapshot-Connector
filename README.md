# GitHub-Issue-Snapshot-Connector

A Python-based issue snapshot connector for Github that imports a page of issues from a public GitHub repository, stores them in an SQLite database, and reads back the saved issues without calling GitHub. “The database persists across runs unless --reset-db is used.

## Prerequisites

- **Python 3** 
- No GitHub account or token is needed for public repositories, but you will be limited to 60 requests per hour.

No dependencies were used. The project uses only Python's standard library, most notably:
`urllib` (HTTP), `sqlite3` (database), `json`, `argparse` (command line), `re`, `socket`,
`http.client`, and `unittest` (tests).

## Local setup

```
git clone https://github.com/alvolkov07/GitHub-Issue-Snapshot-Connector
cd GitHub-Issue-Snapshot-Connector
```

## Usage

Import open issues (one page, pull requests excluded):

```
python3 cli.py import octocat/Hello-World
```

Read the saved issues (no GitHub call):

```
python3 cli.py read octocat/Hello-World
```

Choose the database file with `--db`, for example `python3 cli.py import octocat/Hello-World --db my.db`.
If you do not, `issues.db` is used, or the path in the `GITHUB_ISSUES_DB` environment variable.

Both commands print JSON. Success has `"ok": true`. Failure has `"ok": false` and an
`error` with a `code` and a `message`. The exit code is 0 on success and 1 on failure.

Using it from Python:

```python
from connector import import_issues, read_issues

import_issues("owner/name", "my.db")   # returns a dict
read_issues("owner/name", "my.db")     # returns a dict
```

## Run the tests

There are several tests you can run. For all the individual tests and the randomized test call, run:

```
python3 -m unittest -v
```

Expected terminal output after successfully running all tests: `OK`. The tests mock the GitHub API, so
they run offline and use none of your GitHub request limit. 


Optional randomized test (random import/read/failure sequences checked against a model):

```
FUZZ_ITERATIONS=5000 python3 -m unittest -v test_connector.RandomizedTests
FUZZ_SEED=123456 python3 -m unittest -v test_connector.RandomizedTests
```

The seed is shown in any failure message. Rerun with `FUZZ_SEED=<that number>` to replay it.
(Windows PowerShell: set the variable on its own line first, e.g. `$env:FUZZ_SEED=123456`.)

## Demo (real GitHub API)

```
python3 demo.py --pause
```

It imports a repository, reads it back, imports it again to show there are no duplicates, then triggers a real 404. Add `owner/name` to use another repository. Each run uses 3 of GitHub's 60 unauthenticated requests per hour. 

The demo preserves the database by defaut. Otherwise, add --reset-db after calling demo.py to wipe the database. 

## Example inputs and outputs

**Import**

```
python3 cli.py import octocat/hello-world

{
  "ok": true,
  "repository": "octocat/hello-world",
  "count": 90,
  "issues":[
    {
      "number": 11353,
      "title": "Test Issue 1790833325",
      "url": "https://github.com/octocat/Hello-World/issues/11353"
    }
  ]
}
```

Note that the exact count of issues change over time as the repository is updated.

**Read**

Note that this will print out a list every issue in the database, but single example JSON is shown below:

```
python3 cli.py read octocat/hello-world

...
{
      "number": 11468,
      "title": "Creating issue with GraphQL",
      "url": "https://github.com/octocat/Hello-World/issues/11468"
}

...
```

**Error case**

```
python3 cli.py import octocat/this-is-a-fake-repo-test

{
  "ok": false,
  "error": {
    "code": "not_found",
    "message": "Repository octocat/this-is-a-fake-repo-test was not found."
  }
}
```

## Tools used

My primary AI agent of choice was Claude, which I used to research about how to create SQLite database schemas, isolate the important sections I needed to read from GitHub's REST API and generate randomized an in-depth test class. Additionally, I used it in my code to assist with debugging hidden edge cases related to API calling and assertion misreadings, explaining the methodology behind designing core functions (like import and read) in connector.py, breaking down several problems into smaller subtasks, and formatting explicit error messages for executable and API error handling. 

Additionally, I Used: 

| Python 3 | Programming language of use |
| SQLite (`sqlite3`) | For database schema, querying and reading the stored issues without calling GitHub again |
| GitHub REST API | For getting public repository issue data |
| Git and GitHub | For version control and uploading code |
| VS Code/Terminal | Running and viewing application tests and live calls |
| QuickTime | For the screen recording |


## One unfamiliar problem I solved with AI

One new and unfamiliar problem that I solved with AI was designing efficient tests to check that the database updater truly ignored duplicates. I used Claude to filter through several credible sources (Including Harvard CS50 and SQLite's documentation) to research about UPSERT. After reading from several results about how the statement functions, I wrote my initial implementation and then asked Claude to run a randomized test loop to test the limits of my attempt, and then used its suggested feedback to implement not only a statement bug I missed, but add additional tests to check that UPSERT was able to handle repetitive duplicates under stress. However, also I rejected one of Claude's propositions (to expand the SQLite schema with more rows) aimed to accomodate for my initial implementation, seeing that it would add more complexity and possible issues to my database rather than fixes.


## Troubleshooting

- **`Command not found: Python` on macOS** In the terminal, replace python with python3 for all executable commands (This is what I used to resolve the terminal error).
- **`rate_limited`**: GitHub allows 60 unauthenticated requests per hour. Wait for the reset, then retry.


