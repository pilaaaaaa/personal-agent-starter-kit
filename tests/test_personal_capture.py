from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from personal_agent.calendar import reconcile, verify_actual_projection
from personal_agent.inbox import drain
from personal_agent.interaction import ConnectorInbox, LocalInbox, capture_turn, check_receipt, handle_turn, request_for
from personal_agent.store import digest, index, read
from test_personal_agent import Mock


class CaptureTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.journal = self.root / "personal_data/journal.json"
        self.tasks = self.root / "tasks.json"
        self.tasks.write_text(json.dumps({"tasks": [{"id": "email", "status": "todo"}]}))
        self.transport = LocalInbox(self.root)
        self.time = datetime(2025, 3, 17, 15, tzinfo=timezone.utc)

    def message(self, ident, text, minute=0):
        return {"conversation_id": "chat", "message_id": ident, "text": text,
                "observed_at": (self.time + timedelta(minutes=minute)).isoformat(),
                "timezone": "Europe/Amsterdam"}

    def item(self, message, key, state, name=None, **fields):
        return dict(key=key, name=name or key, state=state, evidence=message["text"], **fields)

    def test_full_conversation_with_semantic_adapter(self):
        first = self.message("1", "边散步边听播客，回来想去买菜。")
        next_turn = self.message("2", "到家了，播客也听完了。", 20)
        duplicate = self.message("3", "刚才那一集的确已经听完了。", 25)
        no_actual = self.message("4", "明早应该会跑步，说不定上周也跑过。", 30)
        # The model boundary is injected. These parsed fixtures exercise the full
        # transport, storage and reply path without a live provider.
        parsed = {
            first["text"]: [self.item(first, "walk", "ongoing", "散步"),
                            self.item(first, "podcast", "ongoing", "听播客", parallel_keys=["walk"]),
                            self.item(first, "shop", "future", "买菜")],
            next_turn["text"]: [self.item(next_turn, "home", "completed", "散步", progress="已到家"),
                                self.item(next_turn, "finish", "completed", "听播客")],
            duplicate["text"]: [self.item(duplicate, "again", "completed", "听播客")],
            no_actual["text"]: [self.item(no_actual, "run", "future"), self.item(no_actual, "maybe", "uncertain")],
        }

        def turn(message):
            return handle_turn(message, lambda raw: parsed[raw["text"]], self.transport.submit,
                               lambda raw: "到家后可以先休息一下。")

        started = turn(first)
        complete = turn(next_turn)
        repeated = turn(duplicate)
        self.assertEqual(complete["reply"], "到家后可以先休息一下。")
        self.assertEqual(turn(no_actual)["capture"]["status"], "no_actual")
        self.assertEqual(started["capture"]["dispositions"]["shop"]["status"], "excluded")
        self.assertEqual(repeated["capture"]["dispositions"]["again"]["status"], "duplicate")
        self.assertEqual(turn(first)["capture"], started["capture"])
        data = read(self.journal)
        plans, actual, reviews, *_ = index(data)
        self.assertEqual((plans, reviews), ({}, {}))
        self.assertEqual(len(actual), 2)
        self.assertEqual(sum(entry["kind"] == "actual" for entry in data["operations"]), 4)
        walk_id = started["capture"]["dispositions"]["walk"]["actual_id"]
        podcast_id = started["capture"]["dispositions"]["podcast"]["actual_id"]
        self.assertEqual(complete["capture"]["dispositions"]["home"]["actual_id"], walk_id)
        self.assertEqual(actual[podcast_id]["parallel_ids"], [walk_id])
        self.assertEqual(actual[walk_id]["progress"], "已到家")
        self.assertEqual(actual[walk_id]["precision"], "estimated")
        self.assertTrue(actual[walk_id]["estimate_basis"])
        self.assertEqual(actual[walk_id]["end"], "2025-03-17T16:20:00+01:00")
        self.assertEqual(len(list((self.root / "operations/inbox").glob("*.json"))), 3)

    def test_cli_tracks_task_completion_and_cancellation_without_plan_changes(self):
        message = self.message("1", "邮件已发出。")
        message["observations"] = [self.item(message, "email", "completed", "发送邮件",
                                             task_id="email", complete_task=True)]
        result = subprocess.run([sys.executable, "-m", "personal_agent", "--root", str(self.root), "turn"],
                                input=json.dumps(message), capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["status"], "accepted")
        self.assertEqual(json.loads(self.tasks.read_text())["tasks"][0]["status"], "completed")
        second = self.message("2", "刚才说错了，邮件任务取消。", 5)
        report = capture_turn(second, [self.item(second, "email", "task_cancelled", task_id="email")],
                              self.transport.submit)
        self.assertEqual(report["dispositions"]["email"]["status"], "task_updated")
        self.assertEqual(json.loads(self.tasks.read_text())["tasks"][0]["status"], "cancelled")
        self.assertEqual(set(entry["kind"] for entry in read(self.journal)["operations"]),
                         {"actual", "capture_turn"})
        from execution_fixtures import settings
        from personal_agent.execution import build_context

        day = self.time.date().isoformat()
        context = build_context(read(self.journal), json.loads(self.tasks.read_text()), day,
                                settings(day, self.time), self.time)
        self.assertEqual(context["closed"]["task:email"], "cancelled")
        self.assertNotIn("task:email", context["conflicts"])

    def test_one_turn_can_commit_actual_and_waiting_checkpoint_and_retry(self):
        from personal_agent.intake import current, preview

        message = self.message("mixed", "表已提交，等回复。")
        message["observations"] = [self.item(message, "forms", "completed", "提交表格", task_id="email")]
        message["intake"] = preview({"source": {"id": "mixed-source", "observed_at": message["observed_at"]},
            "text": message["text"], "spans": [{"start": 0, "end": len(message["text"]), "units": ["waiting"]}],
            "units": [{"id": "waiting", "expected": None, "kind": "waiting", "disposition": "waiting",
                       "target": "task:email", "summary": "已提交，等待回复",
                       "checkpoint": {"next_action": "查看回复", "lane": "maintenance"}}]})
        result = capture_turn(message, message["observations"], self.transport.submit)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["intake_operation_id"], result["id"] + "-intake")
        replay = capture_turn(message, message["observations"], self.transport.submit)
        self.assertEqual(replay, result)
        data = read(self.journal)
        self.assertEqual(len(data["operations"]), 3)
        self.assertEqual(current(data)["waiting"]["checkpoint"]["next_action"], "查看回复")
        self.assertEqual(json.loads(self.tasks.read_text())["tasks"][0]["status"], "todo")

    def test_invalid_checkpoint_does_not_publish_activity(self):
        from personal_agent.intake import preview

        message = self.message("bad-checkpoint", "表已提交，等回复。")
        observations = [self.item(message, "forms", "completed", "提交表格", task_id="email")]
        message["intake"] = preview({"source": {"id": "bad-checkpoint-source", "observed_at": message["observed_at"]},
            "text": message["text"], "spans": [{"start": 0, "end": len(message["text"]), "units": ["waiting"]}],
            "units": [{"id": "waiting", "expected": None, "kind": "waiting", "disposition": "waiting",
                       "target": "task:missing", "summary": "等回复"}]})
        self.assertEqual(capture_turn(message, observations, self.transport.submit)["status"], "rejected")
        self.assertEqual(read(self.journal)["operations"], [])

    def test_failed_write_and_lost_receipt_recover_from_the_same_inbox(self):
        message = self.message("1", "我刚开始整理。")
        observations = [self.item(message, "sort", "ongoing", start=message["observed_at"])]
        with patch("personal_agent.inbox.write", side_effect=OSError("write interrupted")):
            pending = capture_turn(message, observations, self.transport.submit)
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(self.transport.pending()[0]["id"], pending["id"])
        self.assertFalse(self.journal.exists())
        with patch("personal_agent.inbox.atomic_write", side_effect=OSError("receipt interrupted")):
            self.assertEqual(capture_turn(message, observations, self.transport.submit)["status"], "pending")
        self.assertEqual(len(index(read(self.journal))[1]), 1)
        drain(self.root)
        self.assertEqual(self.transport.status(pending["id"])["status"], "accepted")
        self.assertEqual(self.transport.pending(), [])
        self.assertEqual(len(read(self.journal)["operations"]), 2)

    def test_atomic_rejection_then_auditable_repair(self):
        message = self.message("1", "我寄出了信，也洗完衣服了。")
        observations = [self.item(message, "letter", "completed"),
                        self.item(message, "laundry", "completed", start="2026-01-01T00:00:00Z")]
        rejected = capture_turn(message, observations, self.transport.submit)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(len(read(self.journal)["operations"]), 0)
        fixed = {**message, "message_id": "repair-1", "supersedes": rejected["id"]}
        observations[1].pop("start")
        self.assertEqual(capture_turn(fixed, observations, self.transport.submit)["status"], "accepted")
        self.assertEqual(len(index(read(self.journal))[1]), 2)
        self.assertEqual(self.transport.pending(), [])
        self.assertEqual(self.transport.status(rejected["id"])["status"], "rejected")

    def test_connector_pending_forged_receipt_and_retry(self):
        message = self.message("1", "我已经跑完了。")
        observations = [self.item(message, "run", "completed")]
        requests = {}
        def commit(request):
            requests[request["id"]] = request
            self.transport.submit(request, process=False)
            raise OSError("response timed out after commit")

        def receipt(ident):
            path = self.root / "operations/results" / (ident + ".json")
            return json.loads(path.read_text()) if path.exists() else None

        remote = ConnectorInbox(commit, requests.get, receipt)
        request = request_for(message, observations)
        self.assertEqual(capture_turn(message, observations, remote.submit)["status"], "pending")
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            check_receipt(request, {"id": request["id"], "status": "accepted",
                                    "fingerprint": digest(request["record"]), "dispositions": {}})
        drain(self.root)
        self.assertEqual(capture_turn(message, observations, remote.submit)["status"], "accepted")

    def test_sync_failure_recovers_without_replaying_actual(self):
        message = self.message("1", "饭吃完了。")
        status = capture_turn(message, [self.item(message, "meal", "completed")], self.transport.submit)
        data = read(self.journal)
        adapter = Mock()
        self.assertEqual(verify_actual_projection(adapter, data, "calendar", self.time)["status"], "pending")
        adapter.fail = True
        with self.assertRaises(RuntimeError):
            reconcile(adapter, data, {"actual": "calendar"}, self.time, apply=True)
        self.assertEqual(self.transport.status(status["id"])["status"], "accepted")
        self.assertEqual(read(self.journal), data)
        adapter.fail = False
        reconcile(adapter, data, {"actual": "calendar"}, self.time, apply=True)
        report = verify_actual_projection(adapter, data, "calendar", self.time)
        self.assertEqual((report["status"], report["journal_revision"]), ("verified", digest(data)))
        self.assertEqual(len(adapter.events("calendar")), 1)

    def test_partial_journal_tasks_transaction_recovers_before_receipt(self):
        from personal_agent.store import atomic_write

        message = self.message("paired", "邮件发了。")
        facts = [self.item(message, "email", "completed", task_id="email", complete_task=True)]

        def interrupt(path, value):
            if path == self.tasks:
                raise OSError("tasks write interrupted")
            atomic_write(path, value)

        with patch("personal_agent.store.atomic_write", side_effect=interrupt):
            pending = capture_turn(message, facts, self.transport.submit)
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(json.loads(self.tasks.read_text())["tasks"][0]["status"], "todo")
        self.assertTrue(self.journal.with_name(".execution-transaction").exists())
        drain(self.root)
        self.assertEqual(self.transport.status(pending["id"])["status"], "accepted")
        self.assertEqual(json.loads(self.tasks.read_text())["tasks"][0]["status"], "completed")
        self.assertFalse(self.journal.with_name(".execution-transaction").exists())
        self.assertEqual(len(read(self.journal)["operations"]), 2)

    def test_distinct_same_day_occurrence_and_historical_time_estimate(self):
        message = self.message("1", "昨天散步了两次。")
        activities = [self.item(message, key, "occurred", "散步", occurrence=key, day="2025-03-16")
                      for key in ("morning", "evening")]
        result = capture_turn(message, activities, self.transport.submit)
        self.assertEqual(result["status"], "accepted")
        actual = index(read(self.journal))[1]
        self.assertEqual(len(actual), 2)
        for activity in actual.values():
            self.assertEqual(activity["reported_day"], "2025-03-16")
            self.assertEqual(activity["precision"], "estimated")
            self.assertIn("正午", activity["estimate_basis"])

    def test_egg_and_rice_foreground_and_durable_handoff(self):
        from personal_agent.interaction import TurnOutbox
        from personal_agent.store import review_data

        first = self.message("egg", "我现在吃完鸡蛋了，饭还没煮，马上按按钮，然后整理一下花，丢垃圾，出门散步。")
        rice = self.message("rice", "饭煮好了。", 25)
        observations = {
            first["text"]: [
                self.item(first, "egg", "completed", "吃鸡蛋"),
                *(self.item(first, key, "future") for key in ("rice", "flowers", "trash", "walk")),
            ],
            rice["text"]: [self.item(rice, "rice", "completed", "煮饭", effort="passive")],
        }
        reply = {first["text"]: "先把饭按上，再整理花，出门顺手丢垃圾。", rice["text"]: "花还没整理的话，就先收一下花。"}
        spool = self.root / "outbox"

        def offline(_):
            raise OSError("network unavailable")

        queued = handle_turn(first, lambda raw: observations[raw["text"]], offline,
                             lambda raw: reply[raw["text"]], outbox=TurnOutbox(spool))
        self.assertEqual(queued["reply"], reply[first["text"]])
        self.assertEqual(queued["capture"]["status"], "queued")
        self.assertFalse(self.journal.exists())
        TurnOutbox(spool).replay(self.transport.submit)
        done = handle_turn(rice, lambda raw: observations[raw["text"]], self.transport.submit,
                           lambda raw: reply[raw["text"]], outbox=TurnOutbox(spool))
        self.assertEqual(done["reply"], reply[rice["text"]])
        self.assertEqual(done["capture"]["status"], "accepted")
        capture_turn({**rice, "message_id": "repeated"}, observations[rice["text"]], self.transport.submit)
        data = read(self.journal)
        plans, actual, *_ = index(data)
        self.assertEqual(plans, {})
        self.assertEqual(sorted(row["name"] for row in actual.values()), ["吃鸡蛋", "煮饭"])
        passive = next(row for row in actual.values() if row["effort"] == "passive")
        self.assertEqual(passive["precision"], "estimated")
        review = review_data(data, json.loads(self.tasks.read_text()), self.time.date(),
                             self.time.date(), "Europe/Amsterdam", self.time + timedelta(hours=1))
        self.assertEqual(len(review["timeline"]), 2)
        self.assertEqual(review["known_seconds"], 60)
        adapter = Mock()
        reconcile(adapter, data, {"actual": "sandbox"}, self.time, apply=True)
        self.assertEqual(len(adapter.events("sandbox")), 2)
        self.assertTrue(all(event["fields"]["reminders"] is False for event in adapter.events("sandbox").values()))

    def test_concurrent_duplicate_submission_is_one_actual(self):
        from concurrent.futures import ThreadPoolExecutor

        m = self.message("concurrent", "花整理好了。")
        observations = [self.item(m, "flowers", "completed")]
        with ThreadPoolExecutor(max_workers=4) as workers:
            statuses = workers.map(lambda _: capture_turn(m, observations, self.transport.submit), range(8))
            self.assertEqual([r["status"] for r in statuses], ["accepted"] * 8)
        self.assertEqual(len(index(read(self.journal))[1]), 1)

    def test_worker_rejects_mutation_of_an_accepted_capture_envelope(self):
        m = self.message("immutable", "散步回来了。")
        result = capture_turn(m, [self.item(m, "walk", "completed")], self.transport.submit)
        path = self.root / "operations/inbox" / (result["id"] + ".json")
        request = json.loads(path.read_text())
        request["record"]["observations"][0]["evidence"] = "altered after acceptance"
        path.write_text(json.dumps(request))
        before = read(self.journal)
        with self.assertRaisesRegex(ValueError, "Accepted capture request changed"):
            drain(self.root)
        self.assertEqual(read(self.journal), before)
