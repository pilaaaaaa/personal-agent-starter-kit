"""Lifecycle boundaries and Apple merge regression coverage."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from execution_fixtures import settings
from personal_agent.__main__ import parser, run
from personal_agent.execution import build_context
from personal_agent.lifecycle import maintain_checkout, maintain_journal
from personal_agent.reminders import import_checkout
from personal_agent.store import EMPTY, apply_task_schedule, digest, write


NOW = datetime.fromisoformat("2026-11-27T12:00:00+00:00")


def due(day):
    return {"date": f"2026-11-{day}", "timezone": "Europe/Amsterdam"}


def task(key="action", day=25):
    return {"id": key, "title": key, "status": "todo", "priority": "P1",
            "execution_date": due(day)["date"], "due": due(day)}


def observation(day, *, completed=False, github_completed=False, baseline=True):
    return {"version": 1, "items": [{
        "id": "action", "identifier": "device-original",
        "current": {"due": due(day), "completed": completed,
                    "completedAt": "2026-11-27T11:00:00+00:00" if completed else None},
        "legacyCompleted": False,
        "baseline": {"githubDue": due(25), "githubCompleted": github_completed,
                     "apple": {"due": due(25), "completed": github_completed}} if baseline else None,
    }]}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tasks = self.root / "tasks.json"
        self.journal = self.root / "personal_data/journal.json"
        self.journal.parent.mkdir()
        self.journal.write_text(json.dumps(EMPTY))
        self.save(task())

    def save(self, *tasks, **settings):
        self.tasks.write_text(json.dumps({"schema_version": 2, "tasks": tasks, **settings}))

    def read(self):
        return json.loads(self.tasks.read_text())["tasks"]

    def test_first_read_rollover_hard_deadline_and_undated(self):
        hard = {**task("hard"), "deadline": due(25)}
        backlog = {"id": "reading", "title": "reading", "status": "todo"}
        fixed = {**task("fixed"), "reminder_fixed": True}
        self.save(task(), hard, backlog, fixed)
        with patch("personal_agent.__main__.datetime") as clock:
            clock.now.return_value = NOW
            result = run(parser.parse_args(["--root", str(self.root), "snapshot"]))
        self.assertEqual(result["lifecycle"]["overdue"], ["hard"])
        self.assertEqual(self.read()[0]["due"], due(27))
        self.assertEqual(self.read()[1]["deadline"], due(25))
        self.assertEqual(self.read()[2], backlog)
        self.assertEqual(self.read()[3]["due"], due(25))
        self.assertEqual(build_context(EMPTY, result["tasks"], "2026-11-27", settings("2026-11-27", NOW), NOW)
                         ["overdue_deadlines"], ["task:hard"])
        self.assertFalse(maintain_checkout(self.root, NOW)["changed"])

    def test_apple_completion_wins_against_rollover_and_survives_replay(self):
        value = observation(25, completed=True)
        self.assertTrue(import_checkout(self.root, value, NOW)["changed"])
        self.assertEqual(self.read()[0]["completed_at"], "2026-11-27T11:00:00+00:00")
        self.assertEqual(self.read()[0]["due"], due(25))
        self.assertFalse(import_checkout(self.root, value, NOW)["changed"])
        # A stale device checkmark already projected from the repository cannot awaken a corrected status.
        self.save(task())
        import_checkout(self.root, observation(25, completed=True, github_completed=True), NOW)
        self.assertEqual(self.read()[0]["status"], "todo")

    def test_apple_date_supersedes_automatic_move_and_keeps_deadline(self):
        self.save({**task(), "deadline": due(25)})
        maintain_checkout(self.root, NOW)
        self.assertEqual(import_checkout(self.root, observation(29), NOW)["changes"],
                         [{"id": "action", "fields": ["due"]}])
        self.assertEqual(self.read()[0]["execution_date"], due(29)["date"])
        self.assertEqual(self.read()[0]["deadline"], due(25))
        self.assertFalse(import_checkout(self.root, observation(29), NOW)["changed"])

    def test_concurrent_date_and_unknown_bootstrap_are_held(self):
        self.save(task(day=28))
        for baseline in (True, False):
            result = import_checkout(self.root, observation(29, baseline=baseline), NOW)
            self.assertEqual(result["status"], "conflict")
            self.assertFalse(result["changed"])
        self.assertEqual(self.read()[0]["due"], due(28))
        self.assertEqual(import_checkout(self.root, observation(28), NOW)["status"], "reconciled")

    def test_plan_moves_linked_reminder_without_creating_one_for_backlog(self):
        plan = {"id": "v1", "day": "2026-11-28", "effective_at": NOW.isoformat(),
                "previous_id": None, "items": [
            {"id": "slot", "title": "action", "task_ids": ["action"],
             "start": "2026-11-28T14:00:00+01:00", "end": "2026-11-28T15:00:00+01:00"},
            {"id": "quiet", "title": "quiet", "task_ids": ["quiet"],
             "start": "2026-11-28T15:00:00+01:00", "end": "2026-11-28T16:00:00+01:00"},
        ]}
        journal = {**EMPTY, "operations": [{"id": "v1", "kind": "plan", "record": plan}]}
        original = deepcopy(journal)
        tasks = {"tasks": [task(), {"id": "quiet", "title": "quiet", "status": "todo"}]}
        self.assertTrue(apply_task_schedule(journal, tasks, plan, NOW))
        self.assertEqual(tasks["tasks"][0]["due"], due(28))
        self.assertNotIn("due", tasks["tasks"][1])
        self.assertEqual(journal, original)

    def test_partial_actual_maintains_without_closing(self):
        record = {"id": "activity", "name": "First step", "category": "work", "timezone": "UTC",
                  "status": "completed", "source": "user response", "precision": "unknown",
                  "task_ids": ["action"]}
        result = write(self.journal, self.tasks, {"id": "partial", "kind": "actual",
                       "base": digest(EMPTY), "record": record}, NOW)
        self.assertEqual(self.read()[0]["status"], "todo")
        self.assertEqual(self.read()[0]["execution_date"], due(27)["date"])
        self.assertEqual(len(result["lifecycle"]["moves"]), 1)

    def test_waiting_and_disabled_lifecycle(self):
        row = {"id": "wait", "target": "task:action", "disposition": "waiting",
               "trigger_at": "2026-11-28T09:00:00+00:00"}
        journal = {**EMPTY, "operations": [{"id": "i", "kind": "intake_commitments",
                    "record": {"units": [row], "source": {"id": "conversation"}}}]}
        tasks = {"tasks": [task()]}
        self.assertEqual(maintain_journal(journal, tasks, NOW)["moves"], [])
        self.save(task(), lifecycle={"enabled": False})
        self.assertEqual(maintain_checkout(self.root, NOW)["status"], "disabled")
        self.assertEqual(self.read()[0]["due"], due(25))
