import copy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest

from execution_fixtures import proposal, settings
from personal_agent.calendar import CalendarAdapter, Capabilities, reconcile
from personal_agent.store import EMPTY, Conflict, apply, digest, index, pending_tasks, review_data, write
from personal_agent.timeline import markdown

START = datetime(2026, 10, 24, 20, tzinfo=timezone.utc)
TASKS = {"tasks": [{"id": "task", "status": "todo", "due": {"date": "2026-11-01"}}]}
DAY = START.date()


class Mock(CalendarAdapter):
    def __init__(self):
        self.data = {}
        self.fail = False

    def authorization(self, target):
        if self.fail:
            raise RuntimeError("Mock provider unavailable")

    def calendars(self):
        return list(self.data)

    def events(self, target, *, managed=False, start=None, end=None):
        return dict(self.data.setdefault(target, {}))

    def create(self, target, key, event):
        self.data[target][key] = {"fields": event}

    def update(self, target, key, existing, event):
        self.create(target, key, event)

    def delete(self, target, key, existing):
        del self.data[target][key]


class PersonalAgentTests(unittest.TestCase):
    def setUp(self):
        self.data = copy.deepcopy(EMPTY)
        self.tasks = copy.deepcopy(TASKS)
        self.item = {"id": "item", "title": "Synthetic task", "task_ids": ["task"],
                     "start": "2026-10-24T23:00:00+02:00", "end": "2026-10-25T03:00:00+01:00"}
        self.decision = {"trigger": "Synthetic input", "reason": "Protect a time window",
                         "information": ["Known window"], "constraints": ["Capacity"]}
        self.adapter = Mock()
        self.targets = {"plan": "p", "actual": "a"}

    def request(self, kind, record, ident):
        return dict(kind=kind, record=record, id=ident, base=digest(self.data))

    def guarded_record(self, record, now):
        known = {item["id"] for entry in self.data["operations"] if entry["kind"] == "plan"
                 for item in entry["record"]["items"]}
        intake = [{"id": "item:" + item["id"], "title": item["title"], "day": record["day"],
                   "evidence": "synthetic"} for item in record["items"]
                  if not item.get("task_ids") and item["id"] not in known]
        return proposal(self.data, self.tasks, record, now, settings(
            record["day"], now, zone="Europe/Amsterdam", intake=intake))

    def plan(self, ident, now=START, items=None, defer=None):
        record = self.guarded_record(dict(day=DAY.isoformat(),
                                     items=[self.item] if items is None else items, decision=self.decision), now)
        if defer:
            record["execution"]["dispositions"][defer] = {
                "status": "deferred", "authorization": "defer", "until_day": "2026-10-25",
                "evidence": "User asked to move it",
            }
        return apply(self.data, self.request("plan", record, ident), self.tasks, now)

    def defer(self, key, now):
        apply(self.data, self.request("authorize_execution", {
            "key": key, "day": str(DAY), "until_day": "2026-10-25", "evidence": "user requested",
        }, "defer"), self.tasks, now)

    def activity(self, ident="activity", **fields):
        return dict(id=ident, name="Synthetic activity", category="work", timezone="Europe/Amsterdam",
                    status="completed", source="fixture", precision="exact", **fields)

    def sync(self, now=START, **kwargs):
        return reconcile(self.adapter, self.data, self.targets, now, apply=True, **kwargs)

    def test_roll_plan_and_link_at_occurrence(self):
        self.plan("v1")
        self.assertEqual(self.sync()["actions"], [{"key": "plan:2026-10-24:item", "action": "create"}])
        new = dict(self.item, title="Adjusted")
        self.plan("v2", START + timedelta(hours=2), [new])
        self.assertEqual(self.sync(START + timedelta(hours=2))["actions"][0]["action"], "update")
        self.assertEqual(self.plan("noop", START + timedelta(hours=3), [new]), (None, False))
        self.defer("task:task", START + timedelta(hours=3))
        self.plan("v3", START + timedelta(hours=3), [], defer="task:task")
        self.assertEqual(self.sync(START + timedelta(hours=3))["actions"][0]["action"], "delete")
        actual = self.activity(start=self.item["start"], end=self.item["end"], task_ids=["task"],
                               complete_task="task")
        request = self.request("actual", actual, "report")
        entry, changed = apply(self.data, request, self.tasks, START + timedelta(hours=7))
        self.assertTrue(changed)
        self.assertEqual(entry["record"]["plan_links"], [{"version_id": v, "item_id": "item"} for v in ("v1", "v2")])
        self.assertFalse(apply(self.data, request, self.tasks, START + timedelta(hours=8))[1])
        self.assertTrue(pending_tasks(self.data, self.tasks))
        self.assertEqual(self.tasks["tasks"][0]["status"], "completed")
        self.assertEqual(self.tasks["tasks"][0]["due"]["date"], "2026-11-01")
        result = self.sync(START + timedelta(hours=7))
        self.assertEqual(result["actions"], [{"key": "actual:activity", "action": "create"}])
        self.assertTrue(self.adapter.data["a"]["actual:activity"]["fields"]["free"])
        self.assertFalse(self.adapter.data["a"]["actual:activity"]["fields"]["reminders"])
        self.assertEqual(self.sync(START + timedelta(hours=7))["actions"], [])
        graph = review_data(self.data, self.tasks, DAY, DAY + timedelta(days=1), "Europe/Amsterdam",
                            START + timedelta(hours=25))
        self.assertEqual([s["actual_ids"] for s in graph["stages"]], [["activity"], ["activity"], []])
        self.assertEqual(graph["known_seconds"], 5 * 3600)
        self.assertEqual(graph["initial"][DAY.isoformat()]["id"], "v1")
        self.assertEqual(graph["rolling"][DAY.isoformat()]["id"], "v3")
        timeline = markdown(graph)
        self.assertIn("Synthetic activity", timeline)
        self.assertIn("Initial", timeline)
        self.assertIn("Rolling", timeline)
        self.assertIn("+0100", timeline)
        self.assertIn("+0200", timeline)

    def test_historical_plan_survives_actual_completion_and_rolling_changes(self):
        other = dict(self.item, id="later", title="Later", task_ids=[],
                     start="2026-10-25T04:00:00+01:00", end="2026-10-25T05:00:00+01:00")
        self.plan("initial", items=[self.item, other])
        initial = START + timedelta(minutes=1)
        self.sync(initial)
        before = copy.deepcopy(self.adapter.data["p"])
        self.adapter.data["p"]["unmanaged"] = {"fields": {"title": "Private event"}}
        self.adapter.data["p"]["actual:activity"] = {"fields": {"title": "Other track"}}
        expected_plan = copy.deepcopy(self.adapter.data["p"])

        now = START + timedelta(hours=7)
        actual = self.activity(start=self.item["start"], end=self.item["end"],
                               task_ids=["task"], complete_task="task")
        apply(self.data, self.request("actual", actual, "report"), self.tasks, now)
        self.assertTrue(pending_tasks(self.data, self.tasks))
        self.assertEqual(self.tasks["tasks"][0]["status"], "completed")
        self.assertEqual(self.sync(now)["actions"], [{"key": "actual:activity", "action": "create"}])
        self.assertEqual(self.adapter.data["p"], expected_plan)

        self.plan("rolling", now + timedelta(minutes=1),
                  [dict(other, title="Revised", start="2026-10-25T06:00:00+01:00",
                                   end="2026-10-25T07:00:00+01:00")])
        now += timedelta(minutes=1)
        self.assertEqual(self.sync(now)["actions"], [{"key": f"plan:{DAY}:later", "action": "update"}])
        key = f"plan:{DAY}:item"
        self.assertEqual(self.adapter.data["p"][key], before[key])
        self.assertEqual(self.sync(now)["actions"], [])

        stale = self.request("actual", self.activity("stale"), "stale-writer")
        apply(self.data, self.request("delete_actual", {"id": "activity", "reason": "Fixture cleanup"},
                                      "cleanup"), self.tasks, now)
        with self.assertRaises(Conflict):
            apply(self.data, stale, self.tasks, now)
        self.assertEqual(self.sync(now)["actions"], [{"key": "actual:activity", "action": "delete"}])
        self.assertEqual(self.adapter.data["p"][key], before[key])
        for unrelated in ("unmanaged", "actual:activity"):
            self.assertEqual(self.adapter.data["p"][unrelated], expected_plan[unrelated])

    def test_independent_plan_moved_across_days_with_parallel_child(self):
        item = dict(self.item, task_ids=[])
        self.plan("original", items=[item])
        first = self.sync()["actions"][0]["key"]
        next_day = (DAY + timedelta(days=1)).isoformat()
        moved = dict(item, start="2026-10-25T23:00:00+01:00", end="2026-10-26T00:00:00+01:00")
        companion = dict(moved, id="podcast", title="Podcast", parallel_to=item["id"])
        now = START + timedelta(minutes=1)
        self.defer("item:item", now)
        self.plan("release-slot", now, [], defer="item:item")
        operation = self.request("plan", self.guarded_record(
            dict(day=next_day, items=[moved, companion], decision=self.decision), now), "moved")
        apply(self.data, operation, self.tasks, now)
        self.assertEqual(self.sync(now)["actions"], [{"key": first, "action": "update"}])
        self.assertEqual(self.adapter.data["p"][first]["fields"]["description"], "并行活动：Podcast")

    def test_supersede_a_future_version_without_rewriting_history(self):
        self.plan("v1")
        future = self.request("plan", self.guarded_record({
            "day": DAY.isoformat(), "items": [dict(self.item, title="Pending change")],
            "effective_at": (START + timedelta(minutes=30)).isoformat(), "decision": self.decision,
        }, START + timedelta(minutes=5)), "future")
        apply(self.data, future, self.tasks, START + timedelta(minutes=5))
        self.plan("v3", START + timedelta(minutes=10), [dict(self.item, title="Immediate change")])
        now = START + timedelta(hours=6)
        result = review_data(self.data, self.tasks, DAY, DAY, "Europe/Amsterdam", now)
        self.assertEqual(result["rolling"][DAY.isoformat()]["id"], "v3")
        self.assertTrue(result["stages"][1]["superseded_before_effective"])
        self.assertEqual(result["stages"][0]["effective_until"], (START + timedelta(minutes=10)).isoformat())
        self.assertEqual(result["stages"][2]["version"]["supersedes"], ["future"])

    def test_unknown_parallel_correction_delete_and_swap(self):
        self.plan("v1")
        now = START + timedelta(hours=9)
        incomplete = self.activity()
        incomplete.update(precision="unknown")
        apply(self.data, self.request("actual", incomplete, "a1"), self.tasks, now)
        self.assertEqual(self.sync(now)["pending"], 1)
        update = dict(id="activity", source="fixture", reason="User supplied time", precision="estimated",
                      estimate_basis="Reported duration", start=self.item["start"], end=self.item["end"])
        apply(self.data, self.request("actual", update, "a2"), self.tasks, now)
        self.assertEqual(self.sync(now)["actions"], [
            {"key": "actual:day-summary:2026-10-25", "action": "delete"},
            {"key": "actual:activity", "action": "create"},
        ])
        update["end"] = "2026-10-25T02:20:00+01:00"
        apply(self.data, self.request("actual", update, "a3"), self.tasks, now)
        self.assertEqual(self.sync(now)["actions"], [{"key": "actual:activity", "action": "update"}])
        other = self.activity("parallel", start="2026-10-25T00:00:00+02:00", end="2026-10-25T01:00:00+02:00",
                              parallel_ids=["activity"])
        apply(self.data, self.request("actual", other, "a4"), self.tasks, now)
        correction = {"version_id": "v1", "reason": "Clarification", "source": "fixture", "annotation": "Typo"}
        apply(self.data, self.request("correction", correction, "correction"), self.tasks, now)
        graph = review_data(self.data, self.tasks, DAY, DAY + timedelta(days=1), "Europe/Amsterdam", now)
        self.assertEqual(len(graph["stages"]), 1)
        self.assertEqual(len(graph["corrections"]), 1)
        self.assertEqual(graph["parallel"][0]["overlap_seconds"], 3600)
        self.assertEqual(graph["known_seconds"], 15600)
        self.assertEqual(graph["exact_seconds"], 3600)
        self.assertEqual(graph["timeline"][0]["plan_links"], [])
        before = digest(self.data)
        self.assertEqual(reconcile(None, self.data, {}, now)["status"], "disconnected")
        self.adapter = Mock()
        self.adapter.capabilities = Capabilities(False, False, False, True)
        self.targets = {"plan": "one", "actual": "one"}
        self.adapter.fail = True
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.sync(now)
        self.adapter.fail = False
        self.sync(now, colors={"actual": "green"})
        fields = self.adapter.data["one"]["actual:activity"]["fields"]
        self.assertTrue(fields["title"].startswith("[Actual]"))
        self.assertNotIn("free", fields)
        self.assertNotIn("color", fields)
        self.assertEqual(before, digest(self.data))
        apply(self.data, self.request("delete_actual", {"id": "activity", "reason": "User correction"},
                                      "remove"), self.tasks, now)
        self.assertEqual(self.sync(now)["actions"], [{"key": "actual:activity", "action": "delete"}])

    def test_feedback_and_parallel_writer_conflict(self):
        feedback = {"id": "protect", "type": "pattern", "status": "active", "evidence": ["r1"],
                    "scope": "exploration", "confidence": "medium", "recheck": "Next week",
                    "rule": "Save ideas before switching tasks"}
        payload = dict(first=str(DAY), findings={"outcome": "Useful result"}, feedback=[feedback],
                       **{"from": str(DAY), "through": str(DAY)})
        with tempfile.TemporaryDirectory() as root:
            journal, tasks = Path(root) / "journal.json", Path(root) / "tasks.json"
            tasks.write_text(json.dumps(self.tasks))
            requests = [self.request("review", payload, f"review{i}") for i in range(2)]
            outcomes = []
            def append(operation):
                try:
                    outcomes.append(write(journal, tasks, operation, START)["changed"])
                except Conflict:
                    outcomes.append(False)
            writers = [threading.Thread(target=append, args=(op,)) for op in requests]
            for writer in writers:
                writer.start()
            for writer in writers:
                writer.join()
            self.assertCountEqual(outcomes, [True, False])
            state = json.loads(journal.read_text())
            graph = review_data(state, self.tasks, DAY, DAY, "UTC", START)
            self.assertEqual(graph["active_feedback"][0]["rule"], feedback["rule"])
            self.assertEqual(len(index(state)[2]), 1)


