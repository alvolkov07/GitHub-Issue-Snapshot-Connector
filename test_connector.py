"""Automated tests for connector.py. The GitHub API is always mocked.

Run with:  python -m unittest -v 
If the terminal returns a command not found error, use the the following: python3 -m unittest -v.
"""

import http.client
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import closing
from unittest import mock
import random

import connector


def raw_issue(number, title, pull_request=False):
    """Build an item shaped like GitHub's issues endpoint returns."""
    item = {
        "number": number,
        "title": title,
        "state": "open",
        "html_url": f"https://github.com/octocat/hello-world/issues/{number}",
    }
    if pull_request:
        item["pull_request"] = {"url": "https://api.github.com/example"}
    return item


class FakeResponse:
    """Stands in for the object urlopen() returns."""

    def __init__(self, body=b"", read_error=None):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self._read_error = read_error

    def read(self):
        if self._read_error:
            raise self._read_error
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def http_error(status):
    return urllib.error.HTTPError("https://api.github.com/x", status, "err", {}, io.BytesIO(b""))


class BaseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = os.path.join(tmp.name, "test.db")

    def row_count(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]

    def import_with(self, raw_items, repo="octocat/Hello-World"):
        with mock.patch("connector.fetch_open_issues", return_value=raw_items):
            return connector.import_issues(repo, self.db_path)


class ImportAndReadTests(BaseTest):
    def test_import_then_read_excludes_pull_requests(self):
        result = self.import_with(
            [raw_issue(1, "First"), raw_issue(2, "A pull request", pull_request=True), raw_issue(3, "Third")]
        )
        self.assertEqual(result, {"ok": True, "repository": "octocat/hello-world", "count": 2})

        read = connector.read_issues("octocat/Hello-World", self.db_path)
        self.assertTrue(read["ok"])
        self.assertEqual(read["count"], 2)
        self.assertEqual([i["number"] for i in read["issues"]], [1, 3])
        self.assertEqual(
            read["issues"][0],
            {"number": 1, "title": "First", "url": "https://github.com/octocat/hello-world/issues/1"},
        )
        json.dumps(read)  # must be JSON-compatible

    def test_repeated_import_updates_without_duplicates(self):
        self.import_with([raw_issue(1, "Old title"), raw_issue(2, "Second")])
        self.assertEqual(self.row_count(), 2)

        self.import_with([raw_issue(1, "New title"), raw_issue(2, "Second"), raw_issue(3, "Third")])
        self.assertEqual(self.row_count(), 3)  # 2 updated in place, 1 added

        issues = connector.read_issues("octocat/hello-world", self.db_path)["issues"]
        self.assertEqual(issues[0]["title"], "New title")

    def test_same_issue_number_in_two_repositories_is_kept_separate(self):
        self.import_with([raw_issue(1, "In repo A")], repo="octocat/repo-a")
        self.import_with([raw_issue(1, "In repo B")], repo="octocat/repo-b")
        self.assertEqual(self.row_count(), 2)
        a = connector.read_issues("octocat/repo-a", self.db_path)["issues"]
        b = connector.read_issues("octocat/repo-b", self.db_path)["issues"]
        self.assertEqual(a[0]["title"], "In repo A")
        self.assertEqual(b[0]["title"], "In repo B")

    def test_repository_name_is_case_insensitive(self):
        self.import_with([raw_issue(1, "One")], repo="Octocat/Hello-World")
        self.import_with([raw_issue(1, "One")], repo="octocat/hello-world")
        self.assertEqual(self.row_count(), 1)

    def test_read_unknown_repository_returns_empty_list(self):
        result = connector.read_issues("octocat/never-imported", self.db_path)
        self.assertEqual(
            result,
            {"ok": True, "repository": "octocat/never-imported", "count": 0, "issues": []},
        )

    def test_read_makes_no_network_call(self):
        self.import_with([raw_issue(1, "One")])
        with mock.patch("connector.urllib.request.urlopen", side_effect=AssertionError("network used")) as net, \
             mock.patch("connector.fetch_open_issues", side_effect=AssertionError("fetch used")):
            result = connector.read_issues("octocat/hello-world", self.db_path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        net.assert_not_called()

    def test_data_persists_after_restart(self):
        """A brand-new Python process can read what was imported earlier."""
        self.import_with([raw_issue(1, "Persisted")])
        code = (
            "import json, sys, connector;"
            "print(json.dumps(connector.read_issues('octocat/hello-world', sys.argv[1])))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code, self.db_path],
            cwd=os.path.dirname(os.path.abspath(connector.__file__)),
            capture_output=True, text=True, check=True,
        ).stdout
        result = json.loads(out)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["issues"][0]["title"], "Persisted")


