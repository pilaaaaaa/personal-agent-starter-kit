"""Exercise conversational capture through Inbox and planning through the local CLI."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from execution_fixtures import proposal, settings
from personal_agent.calendar import projection
from personal_agent.inbox import operation
from personal_agent.sources import read_knowledge
from personal_agent.store import EMPTY, digest, index, read_snapshot, write
from test_personal_execution import DECISION, fixture_root


class IntentFlowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {"PERSONAL_CALENDAR_PROVIDER": "none"})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(directory.name)
        self.now = datetime.now(timezone.utc)
        self.day = self.now.date().isoformat()
        self.journal, self.tasks = fixture_root(self.root, copy.deepcopy(EMPTY), {"tasks": [
            {"id": "labels", "title": "Print exhibit labels", "status": "todo"},
            {"id": "catalog", "title": "Prepare the exhibit catalog", "status": "todo"},
        ]})

    def snapshot(self):
        return read_snapshot(self.journal, self.tasks)

    def cli(self, command, payload):
        result = subprocess.run(
            [sys.executable, "-m", "personal_agent", "--root", str(self.root), command, "--day", self.day],
            input=json.dumps(payload), capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)

    def capture(self, ident, title, *, expected=None, clock=None, **fields):
        record = {"id": ident, "title": title, "kind": "action", "certainty": "tentative",
                  "status": "open", "day": self.day, "evidence": "user message " + ident, **fields}
        operation_id = "capture-" + ident + ("-update" if expected else "")
        request = {"id": operation_id, "kind": "intent", "record": record, "expected": expected}
        with patch("personal_agent.inbox.datetime") as time:
            time.now.return_value = clock or datetime.now(timezone.utc)
            self.assertEqual(operation(self.root, request, operation_id), "accepted")
        return operation_id

    def draft(self, edits=()):
        return self.cli("execution-draft", {"inputs": settings(self.day, self.now), "edits": list(edits)})

    def block(self, ident, offset, task=None):
        return {"id": ident, "title": ident, "task_ids": [task] if task else [],
                "start": (self.now + timedelta(minutes=offset)).isoformat(),
                "end": (self.now + timedelta(minutes=offset + 25)).isoformat()}

    def plan(self, ident, items, *, patch_record=None, publish=True):
        data, tasks = self.snapshot()
        now = datetime.now(timezone.utc)
        record = proposal(data, tasks, {"day": self.day, "items": items, "decision": DECISION},
                          now, knowledge=read_knowledge(self.root))
        if patch_record:
            del record["items"]
            record["patch"] = patch_record
        if not publish:
            del record["publication"]
        return write(self.journal, self.tasks, {"id": ident, "kind": "plan", "base": digest(data),
                                               "record": record}, now, root=self.root)

    def test_morning_poster_and_afternoon_frame_survive_until_evening(self):
        self.capture("poster", "I might design a poster", clock=self.now - timedelta(hours=8))
        self.capture("frame", "I need to mount the frame", clock=self.now - timedelta(hours=3), certainty="stated")
        self.capture("recipe", "Allow an hour and let the ink dry", kind="detail", target="item:poster")
        draft = self.draft()
        rows = {row["key"]: row for row in draft["items"]}
        self.assertEqual(draft["stage"], "candidate")
        self.assertEqual(set(rows), {"item:frame", "item:poster", "task:labels", "task:catalog"})
        self.assertEqual(rows["item:poster"]["details"][0]["title"], "Allow an hour and let the ink dry")
        self.assertEqual(rows["item:frame"]["status"], "unscheduled")
        data, tasks = self.snapshot()
        self.assertEqual([entry["kind"] for entry in data["operations"]], ["intent"] * 3)
        self.assertEqual(len(tasks["tasks"]), 2)

    def test_multiple_versions_and_partial_reorder_preserve_commitments(self):
        self.capture("poster", "Design a poster", certainty="confirmed")
        self.capture("frame", "Mount the frame", certainty="confirmed")
        first = [self.block("poster", 30), self.block("frame", 60)]
        self.plan("v1", first)
        second = [first[0], self.block("frame", 90)]
        self.plan("v2", second, patch_record={"base_version": "v1", "upsert": second[1:], "remove": []})
        third = [self.block("poster", 125), second[1]]
        self.plan("v3", third, patch_record={"base_version": "v2", "upsert": third[:1], "remove": []})
        with self.assertRaisesRegex(ValueError, "silently removed"):
            self.plan("omit", third[:1], patch_record={"base_version": "v3", "upsert": [], "remove": ["frame"]})
        with self.assertRaisesRegex(ValueError, "base changed"):
            self.plan("stale", third, patch_record={"base_version": "v1", "upsert": [], "remove": []})
        versions = index(self.snapshot()[0])[0][self.day]
        self.assertEqual([version["id"] for version in versions], ["v1", "v2", "v3"])
        self.assertEqual([delta["id"] for delta in versions[-1]["changes"]], ["poster"])

    def test_actual_completes_only_two_factor_and_preserves_plan(self):
        # A historical block can differ from the report time; explicit task evidence controls completion.
        self.plan("v1", [self.block("label-window", 30, "labels"), self.block("email-window", 60, "catalog")])
        initial = index(self.snapshot()[0])[0]
        now = datetime.now(timezone.utc)
        request = {"id": "auth-done", "kind": "actual", "expected": None,
                   "record": {"id": "label-actual", "name": "Print exhibit labels", "category": "work", "timezone": "UTC",
                              "status": "completed", "precision": "exact", "source": "user report",
                              "start": (now - timedelta(minutes=8)).isoformat(), "end": now.isoformat(),
                              "task_ids": ["labels"], "complete_task": "labels"}}
        self.assertEqual(operation(self.root, request, "auth-done"), "accepted")
        data, tasks = self.snapshot()
        self.assertEqual(index(data)[0], initial)
        self.assertEqual({task["id"]: task["status"] for task in tasks["tasks"]},
                         {"labels": "completed", "catalog": "todo"})
        self.assertIn("task:catalog", {row["key"] for row in self.draft()["items"]})
        email = self.block("email-window", 90, "catalog")
        self.plan("move-email", [email],
                  patch_record={"base_version": "v1", "upsert": [email], "remove": []})
        current = index(self.snapshot()[0])[0][self.day][-1]
        self.assertEqual([delta["id"] for delta in current["changes"]], ["email-window"])


    def test_ambiguous_idea_without_date_or_budget_is_retained(self):
        self.capture("maybe", "Maybe learn bookbinding", day=None)
        data, tasks = self.snapshot()
        now = datetime.now(timezone.utc)
        record = proposal(data, tasks, {"day": self.day, "items": [], "decision": DECISION},
                          now, knowledge=read_knowledge(self.root))
        del record["execution"]["budgets"]["item:maybe"]
        write(self.journal, self.tasks, {"id": "publish-unrelated", "kind": "plan", "base": digest(data),
                                         "record": record}, now, root=self.root)
        row = next(row for row in self.draft()["items"] if row["key"] == "item:maybe")
        self.assertIsNone(row["day"])
        self.assertEqual(row["intent"]["certainty"], "tentative")
        self.assertEqual(self.snapshot()[1], tasks)

    def test_recommendation_can_omit_defer_edit_and_reorder_without_filtering(self):
        self.capture("poster", "Design a poster")
        self.capture("frame", "Mount the frame")
        self.capture("walk", "Walk the exhibit floor")
        edits = [
            {"key": "item:frame", "status": "suggested", "reason": "Mount the frame first",
             "assumptions": ["Estimated 25 minutes"],
             **{key: self.block("frame", 30)[key] for key in ("start", "end")}},
            {"key": "item:poster", "status": "deferred", "reason": "Mount board arrives tomorrow",
             "until_day": (self.now + timedelta(days=1)).date().isoformat()},
            {"key": "item:walk", "status": "omitted", "reason": "Drop this from the evening"},
        ]
        draft = self.draft(edits)
        self.assertEqual(draft["stage"], "recommended")
        self.assertEqual([row["choice"] for row in draft["items"][:3]], edits)
        self.assertEqual(len(draft["items"]), 5)
        self.assertEqual(len(self.draft()["items"]), 5)

    def test_cancelled_intent_is_tombstoned_across_plan_history_and_projection(self):
        revision = self.capture("frame", "Mount the frame")
        self.capture("walk", "Walk the exhibit floor")
        self.plan("v1", [self.block("frame", 30), self.block("walk", 60)])
        self.capture("frame", "Mount the frame", expected=revision, status="cancelled", evidence="user explicitly cancelled")
        data, _ = self.snapshot()
        self.assertNotIn("item:frame", {row["key"] for row in self.draft()["items"]})
        self.assertEqual(len(index(data)[0][self.day]), 1)
        keys = projection(data, datetime.now(timezone.utc))[0]["plan"]
        self.assertEqual(list(keys), [f"plan:{self.day}:walk"])
        self.plan("v2", [self.block("walk", 60)],
                  patch_record={"base_version": "v1", "upsert": [], "remove": ["frame"]})
        self.assertNotIn("item:frame", {row["key"] for row in self.draft()["items"]})

    def test_discussing_candidate_does_not_publish_or_overwrite(self):
        self.capture("frame", "Mount the frame")
        self.plan("v1", [self.block("frame", 30)])
        before = self.snapshot()
        self.draft([{"key": "item:frame", "status": "omitted", "reason": "Discuss a possible alternative"}])
        with self.assertRaisesRegex(ValueError, "publication authorization"):
            self.plan("not-approved", [self.block("frame", 60)], publish=False)
        self.assertEqual(before, self.snapshot())

    def test_confirmed_constraint_survives_capture_and_expired_state_is_inactive(self):
        block = self.block("busy", 30)
        self.capture("busy", "Fixed gallery opening", kind="constraint", certainty="confirmed",
                     window={"start": block["start"], "end": block["end"], "kind": "hard"})
        self.capture("energy", "Morning availability is limited", kind="state",
                     expires_at=(self.now - timedelta(minutes=1)).isoformat())
        self.assertNotIn("energy", self.draft()["details"])
        self.assertEqual(self.draft()["constraints"][0]["id"], "intent:busy")
        with self.assertRaisesRegex(ValueError, "Hard constraint conflict"):
            self.plan("overlap", [self.block("email", 30, "catalog")])

    def test_connector_draft_keeps_known_items_when_calendar_read_fails(self):
        from personal_agent.google import legacy

        self.capture("frame", "Mount the frame")
        request = {"id": "wire-draft", "kind": "execution_draft", "expected": None,
                   "record": {"day": self.day, "inputs": settings(self.day, self.now)}}
        before = self.snapshot()
        with (patch.dict(os.environ, {"PERSONAL_CALENDAR_PROVIDER": "google"}),
              patch("personal_agent.sources.live_inputs", side_effect=legacy.SyncError("offline"))):
            result = operation(self.root, request, "wire-draft")
        self.assertEqual(result["status"], "draft_available")
        self.assertEqual(result["sources"]["plan_calendar"]["status"], "failed")
        self.assertIn("item:frame", {row["key"] for row in result["items"]})
        self.assertEqual(before, self.snapshot())
