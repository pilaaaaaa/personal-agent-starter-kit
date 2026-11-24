import contextlib
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sync-google-calendar.py"
spec = importlib.util.spec_from_file_location("calendar_sync", SCRIPT)
cal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cal)
EPOCH = "a" * 40
TASK = dict(id="mail", title="Mail", priority="P1", project="home", notes="reply", status="todo",
            start_time="2026-10-25T02:30:00+02:00", end_time="2026-10-25T02:15:00+01:00")
CLEAR = dict(TASK, start_time=None, end_time=None)


class FakeCalendar:
    calendar = "target@example.com"
    calendar_path = "target%40example.com"
    events = "/calendars/target%40example.com/events"

    def __init__(self):
        self.target = dict(id=self.calendar, summary=cal.NAME, accessRole="owner")
        self.rows = {}
        self.writes = []
        self.pages = 0
        self.revision = 0
        self.after = None

    def call(self, path, method="GET", body=None, etag=None):
        if path == "/users/me/calendarList/" + self.calendar_path:
            assert method == "GET"
            return self.target
        resource, _, query = path.partition("?")
        if resource == self.events and method == "GET":
            filters = parse_qs(query)
            assert filters["privateExtendedProperty"] == ["personalAgentOwner=" + cal.OWNER]
            rows = [r for r in self.rows.values() if r.get("status") != "cancelled"
                    and r.get("extendedProperties", {}).get("private", {}).get("personalAgentOwner") == cal.OWNER]
            page = int(filters.get("pageToken", ["0"])[0])
            self.pages += 1
            result = {"items": copy.deepcopy(rows[page:page + 1])}
            if page + 1 < len(rows):
                result["nextPageToken"] = str(page + 1)
            return result
        if method == "POST":
            assert resource == self.events
            ident = body["id"]
            if ident in self.rows:
                raise cal.ApiError(409)
            row = dict(copy.deepcopy(body), organizer={"self": True}, status="confirmed")
            self.rows[ident] = row
        else:
            assert resource.startswith(self.events + "/")
            ident = resource.split("/")[-1]
            if ident not in self.rows:
                raise cal.ApiError(404)
            row = self.rows[ident]
            if method == "GET":
                return copy.deepcopy(row)
            if not etag or etag != row["etag"]:
                raise cal.ApiError(412)
            if method == "PATCH":
                row.update(copy.deepcopy(body))
            elif method == "DELETE":
                self.rows[ident] = row = {"status": "cancelled", "id": ident}
            else:
                raise AssertionError(method)
        self.revision += 1
        row["etag"] = str(self.revision)
        self.writes.append((method, ident))
        if self.after:
            self.after(method, row)
        return copy.deepcopy(row)


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeCalendar()
        self.enter = contextlib.ExitStack()
        self.addCleanup(self.enter.close)
        self.enter.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.enter.enter_context(patch.object(cal, "pause"))

    def sync(self, *tasks, apply=True, epoch=EPOCH):
        desired = cal.desired_tasks({"schema_version": 2, "tasks": list(tasks)})
        return cal.sync(self.api, desired, dict.fromkeys(desired, epoch), apply, allow_delete=True)

    def test_delete_requires_separate_authorization(self):
        self.sync(TASK)
        desired = cal.desired_tasks({"schema_version": 2, "tasks": [CLEAR]})
        before = list(self.api.writes)
        with self.assertRaisesRegex(cal.SyncError, "authorize deletion"):
            cal.sync(self.api, desired, {}, apply=True)
        self.assertEqual(self.api.writes, before)

    def test_create_payload_and_offsets(self):
        self.sync(TASK)
        event, = self.api.rows.values()
        self.assertEqual(event["summary"], "Mail")
        self.assertEqual(event["description"], "Task ID: mail\nProject: home\nPriority: P1")
        self.assertEqual(event["start"]["dateTime"], TASK["start_time"])
        self.assertEqual(event["end"]["dateTime"], TASK["end_time"])
        self.assertEqual(event["extendedProperties"]["private"]["taskId"], "mail")
        self.assertRegex(event["id"], r"^[0-9a-v]{5,1024}$")

    def test_update_start_end_and_title_in_place(self):
        self.sync(TASK)
        ident, = self.api.rows
        self.sync(dict(TASK, start_time="2026-11-01T09:00Z", end_time="2026-11-01T10:30Z"))
        self.sync(dict(TASK, title="Renamed", start_time="2026-11-01T09:00Z", end_time="2026-11-01T10:30Z"))
        self.assertEqual(self.api.writes, [(verb, ident) for verb in ("POST", "PATCH", "PATCH")])
        self.assertEqual(self.api.rows[ident]["summary"], "Renamed")
        self.assertEqual(self.api.rows[ident]["end"]["dateTime"], "2026-11-01T10:30:00+00:00")

    def test_clear_and_readd_identical_block(self):
        self.sync(TASK)
        self.sync(CLEAR)
        self.assertEqual(self.sync(CLEAR), [])
        self.sync(TASK, epoch="b" * 40)
        self.assertEqual([v for v, _ in self.api.writes], ["POST", "DELETE", "POST"])
        self.assertNotEqual(self.api.writes[0][1], self.api.writes[-1][1])

    def test_due_only_and_removed_tasks(self):
        self.sync(dict(CLEAR, due={"date": "2026-11-22"}))
        self.assertFalse(self.api.writes)
        self.sync(TASK)
        self.assertEqual(self.sync(), [])
        self.assertEqual(len(self.api.writes), 1)

    def test_repeat_completion_and_google_converted_offsets(self):
        self.sync(TASK)
        event, = self.api.rows.values()
        event["start"] = {"dateTime": "2026-10-25T00:30:00Z"}
        event["end"] = {"dateTime": "2026-10-25T01:15:00Z"}
        self.assertEqual(self.sync(dict(TASK, status="done")), [])
        self.assertEqual(self.sync(TASK), [])
        self.assertEqual(len(self.api.writes), 1)

    def test_invalid_last_task_prevents_all_writes(self):
        for start, end in [(None, TASK["end_time"]), (TASK["start_time"], None),
                           (TASK["end_time"], TASK["start_time"]), ("2026-11-01T10:00", TASK["end_time"]),
                           ("2026-02-31T10:00Z", TASK["end_time"]), ("2026-01-01T00:00+01:99", TASK["end_time"])]:
            with self.subTest(start=start, end=end), self.assertRaises(cal.SyncError):
                self.sync(TASK, dict(TASK, id="bad", start_time=start, end_time=end))
        with self.assertRaises(cal.SyncError):
            self.sync(TASK, TASK)
        self.assertFalse(self.api.writes)

    def test_interrupted_insert_restart_and_pagination(self):
        other = dict(TASK, id="other")
        def interrupt(*args):
            raise KeyboardInterrupt()
        self.api.after = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.sync(TASK, other)
        self.api.after = None
        self.sync(TASK, other)
        self.assertEqual(self.sync(TASK, other), [])
        self.assertEqual(len(self.api.writes), 2)
        self.assertGreaterEqual(self.api.pages, 4)

    def test_retry_lost_response_for_each_write(self):
        for task in (TASK, dict(TASK, title="changed"), CLEAR):
            def lose_response(*_):
                self.api.after = None
                raise cal.ApiError(503, retryable=True)
            self.api.after = lose_response
            self.sync(task)
        self.assertEqual([m for m, _ in self.api.writes], ["POST", "PATCH", "DELETE"])

    def test_id_lookup_recovers_when_filtered_list_is_stale(self):
        self.sync(TASK)
        def empty_listing(path, method="GET", body=None, etag=None):
            if "?" in path:
                return {"items": []}
            return underlying(path, method, body, etag)
        underlying = self.api.call
        with patch.object(self.api, "call", side_effect=empty_listing):
            self.assertEqual(self.sync(TASK), [])
        self.assertEqual(len(self.api.writes), 1)

    def test_ambiguous_insert_conflict_never_allocates_another_id(self):
        underlying = self.api.call
        attempts = []
        def conflict(path, method="GET", body=None, etag=None):
            if method == "POST":
                attempts.append(body["id"])
                raise cal.ApiError(409)
            return underlying(path, method, body, etag)
        with patch.object(self.api, "call", side_effect=conflict), self.assertRaises(cal.ApiError):
            self.sync(TASK)
        self.assertGreater(len(attempts), 1)
        self.assertEqual(len(set(attempts)), 1)

    def test_conflict_rechecks_ownership_before_retry(self):
        self.sync(TASK)
        underlying = self.api.call
        def race(path, method="GET", body=None, etag=None):
            if method == "DELETE":
                row, = self.api.rows.values()
                row["extendedProperties"]["private"].clear()
                raise cal.ApiError(412)
            return underlying(path, method, body, etag)
        with patch.object(self.api, "call", side_effect=race):
            self.sync(CLEAR)
        self.assertEqual([m for m, _ in self.api.writes], ["POST"])

    def test_dry_run_plans_each_action_without_writes(self):
        self.sync(TASK, dict(TASK, id="remove"))
        before = copy.deepcopy(self.api.rows)
        plan = self.sync(dict(TASK, title="new"), dict(CLEAR, id="remove"), dict(TASK, id="fresh"), apply=False)
        self.assertEqual([x[0] for x in plan], ["update", "delete", "create"])
        self.assertEqual(self.api.rows, before)
        self.assertEqual(len(self.api.writes), 2)

    def test_unrelated_event_and_deterministic_id_collision(self):
        manual = {"id": "manual", "summary": "Mail"}
        self.api.rows["manual"] = manual.copy()
        self.sync(TASK)
        self.sync(CLEAR)
        self.assertEqual(self.api.rows["manual"], manual)
        ident = cal.event_id("mail", EPOCH, 0)
        self.api.rows[ident] = dict(manual, id=ident)
        with self.assertRaises(cal.SyncError):
            self.sync(TASK)
        self.assertEqual(self.api.rows[ident]["summary"], "Mail")
        self.assertEqual(len(self.api.writes), 2)

    def test_wrong_calendar_primary_or_nonowner(self):
        for bad in ({"primary": True}, {"summary": "Wrong"}, {"id": "elsewhere"}, {"accessRole": "writer"}):
            with self.subTest(bad=bad), patch.dict(self.api.target, bad), self.assertRaises(cal.SyncError):
                self.sync(TASK)
        self.assertFalse(self.api.writes)

    def test_recurring_guests_and_copied_markers(self):
        self.sync(TASK)
        row, = self.api.rows.values()
        for bad in ({"recurrence": ["RRULE:FREQ=DAILY"]}, {"attendees": [{"self": True}]}, {"id": "manual-copy"}):
            with self.subTest(bad=bad), patch.dict(row, bad), self.assertRaises(cal.SyncError):
                self.sync(CLEAR)
        self.assertEqual(len(self.api.writes), 1)

    def test_missing_configuration_and_primary_alias(self):
        for name in cal.SECRETS:
            env = dict.fromkeys(cal.SECRETS, "configured")
            del env[name]
            with self.subTest(name=name), patch.object(cal, "exchange") as http, self.assertRaisesRegex(cal.SyncError, name):
                cal.GoogleCalendar(env)
            http.assert_not_called()
        with self.assertRaises(cal.SyncError):
            cal.configuration(dict(dict.fromkeys(cal.SECRETS, "x"), GOOGLE_CALENDAR_ID="primary"))

    def test_apply_gate_precedes_any_network_access(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["sync", "--apply", "--tasks", str(SCRIPT.parents[1] / "tasks.json")]), patch.object(cal, "exchange") as http, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cal.main(), 1)
            http.assert_not_called()

    def test_oauth_and_conditional_rest_request(self):
        env = dict(zip(cal.SECRETS, ("client", "secret", "refresh", self.api.calendar)))
        with patch.object(cal, "exchange", side_effect=[{"access_token": "one"}, cal.ApiError(401),
                                                       {"access_token": "two"}, {}]) as http:
            api = cal.GoogleCalendar(env)
            api.call(api.events + "/id", "PATCH", {"summary": "new"}, '"version"')
        auth = http.call_args_list[0]
        self.assertEqual(auth.args, ("https://oauth2.googleapis.com/token",))
        self.assertEqual(parse_qs(auth.kwargs["data"].decode())["grant_type"], ["refresh_token"])
        call = http.call_args_list[-1]
        self.assertEqual(urlsplit(call.args[0]).path, "/calendar/v3/calendars/target%40example.com/events/id")
        self.assertEqual(call.kwargs["headers"]["If-Match"], '"version"')
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer two")

    def test_throttle_retries_and_permanent_error_stops(self):
        from unittest.mock import Mock
        read = Mock(side_effect=[cal.ApiError(429, True, 2), {"items": []}])
        self.assertEqual(cal.read_with_retry(read), {"items": []})
        cal.pause.assert_called_with(0, 2)
        with self.assertRaises(cal.ApiError):
            cal.read_with_retry(Mock(side_effect=cal.ApiError(403)))

    def test_git_key_survives_edits_and_changes_after_clear(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "tasks.json"
            def git(*args):
                return subprocess.check_output(["git", "-C", root, *args]).decode().strip()
            git("init", "-q")
            def commit(task):
                path.write_text(json.dumps({"schema_version": 2, "tasks": [task] if task else []}))
                git("add", "tasks.json")
                git("-c", "user.name=Test", "-c", "user.email=test@invalid", "commit", "-qm", "tasks")
                return git("rev-parse", "HEAD")
            first = commit(TASK)
            commit(dict(TASK, title="new"))
            self.assertEqual(cal.schedule_episodes(path, ["mail"]), {"mail": first})
            commit(None)
            commit(TASK)
            self.assertEqual(cal.schedule_episodes(path, ["mail"]), {"mail": first})
            commit(CLEAR)
            last = commit(TASK)
            self.assertEqual(cal.schedule_episodes(path, ["mail"]), {"mail": last})


if __name__ == "__main__":
    unittest.main()