class ErrorTests(BaseTest):
    def test_api_failure_returns_error_and_saves_nothing(self):
        failure = connector.ConnectorError("not_found", "Repository octocat/nope was not found.")
        with mock.patch("connector.fetch_open_issues", side_effect=failure):
            result = connector.import_issues("octocat/nope", self.db_path)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "not_found")
        self.assertIn("octocat/nope", result["error"]["message"])
        self.assertFalse(os.path.exists(self.db_path))  # failed fetch leaves no file

    def test_failed_import_does_not_remove_saved_data(self):
        self.import_with([raw_issue(1, "Keep me")])
        failure = connector.ConnectorError("rate_limited", "slow down")
        with mock.patch("connector.fetch_open_issues", side_effect=failure):
            connector.import_issues("octocat/hello-world", self.db_path)
        self.assertEqual(connector.read_issues("octocat/hello-world", self.db_path)["count"], 1)

    def test_http_and_network_failures_map_to_error_codes(self):
        cases = [
            (http_error(404), "not_found"),
            (http_error(403), "rate_limited"),
            (http_error(429), "rate_limited"),
            (http_error(410), "api_error"),
            (http_error(500), "api_error"),
            (urllib.error.URLError("connection refused"), "network_error"),
            (urllib.error.URLError(socket.timeout()), "timeout"),
            (TimeoutError(), "timeout"),
            (ConnectionResetError(), "network_error"),
            (http.client.IncompleteRead(b"ab", 5), "network_error"),
            (http.client.BadStatusLine("garbage"), "network_error"),
            (ValueError("bad request"), "api_error"),
        ]
        for raised, expected_code in cases:
            with self.subTest(raised=repr(raised)):
                with mock.patch("connector.urllib.request.urlopen", side_effect=raised):
                    result = connector.import_issues("octocat/hello-world", self.db_path)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], expected_code)
                self.assertTrue(result["error"]["message"])

    def test_invalid_json_from_api_is_an_api_error(self):
        with mock.patch("connector.urllib.request.urlopen", return_value=FakeResponse(b"not json")):
            result = connector.import_issues("octocat/hello-world", self.db_path)
        self.assertEqual(result["error"]["code"], "api_error")

    def test_unexpected_json_shape_is_an_api_error(self):
        with mock.patch("connector.urllib.request.urlopen", return_value=FakeResponse({"message": "oops"})):
            result = connector.import_issues("octocat/hello-world", self.db_path)
        self.assertEqual(result["error"]["code"], "api_error")

    def test_truncated_response_body_is_a_network_error(self):
        broken = FakeResponse(read_error=http.client.IncompleteRead(b"[{", 500))
        with mock.patch("connector.urllib.request.urlopen", return_value=broken):
            result = connector.import_issues("octocat/hello-world", self.db_path)
        self.assertEqual(result["error"]["code"], "network_error")

    def test_deeply_nested_json_is_an_api_error(self):
        body = b"[" * 100000 + b"]" * 100000
        with mock.patch("connector.urllib.request.urlopen", return_value=FakeResponse(body)):
            result = connector.import_issues("octocat/hello-world", self.db_path)
        self.assertEqual(result["error"]["code"], "api_error")

    def test_malformed_issues_are_rejected_and_nothing_is_saved(self):
        good = {"number": 1, "title": "t", "html_url": "u"}
        bad_items = [
            {**good, "number": "5"},          # string, not an int
            {**good, "number": 2.5},          # would silently become 2
            {**good, "number": True},         # bool is an int subclass
            {**good, "number": 0},
            {**good, "number": 10 ** 30},     # too big for SQLite INTEGER
            {**good, "title": None},          # would be saved as the text "None"
            {**good, "title": 7},
            {**good, "title": "bad\ud800title"},   # unpaired surrogate
            {**good, "html_url": None},
            {"number": 1, "title": "t"},      # url missing
            "not an object",
        ]
        for bad_item in bad_items:
            with self.subTest(item=repr(bad_item)[:60]):
                if os.path.exists(self.db_path):
                    os.remove(self.db_path)
                result = self.import_with([good, bad_item])
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "api_error")
                self.assertFalse(os.path.exists(self.db_path))  # whole page rejected

    def test_pull_requests_are_skipped_before_their_fields_are_checked(self):
        result = self.import_with([{"pull_request": {"url": "x"}}])
        self.assertEqual(result, {"ok": True, "repository": "octocat/hello-world", "count": 0})

    def test_empty_or_wrong_type_database_path_is_rejected_before_any_request(self):
        for bad_path in ["", "   ", None, 42]:
            with self.subTest(db_path=bad_path):
                with mock.patch("connector.fetch_open_issues", side_effect=AssertionError("must not fetch")):
                    for func in (connector.import_issues, connector.read_issues):
                        result = func("octocat/hello-world", bad_path)
                        self.assertFalse(result["ok"])
                        self.assertEqual(result["error"]["code"], "database_error")

    def test_rate_limit_message_is_readable(self):
        message = connector._error_for_status(403, "octocat/hello-world").message
        self.assertIn("non-token rate limit", message)  # was "non-tokenrate limit"

    def test_unusable_database_path_returns_database_error(self):
        # The folder does not exist, so SQLite cannot create the file.
        bad_path = os.path.join(os.path.dirname(self.db_path), "missing-folder", "x.db")

        read = connector.read_issues("octocat/hello-world", bad_path)
        self.assertFalse(read["ok"])
        self.assertEqual(read["error"]["code"], "database_error")

        with mock.patch("connector.fetch_open_issues", return_value=[raw_issue(1, "One")]):
            imported = connector.import_issues("octocat/hello-world", bad_path)
        self.assertFalse(imported["ok"])
        self.assertEqual(imported["error"]["code"], "database_error")