class InboxAcceptanceTests(unittest.TestCase):
    def test_independent_intake_and_completion_correction(self):
        from personal_agent.inbox import drain

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending = root / "operations/inbox"
            pending.mkdir(parents=True)
            (root / "tasks.json").write_text(json.dumps(TASKS))
            activity = {"id": "activity", "name": "Work", "category": "work", "status": "completed",
                        "timezone": "UTC", "precision": "unknown", "source": "user",
                        "task_ids": ["task"], "complete_task": "task"}
            for suffix in ("one", "two"):
                record = activity if suffix == "one" else dict(
                    activity, id="shower", name="Shower", task_ids=[], complete_task=None,
                    end=START.isoformat(), precision="estimated", estimate_basis="Just finished at message time")
                (pending / f"{suffix}.json").write_text(json.dumps(
                    dict(id=suffix, kind="actual", expected=None, record=record)))
            drain(root)
            journal = root / "personal_data/journal.json"
            self.assertEqual(len(read_json(journal)["operations"]), 2)
            adapter = Mock()
            snapshot = read_json(journal)
            reconcile(adapter, snapshot, {"actual": "a", "plan": "p"}, datetime.now(timezone.utc), apply=True)
            self.assertIn("[结束时间点]", adapter.data["a"]["actual:shower"]["fields"]["title"])
            self.assertEqual(adapter.data["p"], {})
            self.assertEqual(read_json(root / "tasks.json")["tasks"][0]["status"], "completed")

            (pending / "correction.json").write_text(json.dumps(dict(
                id="correction", kind="actual", expected="one",
                record={"id": "activity", "status": "cancelled", "reason": "User clarified", "source": "user"})))
            drain(root)
            drain(root)
            self.assertEqual(read_json(root / "tasks.json")["tasks"][0]["status"], "todo")
            self.assertEqual(len(read_json(journal)["operations"]), 3)


def read_json(path):
    return json.loads(path.read_text())
