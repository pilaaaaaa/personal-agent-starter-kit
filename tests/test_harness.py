from datetime import datetime, timedelta
import json
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory
import unittest

from execution_fixtures import proposal
from personal_agent.calendar import projection
from personal_agent.sources import read_knowledge
from personal_agent.store import UTC, index, read
from plugins.personal_agent.runtime import Runtime, git


def fixture(root):
    source = Path(__file__).resolve().parents[1]
    copytree(source / "templates/planning", root / "planning")
    copytree(source / "methods", root / "methods")
    copytree(source / "docs", root / "docs")
    (root / "AGENTS.md").write_text((source / "AGENTS.md").read_text())
    (root / "personal_data").mkdir()
    (root / "personal_data/journal.json").write_text('{"schema_version":1,"operations":[]}')
    (root / "tasks.json").write_text(json.dumps({"schema_version": 2, "tasks": [
        {"id": "forms", "title": "Synthetic forms", "status": "todo"},
    ]}))
    git(root, "init", "--initial-branch=main")
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "Fixture")
    remote = root.parent / "remote.git"
    git(root, "clone", "--bare", "--", str(root), str(remote))
    return remote


def batch(ident, *operations):
    return {"request_id": ident, "conversation_id": "synthetic", "observed_at": datetime.now(UTC).isoformat(),
            "operations": list(operations)}


def actual(key, name, **fields):
    return {"key": key, "kind": "actual", "record": {
        "name": name, "evidence": "Synthetic test evidence", "state": "completed", **fields}}


class HarnessTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fixture_root = self.root / "fixture"
        fixture_root.mkdir()
        self.remote = fixture(fixture_root)
        self.runtime = Runtime(self.root / "state", str(self.remote), "UTC", targets=("google_calendar", "apple_reminders"))
        self.runtime.refresh()

    def run_batch(self, payload):
        self.assertEqual("ACCEPTED", self.runtime.submit(payload)["state"])
        self.runtime.process(self.runtime.jobs_in({"ACCEPTED"})[0])
        return self.runtime.result(payload["request_id"])

    def test_batch_projection_and_retry_after_losing_local_state(self):
        yesterday = (datetime.now(UTC) - timedelta(days=2)).date()
        payload = batch("mixed", actual("lunch", "Synthetic meal"),
                        actual("forms", "Synthetic forms", task_id="forms", complete_task=True),
                        actual("cross-day", "Synthetic call", day=str(yesterday),
                               start=f"{yesterday}T23:30:00+00:00",
                               end=f"{yesterday + timedelta(days=1)}T02:30:00+00:00"),
                        {"key": "new", "kind": "task", "record": {"title": "Synthetic new task"}},
                        {"key": "preference", "kind": "preference", "record": {
                            "scope": "preferences", "summary": "Synthetic", "evidence": "Synthetic"}})
        receipt = self.run_batch(payload)
        self.assertEqual("SYNCING", receipt["state"], receipt)
        self.assertEqual(receipt, self.runtime.submit(payload))
        changed = {**payload, "conversation_id": "another"}
        self.assertEqual("FAILED", self.runtime.submit(changed)["state"])
        self.runtime = Runtime(self.root / "replacement", str(self.remote), "UTC", targets=("google_calendar", "apple_reminders"))
        self.runtime.refresh()
        self.assertEqual("COMMITTED", self.runtime.result("mixed")["state"])
        replay = self.run_batch(payload)
        self.assertEqual(receipt["github_commit"], replay["github_commit"])
        self.assertTrue(replay["timings_ms"]["recovered_commit"])
        with self.runtime.checkout() as checkout:
            data = read(checkout / "personal_data/journal.json")
            self.assertEqual(3, len(index(data)[1]))
            tasks = json.loads((checkout / "tasks.json").read_text())["tasks"]
            self.assertEqual("completed", tasks[0]["status"])
            self.assertEqual(2, len(tasks))
            desired, _ = projection(data, datetime.now(UTC))
            self.assertEqual(3, len(desired["actual"]))
        path = self.runtime.jobs_in({"SYNCING"})[0]
        self.runtime.verify(path, lambda target, commit: "verified" if target == "google_calendar" else "workflow_pending")
        self.assertEqual("SYNCING", self.runtime.result("mixed")["state"])
        self.runtime.verify(path, lambda target, commit: "verified")
        self.assertEqual("COMPLETED", self.runtime.result("mixed")["state"])

    def test_missing_data_never_partially_commits(self):
        before = git(self.remote, "rev-parse", "main").stdout
        invalid = batch("invalid", actual("meal", "Synthetic meal"),
                        actual("task", "Unknown task", task_id="absent", complete_task=True))
        receipt = self.run_batch(invalid)
        self.assertEqual("FAILED", receipt["state"], receipt)
        self.assertEqual(before, git(self.remote, "rev-parse", "main").stdout)
        missing = batch("missing", {"key": "task", "kind": "task", "record": {}})
        self.assertEqual("NEEDS_INPUT", self.runtime.submit(missing)["state"])

    def test_plan_requires_complete_candidates_and_preserves_authorization(self):
        now = datetime.now(UTC)
        day = (now + timedelta(days=1)).date().isoformat()
        start = datetime.fromisoformat(f"{day}T10:00:00+00:00")
        with self.runtime.checkout() as checkout:
            document = proposal(
                read(checkout / "personal_data/journal.json"),
                json.loads((checkout / "tasks.json").read_text()),
                {"day": day, "items": [{"id": "forms-block", "title": "Synthetic forms", "task_ids": ["forms"],
                                       "start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat()}],
                 "decision": {"trigger": "Test", "reason": "User approved", "information": [], "constraints": []}},
                now, knowledge=read_knowledge(checkout),
            )
        op = {"key": "plan", "kind": "plan", "expected": None, "record": document}
        incomplete = {**document, "execution": {**document["execution"], "dispositions": {}}}
        rejected = self.run_batch(batch("incomplete", {**op, "record": incomplete}))
        self.assertIn("Task conservation failed", rejected["error"])
        receipt = self.run_batch(batch("publish", op))
        self.assertEqual("SYNCING", receipt["state"], receipt)

    def test_snapshot_has_candidate_and_protocol(self):
        self.assertEqual("AVAILABLE", self.runtime.read("protocol")["state"])
        snapshot = self.runtime.read("state")
        self.assertIn("task:forms", snapshot["context"]["pool"])
        self.assertIn("forms", snapshot["task_revisions"])


if __name__ == "__main__":
    unittest.main()
