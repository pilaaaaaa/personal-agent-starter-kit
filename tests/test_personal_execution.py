import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution_fixtures import budget, proposal, settings
from personal_agent.execution import build_context, monitor, validate_plan
from personal_agent.inbox import operation
from personal_agent.sources import live_inputs, read_knowledge
from personal_agent.store import EMPTY, Conflict, apply, digest, write

NOW = datetime(2026, 11, 26, 17, tzinfo=timezone.utc)
DAY = "2026-11-26"
TASK = {"id": "alpha", "title": "Important", "status": "todo", "due": {"date": DAY}}
OTHER = {"id": "backlog", "title": "Later", "status": "todo"}
DECISION = {"reason": "Review capacity", "trigger": "Chat", "information": [], "constraints": []}


def block(ident, start=30, end=60, task=None):
    return {"id": ident, "title": ident, "task_ids": [task] if task else [],
            "start": (NOW + timedelta(minutes=start)).isoformat(),
            "end": (NOW + timedelta(minutes=end)).isoformat()}


def addition(ident):
    return {"id": "item:" + ident, "title": ident, "day": DAY, "evidence": "Message 123"}


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tasks = {"tasks": [copy.deepcopy(TASK), copy.deepcopy(OTHER)]}
        self.data = copy.deepcopy(EMPTY)

    def request(self, ident, record, kind="plan"):
        return dict(id=ident, base=digest(self.data), record=record, kind=kind)

    def propose(self, *items, now=NOW, **overrides):
        return proposal(self.data, self.tasks, dict(day=DAY, items=list(items), decision=DECISION),
                        now, settings(DAY, now, **overrides))

    def commit(self, name, record, now=NOW, kind="plan"):
        return apply(self.data, self.request(name, record, kind), self.tasks, now)

    def test_union_of_tasks_plan_and_multiple_conversation_additions(self):
        self.commit("first", self.propose(block("lunch"), intake=[addition("lunch")]))
        additions = [addition("laundry"), addition("email")]
        self.commit("intake", {"intake": additions}, kind="intake_commitments")
        later = NOW + timedelta(minutes=1)
        payload = self.propose(block("lunch"), block("email", 60, 90), now=later)
        states = payload["execution"]["dispositions"]
        self.assertEqual(states["item:laundry"]["status"], "unscheduled")
        self.assertEqual(set(states), {"task:alpha", "task:backlog", "item:lunch", "item:laundry", "item:email"})
        self.commit("second", payload, now=later)
        with self.assertRaisesRegex(ValueError, "silently removed"):
            self.commit("omit-lunch", self.propose(block("email", 60, 90), now=later), now=later)

    def test_elapsed_and_activity_change_replan_without_erasing(self):
        self.commit("initial", self.propose(block("focus", task="alpha")))
        now = NOW + timedelta(minutes=70)
        self.commit("activity", {
            "id": "activity", "name": "Unexpected call", "category": "life", "timezone": "UTC",
            "status": "ongoing", "source": "user response", "precision": "exact",
            "start": (now - timedelta(minutes=15)).isoformat(), "task_ids": ["backlog"],
        }, now, kind="actual")
        report = monitor(self.data, self.tasks, DAY, "UTC", now)
        self.assertEqual(report["overdue"], ["task:alpha"])
        self.assertEqual(report["ongoing"], ["task:backlog"])
        self.assertTrue(report["replan_required"])
        payload = self.propose(block("focus", 90, 120, "alpha"), now=now)
        self.assertEqual(payload["execution"]["dispositions"]["task:backlog"]["status"], "in_progress")
        self.commit("moved", payload, now)

    def test_real_shortfall_and_sleep_tradeoff_are_calculated(self):
        short = {"start": NOW.isoformat(), "end": (NOW + timedelta(minutes=60)).isoformat()}
        sleep = dict(short, id="sleep", kind="soft", evidence="User preference",
                     start=(NOW + timedelta(minutes=30)).isoformat())
        payload = self.propose(block("urgent", task="alpha"), intake=[addition("laundry"), addition("mail")],
                               availability=[short], constraints=[sleep])
        payload["execution"]["budgets"]["task:alpha"] = budget(30)
        report = validate_plan(self.data, self.tasks, payload, NOW)
        self.assertEqual(report["shortfall_minutes"], 10)
        self.assertEqual(report["preferred_shortfall_minutes"], 40)
        self.assertEqual(report["soft_conflicts"], [["task:alpha", "sleep"]])
        sleep["kind"] = "temporary_hard"
        payload = self.propose(block("urgent", task="alpha"), availability=[short], constraints=[sleep])
        with self.assertRaisesRegex(ValueError, "Hard constraint conflict"):
            self.commit("hard", payload)

    def test_conflicting_completion_remains_visible(self):
        self.commit("activity", {
            "id": "activity", "name": "Alpha", "category": "work", "timezone": "UTC",
            "status": "completed", "source": "user response", "precision": "unknown",
            "task_ids": ["alpha"], "complete_task": "alpha",
        }, kind="actual")
        self.tasks["tasks"][0]["status"] = "cancelled"
        payload = self.propose()
        self.assertEqual(payload["execution"]["dispositions"]["task:alpha"]["status"], "conflict")
        self.assertIn("task:alpha", validate_plan(self.data, self.tasks, payload, NOW)["status_conflicts"])
        payload["execution"]["dispositions"].pop("task:alpha")
        with self.assertRaisesRegex(ValueError, "Task conservation failed"):
            self.commit("hide", payload)

    def test_source_failure_or_staleness_blocks_write(self):
        inputs = settings(DAY, NOW)
        inputs["sources"]["actual_calendar"]["status"] = "failed"
        payload = self.propose(sources=inputs["sources"])
        with self.assertRaisesRegex(ValueError, "Required source failed: actual_calendar"):
            self.commit("fail", payload)
        with self.assertRaisesRegex(ValueError, "snapshot missing or stale"):
            self.commit("stale", self.propose(), NOW + timedelta(minutes=31))
        inputs = settings(DAY, NOW)
        inputs["sources"]["conversation"]["observed_at"] = (NOW - timedelta(hours=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "observation missing or stale: conversation"):
            self.commit("stale-conversation", self.propose(sources=inputs["sources"]))

    def test_same_stable_identity_collapses_sources_and_blocks_duplicate_schedule(self):
        self.commit("first", self.propose(block("work", task="alpha")))
        context = build_context(self.data, self.tasks, DAY, settings(DAY, NOW), NOW)
        self.assertEqual(context["pool"]["task:alpha"]["sources"], ["tasks", "plan"])
        with self.assertRaisesRegex(ValueError, "Duplicate plan block"):
            self.commit("duplicate", self.propose(block("work", task="alpha"), block("copy", 90, 120, "alpha")))
        self.tasks["tasks"].append(copy.deepcopy(TASK))
        with self.assertRaisesRegex(ValueError, "duplicate task ID"):
            self.propose()

    def test_consecutive_replans_keep_full_inventory(self):
        self.commit("intake", {"intake": [addition("life")]}, kind="intake_commitments")
        expected = {"task:alpha", "task:backlog", "item:life"}
        for index in range(8):
            now = NOW + timedelta(minutes=index)
            payload = self.propose(block("work", 30 + index, 60 + index, "alpha"), now=now)
            self.assertEqual(set(payload["execution"]["dispositions"]), expected)
            self.commit(f"roll-{index}", payload, now)

    def test_no_deletion_fake_completion_or_unauthorized_defer(self):
        self.commit("first", self.propose(block("focus", task="alpha")))
        payload = self.propose()
        with self.assertRaisesRegex(ValueError, "silently removed"):
            self.commit("remove", payload)
        disposition = payload["execution"]["dispositions"]["task:alpha"]
        disposition["status"] = "completed"
        with self.assertRaisesRegex(ValueError, "Invalid disposition"):
            self.commit("fake-close", payload)
        disposition.update(status="deferred", authorization="made-up", until_day="2026-11-27")
        with self.assertRaisesRegex(ValueError, "Unauthorized deferral"):
            self.commit("fake-defer", payload)
        disposition["status"] = "conflict"
        with self.assertRaisesRegex(ValueError, "concrete conflict"):
            self.commit("fake-conflict", payload)

    def test_omitted_backlog_blocks_both_connector_and_local_write(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            journal, tasks = fixture_root(root, self.data, self.tasks)
            now = datetime.now(timezone.utc)
            day = now.date().isoformat()
            payload = proposal(self.data, self.tasks, dict(day=day, items=[], decision=DECISION),
                               now, knowledge=read_knowledge(root))
            del payload["execution"]["dispositions"]["task:backlog"]
            with self.assertRaisesRegex(ValueError, "missing=.*task:backlog"):
                write(journal, tasks, self.request("local", payload), now, root=root)
            with self.assertRaisesRegex(ValueError, "missing=.*task:backlog"):
                operation(root, {"kind": "plan", "id": "connector", "record": payload, "expected": None},
                          "connector")
            self.assertEqual(json.loads(journal.read_text()), self.data)
            self.assertEqual(json.loads(tasks.read_text()), self.tasks)

    def test_parallel_writers_and_task_only_revision_conflicts(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            journal, tasks = fixture_root(root, self.data, self.tasks)
            payload = proposal(self.data, self.tasks, dict(day=DAY, items=[], decision=DECISION), NOW,
                               knowledge=read_knowledge(root))
            changed_tasks = copy.deepcopy(self.tasks)
            changed_tasks["tasks"].append({"id": "concurrent", "status": "todo"})
            tasks.write_text(json.dumps(changed_tasks))
            with self.assertRaisesRegex(ValueError, "context changed"):
                write(journal, tasks, self.request("stale", payload), NOW, root=root)
            tasks.write_text(json.dumps(self.tasks))

            def submit(name):
                try:
                    return write(journal, tasks, self.request(name, payload), NOW, root=root)["changed"]
                except Conflict:
                    return False

            with ThreadPoolExecutor(max_workers=2) as workers:
                self.assertCountEqual(list(workers.map(submit, ["one", "two"])), [True, False])
            self.assertEqual(len(json.loads(journal.read_text())["operations"]), 1)

    def test_connector_capture_then_accepts_complete_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal, _ = fixture_root(root, self.data, self.tasks)
            now = datetime.now(timezone.utc)
            day = now.date().isoformat()
            receipt = operation(root, {"kind": "execution_context", "id": "capture", "expected": None,
                                       "record": {"day": day, "inputs": settings(day, now)}}, "capture")
            self.assertEqual(receipt["status"], "context_available")
            self.assertEqual(receipt["pool_ids"], ["task:alpha", "task:backlog"])
            record = proposal(self.data, self.tasks, dict(day=day, items=[], decision=DECISION),
                              now, receipt["inputs"], read_knowledge(root))
            record["execution"]["revision"] = receipt["revision"]
            record["execution"]["captured_at"] = receipt["captured_at"]
            self.assertEqual(operation(root, {"kind": "plan", "record": record, "expected": None,
                                               "id": "plan"}, "plan"), "accepted")
            stored = json.loads(journal.read_text())["operations"][0]["record"]["execution"]
            self.assertEqual(set(stored["dispositions"]), set(receipt["pool_ids"]))
            self.assertIn("shortfall_minutes", stored["assessment"])

    def test_interrupted_paired_write_can_be_retried_without_losing_task_projection(self):
        from personal_agent import store

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal, tasks = fixture_root(root, self.data, self.tasks)
            payload = proposal(self.data, self.tasks, dict(day=DAY, items=[block("work", task="alpha")],
                                                         decision=DECISION), NOW, knowledge=read_knowledge(root))
            request = self.request("plan", payload)
            original_write = store.atomic_write

            def fail_on_tasks(path, content):
                if path == tasks:
                    raise OSError("simulated interrupted task projection")
                original_write(path, content)

            with patch("personal_agent.store.atomic_write", side_effect=fail_on_tasks):
                with self.assertRaisesRegex(OSError, "simulated interrupted"):
                    write(journal, tasks, request, NOW, root=root)
            with self.assertRaisesRegex(ValueError, "Interrupted write pending"):
                store.read_snapshot(journal, tasks)
            self.assertFalse(write(journal, tasks, request, NOW, root=root)["changed"])
            state, saved_tasks = store.read_snapshot(journal, tasks)
            self.assertEqual(len(state["operations"]), 1)
            self.assertEqual(saved_tasks["tasks"][0]["start_time"], block("work")["start"])

    def test_live_plan_fixed_windows_keep_actual_separate_and_detect_calendar_drift(self):
        event = {"id": "abc", "summary": "External event",
                 "start": {"dateTime": block("event")["start"]},
                 "end": {"dateTime": block("event")["end"]}}
        env = {"PERSONAL_CALENDAR_PROVIDER": "google", "PERSONAL_CALENDAR_NAMESPACE": "test",
               "GOOGLE_CALENDAR_ID": "p", "GOOGLE_ACTUAL_CALENDAR_ID": "a"}
        with patch.dict("os.environ", env), patch("personal_agent.google.GoogleAdapter") as google:
            adapter = google.return_value
            adapter.namespace = "test"
            adapter.events.side_effect = lambda *args, **kwargs: [event]
            inputs = live_inputs(DAY, settings(DAY, NOW), NOW)
            self.assertEqual([row["key"] for row in inputs["calendar_commitments"]], ["calendar:plan:abc"])
            self.assertEqual([row["key"] for row in inputs["actual_evidence"]], ["calendar:actual:abc"])
            record = proposal(self.data, self.tasks, dict(day=DAY, items=[block("focus", task="alpha")],
                                                         decision=DECISION), NOW, inputs)
            with self.assertRaisesRegex(ValueError, "Hard constraint conflict"):
                self.commit("overlap", record)
            event["summary"] = "Changed since the snapshot"
            self.assertNotEqual(live_inputs(DAY, inputs, NOW), inputs)
            adapter.events.side_effect = RuntimeError("source disconnected")
            with self.assertRaisesRegex(RuntimeError, "source disconnected"):
                live_inputs(DAY, inputs, NOW)


def fixture_root(root, data, tasks):
    journal, task_path = root / "personal_data/journal.json", root / "tasks.json"
    journal.parent.mkdir()
    journal.write_text(json.dumps(data))
    task_path.write_text(json.dumps(tasks))
    (root / "planning").mkdir()
    for name in ("preferences", "context", "decision_log"):
        (root / "planning" / f"{name}.md").write_text("Synthetic fixture")
    return journal, task_path
