import copy
from datetime import datetime, timedelta, timezone
import unittest

from execution_fixtures import settings
from personal_agent.execution import build_context
from personal_agent.intake import current, preview
from personal_agent.store import Conflict, apply, digest

NOW = datetime(2026, 11, 27, 16, tzinfo=timezone.utc)
DAY = NOW.date().isoformat()
TASKS = {"tasks": [{"id": "read", "status": "todo"}, {"id": "flight", "status": "todo"},
                   {"id": "email", "status": "completed"}]}


def unit(ident, kind, summary, **fields):
    return dict(id=ident, expected=None, kind=kind, disposition="recorded", summary=summary, **fields)


def request(ident, parts, when=NOW):
    text, spans, units = "", [], []
    for clause, row in parts:
        spans.append({"start": len(text), "end": len(text) + len(clause), "units": [row["id"]]})
        text += clause
        units.append(row)
    return dict(source={"id": ident, "observed_at": when.isoformat()}, text=text, spans=spans, units=units)


def submit(data, payload, ident="turn", now=NOW):
    return apply(data, dict(id=ident, kind="intake_commitments", base=digest(data), record=preview(payload)),
                 TASKS, now)


def voice_turn():
    return request("voice-1", [
        ("Review the exhibit instructions tonight, ", unit("read-onboarding", "task", "Read exhibit instructions", target="task:read", day=DAY)),
        ("Also check the printing budget, ", unit("housing", "task", "Check the printing budget")),
        ("We can order the display stands. ", unit("flight-update", "task", "Update the display stand order", target="task:flight")),
        ("The press update is sent, ", unit("sent-email", "completion", "The user reports sending the press update", target="task:email")),
        ("Deliver a mannequin tomorrow. ", unit("sell", "commitment", "Deliver a mannequin", target="item:intake-sell",
                                    day="2026-11-28")),
        ("Maybe sketch during the break.", unit("podcast", "idea", "Sketching")),
    ])


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.data = {"schema_version": 1, "operations": []}

    def test_voice_round_trip_tracks_all_units_and_distinguishes_idea(self):
        payload = voice_turn()
        self.assertTrue(submit(self.data, payload)[1])
        self.assertFalse(submit(self.data, payload)[1])
        self.assertNotIn("text", self.data["operations"][0]["record"])
        ledger = current(self.data)
        self.assertEqual(len(ledger), 6)
        ctx = build_context(self.data, TASKS, DAY, settings(DAY, NOW), NOW)
        self.assertIn("item:intake-sell", ctx["pool"])
        self.assertNotIn("item:intake-podcast", ctx["pool"])
        self.assertIn("task:email", ctx["closed"])

    def test_missing_span_or_unit_is_rejected(self):
        payload = voice_turn()
        payload["units"].pop(1)
        with self.assertRaisesRegex(ValueError, "identities differ"):
            preview(payload)
        payload = voice_turn()
        payload["spans"].pop(1)
        with self.assertRaisesRegex(ValueError, "partition"):
            preview(payload)

    def test_revision_conflict_is_local_and_retries_do_not_append(self):
        original = voice_turn()
        submit(self.data, original)
        tomorrow = NOW + timedelta(minutes=1)
        edit = unit("sell", "commitment", "晚上Deliver a mannequin", target="item:intake-sell", day="2026-11-28")
        edit["expected"] = "turn"
        submit(self.data, request("voice-2", [("Move the mannequin delivery to the evening.", edit)], tomorrow), "edit", tomorrow)
        self.assertFalse(submit(self.data, original, now=tomorrow)[1])
        other = request("voice-3", [("Ask the gallery about printing.", unit("housing", "task", "Ask the gallery"))], tomorrow)
        other["units"][0]["expected"] = "turn"
        self.assertTrue(submit(self.data, other, "other", tomorrow)[1])
        stale = request("voice-4", [("Move the delivery to morning.", copy.deepcopy(edit))], tomorrow)
        with self.assertRaisesRegex(Conflict, "unit changed"):
            submit(self.data, stale, "stale", tomorrow)
        self.assertEqual(len(current(self.data)), 6)
        self.assertEqual(current(self.data)["sell"]["summary"], "晚上Deliver a mannequin")

    def test_waiting_checks_reply_without_assuming_absence_or_scheduling_podcast(self):
        from personal_agent.execution import monitor

        follow = unit("wait-v", "waiting", "等待回复", target="task:flight",
                      trigger_at=(NOW + timedelta(hours=1)).isoformat(), depends_on=["task:read"])
        follow["disposition"] = "waiting"
        payload = request("voice-wait", [("Check for the gallery reply by tomorrow.", follow),
                                         ("Maybe sketch during a break.", unit("audio", "idea", "Sketching"))])
        submit(self.data, payload)
        before = monitor(self.data, TASKS, DAY, "UTC", NOW)["proactive"]["signals"]
        later = monitor(self.data, TASKS, DAY, "UTC", NOW + timedelta(hours=1))["proactive"]["signals"]
        self.assertEqual([signal["kind"] for signal in before], ["optional_opportunity"])
        self.assertEqual(later[0]["next_step"], "check_if_reply_arrived")
        self.assertEqual(later[0]["authorization"], "advisory_only")
        self.assertEqual(before[0]["id"], later[1]["id"])
        self.assertEqual(len(self.data["operations"]), 1)

    def plan(self, now, *blocks, **inputs):
        from execution_fixtures import proposal

        record = {"day": DAY, "items": list(blocks),
                  "decision": {"trigger": "voice", "reason": "Keep the evening work window",
                               "information": [], "constraints": []}}
        record = proposal(self.data, TASKS, record, now, settings(DAY, now, **inputs))
        record["execution"]["information"] = {
            ident: {"version": row["version"], "reason": "Different execution window",
                    "revisit": "At the next relevant planning check"}
            for ident, row in current(self.data).items()}
        return record

    def block(self, task, start, end):
        return {"id": "block-" + task, "title": task, "task_ids": [task],
                "start": (NOW + timedelta(minutes=start)).isoformat(),
                "end": (NOW + timedelta(minutes=end)).isoformat()}

    def publish(self, ident, record, now):
        return apply(self.data, dict(id=ident, kind="plan", base=digest(self.data), record=record), TASKS, now)

    def test_formal_plan_requires_coverage_by_source_and_identity(self):
        submit(self.data, voice_turn())
        with self.assertRaisesRegex(ValueError, "source is missing"):
            self.plan(NOW, intake_sources=["unprocessed-turn"])
        record = self.plan(NOW, self.block("read", 30, 60), intake_sources=["voice-1"])
        del record["execution"]["information"]["housing"]
        with self.assertRaisesRegex(ValueError, "coverage failed.*housing"):
            self.publish("omission", record, NOW)
        record = self.plan(NOW, self.block("read", 30, 60), intake_sources=["voice-1"])
        entry, _ = self.publish("valid", record, NOW)
        outcomes = entry["record"]["execution"]["assessment"]["information"]
        self.assertEqual(outcomes["read-onboarding"]["disposition"], "scheduled")
        self.assertEqual(outcomes["housing"]["disposition"], "recorded")
        self.assertEqual(outcomes["sell"]["disposition"], "recorded")
        self.assertEqual(outcomes["podcast"]["disposition"], "recorded")
        self.assertNotIn("item:intake-podcast", record["execution"]["dispositions"])

    def test_progressive_clarification_blocks_only_current_execution(self):
        question = unit("which-airport", "question", "Confirm gallery entrance", blocking=True, day=DAY)
        question["disposition"] = "needs_confirmation"
        submit(self.data, request("ambiguous", [("We still need to confirm the gallery entrance.", question)]))
        with self.assertRaisesRegex(ValueError, "must be clarified"):
            self.publish("invalid", self.plan(NOW), NOW)
        later = NOW + timedelta(minutes=1)
        question["day"], question["expected"] = "2026-11-28", "turn"
        change = request("clarification", [("Confirm the entrance before ordering stands tomorrow. Read the instructions tonight.", question)], later)
        submit(self.data, change, "clarified-scope", later)
        entry, _ = self.publish("confirmed", self.plan(later, self.block("read", 30, 60)), later)
        self.assertEqual(entry["record"]["execution"]["assessment"]["information"]
                         ["which-airport"]["disposition"], "needs_confirmation")

    def test_temporary_constraint_protects_unaffected_blocks(self):
        from personal_agent.execution import compare_plans, monitor

        read, flight = self.block("read", 30, 60), self.block("flight", 90, 120)
        self.publish("initial", self.plan(NOW, read, flight), NOW)
        later = NOW + timedelta(minutes=1)
        constraint = unit("collective-dinner", "constraint", "Gallery launch", day=DAY, window={
            "start": read["start"], "end": read["end"], "hard": True})
        submit(self.data, request("new-window", [("The gallery launch takes this half hour.", constraint)], later), now=later)
        impact = monitor(self.data, TASKS, DAY, "UTC", later)["replan_impact"]
        self.assertEqual(impact["affected"], {"task:read": ["intake:collective-dinner"]})
        self.assertEqual(impact["preserve"], ["task:flight"])
        same = self.plan(later, read, flight)
        both = self.plan(later, self.block("read", 60, 90), self.block("flight", 120, 150))
        local = self.plan(later, self.block("read", 60, 90), flight)
        for record in (same, both, local):
            record["execution"]["local_replan"] = True
        compared = compare_plans(self.data, TASKS, {"same": same, "both": both, "local": local}, later)
        self.assertIn("Hard constraint", compared["same"]["reason"])
        self.assertIn("preserve unaffected", compared["both"]["reason"])
        self.assertEqual(compared["local"]["changed_existing_blocks"], ["task:read"])
        self.publish("local", local, later)
        self.assertEqual(self.data["operations"][0]["record"]["items"][1], read)

    def test_method_status_and_revision_participate_in_plan_dispatch(self):
        from pathlib import Path
        from personal_agent.execution import validate_plan
        from personal_agent.sources import read_knowledge

        from tempfile import TemporaryDirectory
        from scripts.init_workspace import initialize

        with TemporaryDirectory() as workspace:
            initialize(Path(workspace))
            knowledge = read_knowledge(Path(workspace))
        from execution_fixtures import proposal

        record = proposal(self.data, TASKS, {"day": DAY, "items": [],
                          "decision": {"methods": ["cost-of-delay"]}}, NOW, knowledge=knowledge)
        self.assertEqual(validate_plan(self.data, TASKS, record, NOW, knowledge)["methods"],
                         {"cost-of-delay": knowledge["methods"]["cost-of-delay"]["revision"]})
        record["decision"]["methods"] = ["unregistered"]
        with self.assertRaisesRegex(ValueError, "unavailable or disabled"):
            validate_plan(self.data, TASKS, record, NOW, knowledge)

    def test_cli_preview_and_connector_write_are_separate_from_projection(self):
        import json
        from pathlib import Path
        import subprocess
        import sys
        import tempfile
        from unittest.mock import patch

        from personal_agent.inbox import drain
        from personal_agent.store import read

        checkout = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            journal = root / "personal_data/journal.json"
            journal.parent.mkdir()
            journal.write_text(json.dumps(self.data))
            (root / "tasks.json").write_text(json.dumps(TASKS))
            intake = subprocess.run([sys.executable, "-m", "personal_agent", "intake-preview"],
                                    input=json.dumps(voice_turn(), ensure_ascii=False), text=True,
                                    capture_output=True, cwd=checkout, check=True)
            inbox = root / "operations/inbox"
            inbox.mkdir(parents=True)
            (inbox / "voice.json").write_text(json.dumps({
                "id": "voice", "kind": "intake_commitments", "expected": None,
                "record": json.loads(intake.stdout),
            }))
            with patch("personal_agent.inbox.datetime") as clock:
                clock.now.return_value = NOW
                drain(root)
                drain(root)
            receipt = json.loads((root / "operations/results/voice.json").read_text())
            self.assertEqual(receipt["status"], "accepted")
            self.assertEqual(len(read(journal)["operations"]), 1)
            self.assertEqual(json.loads((root / "tasks.json").read_text()), TASKS)
