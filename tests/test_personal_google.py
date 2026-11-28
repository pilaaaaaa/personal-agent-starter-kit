from datetime import datetime, timezone
import unittest
from unittest.mock import patch
from unittest.mock import patch

from personal_agent.google import GoogleAdapter, legacy
from personal_agent.store import EMPTY


class MemoryAPI:
    def __init__(self):
        self.remote = {}
        self.tombstones = set()
        self.lose_response = True

    def call(self, path, method="GET", body=None, etag=None):
        if "calendarList" in path:
            return {"id": path.rsplit("/", 1)[-1], "accessRole": "owner"}
        ident = path.rsplit("/", 1)[-1]
        if "?" in path:
            return {"items": list(self.remote.values())}
        if method == "POST":
            ident = body["id"]
            self.remote[ident] = dict(body, etag="etag", organizer={"self": True})
            if self.lose_response:
                self.lose_response = False
                raise legacy.ApiError("transport", retryable=True)
            return self.remote[ident]
        if method == "PATCH":
            assert etag == self.remote[ident]["etag"]
            self.remote[ident].update(body)
            return self.remote[ident]
        if method == "DELETE":
            assert etag == self.remote[ident]["etag"]
            del self.remote[ident]
            self.tombstones.add(ident)
            return {}
        if ident in self.tombstones:
            raise legacy.ApiError(410)
        if ident not in self.remote:
            raise legacy.ApiError(404)
        return self.remote[ident]


class GoogleAdapterTests(unittest.TestCase):
    def test_projection_rounds_outward_to_google_seconds(self):
        with patch.object(legacy, "GoogleCalendar", return_value=MemoryAPI()):
            adapter = GoogleAdapter({}, "synthetic-instance")
        event = {"title": "Fixture", "start": "2026-01-01T10:00:00.123+01:00",
                 "end": "2026-01-01T10:00:00.456+01:00", "free": True}
        body = adapter.body(event)
        self.assertEqual(body["start"]["dateTime"], "2026-01-01T09:00:00+00:00")
        self.assertEqual(body["end"]["dateTime"], "2026-01-01T09:00:01+00:00")
        self.assertTrue(adapter.matches(body, event))

    def test_plan_audit_reports_unmanaged_same_slot_without_mutation(self):
        api = MemoryAPI()
        api.lose_response = False
        with patch.object(legacy, "GoogleCalendar", return_value=api):
            adapter = GoogleAdapter({}, "test")
        event = {"title": "Walk", "start": "2026-11-25T13:30:00+01:00",
                 "end": "2026-11-25T14:45:00+01:00", "free": False}
        adapter.create("plan", "plan:2026-11-25:walk", event)
        managed, = api.remote
        api.remote["manual"] = dict(adapter.body(dict(event, title="Walk and podcast")), id="manual")
        api.remote["other"] = dict(api.remote["manual"], id="other",
                                  end={"dateTime": "2026-11-25T15:00:00+01:00"})
        candidates = adapter.plan_duplicate_candidates("plan", "2026-11-25T00:00:00Z", "2026-11-26T00:00:00Z")
        self.assertEqual(candidates, [{"unmanaged_id": "manual", "managed_ids": [managed]}])
        self.assertEqual(set(api.remote), {managed, "manual", "other"})

    @patch.dict("os.environ", {"PERSONAL_AGENT_ALLOW_CALENDAR_DELETIONS": "true"})
    def test_retry_after_ambiguous_write_and_ownership(self):
        with patch.object(legacy, "GoogleCalendar", return_value=MemoryAPI()):
            adapter = GoogleAdapter({}, "synthetic-instance")
        datum = {"id": "a", "name": "Synthetic activity", "category": "fixture", "status": "completed",
                 "start": "2026-01-01T10:00:00+01:00", "end": "2026-01-01T11:00:00+01:00",
                 "timezone": "Europe/Amsterdam", "precision": "exact"}
        data = dict(EMPTY, operations=[{"id": "a1", "kind": "actual", "record": datum}])
        targets = {"actual": "owned-secondary"}
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with patch.object(legacy, "pause") as pause:
            adapter.sync(data, targets, now, apply=True)
            pause.assert_called_once()
        ident, remote = next(iter(adapter.api.remote.items()))
        self.assertEqual(remote["transparency"], "transparent")
        self.assertEqual(remote["reminders"], {"useDefault": False, "overrides": []})
        self.assertNotIn("attendees", remote)
        self.assertEqual(adapter.sync(data, targets, now, apply=True)["actions"], [])
        datum["name"] = "Corrected activity"
        self.assertEqual(adapter.sync(data, targets, now, apply=True)["actions"][0]["action"], "update")
        self.assertEqual(adapter.api.remote[ident]["summary"], "Corrected activity")
        remote["attendees"] = [{"email": "fixture@example.invalid"}]
        with self.assertRaisesRegex(legacy.SyncError, "ownership conflict"):
            adapter.sync(data, targets, now, apply=True)
        del remote["attendees"]
        datum["status"] = "cancelled"
        action = adapter.sync(data, targets, now, apply=True)["actions"][0]["action"]
        self.assertEqual(action, "delete")
        datum["status"] = "completed"
        adapter.sync(data, targets, now, apply=True)
        self.assertNotIn(ident, adapter.api.remote)
        self.assertEqual(len(adapter.api.remote), 1)