class HttpRequestTests(BaseTest):
    def test_request_asks_for_open_issues_and_filters_pull_requests(self):
        payload = [raw_issue(1, "Issue"), raw_issue(2, "PR", pull_request=True)]
        with mock.patch("connector.urllib.request.urlopen", return_value=FakeResponse(payload)) as urlopen:
            result = connector.import_issues("Octocat/Hello-World", self.db_path)
        request = urlopen.call_args[0][0]
        self.assertIn("/repos/octocat/hello-world/issues", request.full_url)
        self.assertIn("state=open", request.full_url)
        self.assertEqual(request.get_header("User-agent"), "github-issue-snapshot-connector")
        self.assertEqual(urlopen.call_args[1]["timeout"], connector.REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(result["count"], 1)


class ValidationTests(BaseTest):
    def test_invalid_repositories_are_rejected_without_any_request(self):
        bad_inputs = ["", "octocat", "octocat/", "/repo", "a/b/c", "octo cat/repo",
                      "-bad/repo", "../etc", "octocat/..", 123, None,
                      "octocat\n/hello-world", "octo\r\n/x", "octocat/hello\n/world"]
        for bad in bad_inputs:
            with self.subTest(repo=bad):
                with mock.patch("connector.fetch_open_issues", side_effect=AssertionError("must not fetch")):
                    for func in (connector.import_issues, connector.read_issues):
                        result = func(bad, self.db_path)
                        self.assertFalse(result["ok"])
                        self.assertEqual(result["error"]["code"], "invalid_repository")

    def test_valid_repository_forms_are_accepted(self): #changed test_valid_repo_forms to use a better assertion 
        expected = [("octocat/hello-world", "octocat/hello-world"),  ("  octocat/hello-world  ", "octocat/hello-world"), ("a/b", "a/b"), ("my-org/my.repo_name", "my-org/my.repo_name")]

        for good, expected_repo in expected:
            with self.subTest(repo=good):
                owner, name = connector.parse_repo(good)
                self.assertEqual(f"{owner}/{name}", expected_repo)

# Repetitive, randomized test for the connector.
class RandomizedTests(BaseTest): 
    """Random sequences of imports, failed imports and reads, checked against
    a plain Python dict that acts as the expected state of the database.

    Every assertion message includes the seed, so any failure can be replayed
    exactly with FUZZ_SEED=<seed>.

    If you want to dictate how many randomized tests to run, include the FUZZ_ITERATIONS environment variable.
    Example: FUZZ_ITERATIONS=5000 python3 -m unittest -v 
    In the example above, we will loop 5000 times.
    """

    ITERATIONS = int(os.environ.get("FUZZ_ITERATIONS", "300"))
    REPOS = ["octocat/Hello-World", "torvalds/linux", "a/b", "my-org/my.repo_name"]
    TITLES = ["Bug", "", "  spaced  ", "na\u00efve caf\u00e9", "\u65e5\u672c\u8a9e",
              "it's \"quoted\"", "'; DROP TABLE issues; --", "x" * 2000]

    def test_random_sequences_match_a_model(self):
        seed = int(os.environ.get("FUZZ_SEED", random.randrange(10 ** 9)))
        rng = random.Random(seed)
        model = {}  # (repository, issue number) -> title: what SQLite should hold

        for step in range(self.ITERATIONS):
            # Same repository, spelled differently some of the time.
            repo_in = rng.choice(self.REPOS)
            if rng.random() < 0.5:
                repo_in = rng.choice([repo_in.lower(), repo_in.upper(), f"  {repo_in} "])
            repo = repo_in.strip().lower()
            why = f"seed={seed} step={step} repo={repo_in!r}"
            action = rng.choice(["import", "import", "failed_import", "read"])

            if action == "import":
                raw, expected = [], {}
                for number in rng.sample(range(1, 30), rng.randint(0, 12)):
                    is_pr = rng.random() < 0.3
                    title = rng.choice(self.TITLES)
                    raw.append(raw_issue(number, title, pull_request=is_pr))
                    if not is_pr:
                        expected[number] = title
                result = self.import_with(raw, repo=repo_in)
                self.assertEqual(
                    result, {"ok": True, "repository": repo, "count": len(expected)}, why)
                for number, title in expected.items():
                    model[(repo, number)] = title  # insert or update

            elif action == "failed_import":
                error = connector.ConnectorError("not_found", "boom")
                with mock.patch("connector.fetch_open_issues", side_effect=error):
                    result = connector.import_issues(repo_in, self.db_path)
                self.assertFalse(result["ok"], why)
                self.assertEqual(result["error"]["code"], "not_found", why)
                # The model is deliberately NOT updated: a failure must change nothing.

            else:  # read, with the network made unusable
                with mock.patch("connector.urllib.request.urlopen",
                                side_effect=AssertionError("read used the network")):
                    result = connector.read_issues(repo_in, self.db_path)
                expected = sorted((n, t) for (r, n), t in model.items() if r == repo)
                self.assertTrue(result["ok"], why)
                self.assertEqual([(i["number"], i["title"]) for i in result["issues"]],
                                 expected, why)
                self.assertEqual(result["count"], len(expected), why)

        # At the end the whole table must equal the model: nothing missing,
        # nothing extra, and no duplicate (repository, number) rows. 
        # Should inspect the table any times, regardless of whether the model is empty or not.
        # init_db (not sqlite3.connect) so a run that never imported anything
        # still has an issues table to query instead of crashing.
        with closing(connector.init_db(self.db_path)) as conn:
            rows = conn.execute("SELECT repository, number, title FROM issues").fetchall()
        self.assertEqual(sorted(rows),
                            sorted((r, n, t) for (r, n), t in model.items()),
                            f"seed={seed} final table differs from model")

if __name__ == "__main__":
    unittest.main() #If all tests pass, we will see 23 tests pass and an OK message.