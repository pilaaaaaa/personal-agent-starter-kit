"""Offline conformance checks. Fixture guard events are authored, never inferred by a model."""
import json
from pathlib import Path
import re
import tempfile
import unittest

from jsonschema import Draft202012Validator, FormatChecker

from test_workspace import ROOT, initialize


def load(path):
    return json.loads((ROOT / path).read_text())


WORKFLOW = load("onboarding/workflow.json")
STATES = WORKFLOW["states"]
BLANK = load("templates/onboarding_state.json")
SCHEMA = Draft202012Validator(load("onboarding/state.schema.json"),
                             format_checker=FormatChecker())
STEADY = WORKFLOW["steady_state"]


def replay(case, events):
    state = case["start"]
    context = case["context"]
    status = context.get("status", "active")
    for index, event in enumerate(events):
        if event in WORKFLOW["controls"]:
            rule = WORKFLOW["controls"][event]
            previous = status
            if (event == "legacy" and state == "outside"
                    and context.get("checkpoint") == "absent_verified_legacy"):
                previous = "no_checkpoint"
            accepted = rule["from"]
            if previous not in ([accepted] if isinstance(accepted, str) else accepted):
                raise ValueError(event)
            status = rule["to"]
            if event == "legacy":
                state = STEADY
            continue
        if status not in {"active", "completed"} or event not in STATES[state]["transitions"]:
            raise ValueError(event)
        target = STATES[state]["transitions"][event]
        for continuation in WORKFLOW["continuations"]:
            if (continuation["from"] == state and continuation["default_target"] == target
                    and continuation["if_already_resolved"] in context.get("resolved", [])):
                target = continuation["target"]
        if event == "value_delivered" and index != len(events) - 1:
            raise ValueError("S2 must end the turn")
        state = target
        status = "completed" if state == STEADY else "active"
    return state, status


def check_turn(question_count, actions_after_question):
    if question_count > WORKFLOW["max_core_questions_per_turn"] or actions_after_question:
        raise ValueError("Expected a single user-input interrupt")


class OnboardingTests(unittest.TestCase):
    def test_schema_and_initialized_checkpoint(self):
        Draft202012Validator.check_schema(SCHEMA.schema)
        SCHEMA.validate(BLANK)
        self.assertEqual(BLANK["workflow_version"], WORKFLOW["version"])
        self.assertEqual(BLANK["current_state"], WORKFLOW["initial_state"])
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "workspace"
            initialize(destination)
            checkpoint = json.loads((destination / "personal_data/onboarding_state.json").read_text())
            SCHEMA.validate(checkpoint)
            self.assertNotEqual(checkpoint.pop("last_updated_at"), BLANK["last_updated_at"])
            self.assertEqual(checkpoint, {k: v for k, v in BLANK.items() if k != "last_updated_at"})
            manifest = (destination / "FRAMEWORK.txt").read_text().splitlines()
            self.assertFalse(any(path.startswith("personal_data/") for path in manifest))

    def test_installed_onboarding_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "workspace"
            initialize(destination)
            manifest = (destination / "FRAMEWORK.txt").read_text().splitlines()
            for name in ["SKILL.md", "AGENTS.md", "docs/onboarding.md",
                         *[path for path in manifest if path.startswith("onboarding/")]]:
                path = destination / name
                if path.suffix != ".md":
                    continue
                for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", path.read_text()):
                    if "://" not in target:
                        with self.subTest(path=name, target=target):
                            self.assertTrue((path.parent / target).is_file())

    def test_schema_rejects_invalid_checkpoints(self):
        invalid = [
            dict(workflow_version=2), dict(overall_status="completed"),
            dict(last_updated_at="yesterdayZ"), dict(user_goal="personal information"),
            dict(overall_status="paused", resume_state="S2"),
            dict(current_state="S2", pending_question=dict(state="S2", key="storage_choice")),
            dict(completed_states=["S3"], declined_states=["S3"]),
            dict(current_state="S5", overall_status="completed"),
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertFalse(SCHEMA.is_valid(dict(BLANK, **changes)))

    def test_valid_pause_and_legacy(self):
        SCHEMA.validate(dict(BLANK, current_state="S3", overall_status="paused", resume_state="S3",
                             completed_states=["S0", "S1", "S2"],
                             pending_question=dict(state="S3", key="storage_choice")))
        SCHEMA.validate(dict(BLANK, current_state="S5", overall_status="completed", origin="legacy"))

    def test_workflow_graph(self):
        for state, rule in STATES.items():
            with self.subTest(state=state):
                self.assertTrue(all(rule[key] for key in
                                    ["entry", "goal", "required", "allowed", "wait", "fallback"]))
                self.assertTrue(set(rule["transitions"].values()) <= set(STATES))
        self.assertEqual(WORKFLOW["max_core_questions_per_turn"], 1)

    def test_conversation_fixtures(self):
        for case in load("tests/fixtures/onboarding.json"):
            with self.subTest(case=case["name"]):
                self.assertTrue(all(case[key] for key in ("user", "allow", "forbid")))
                state, status = replay(case, case["events"])
                self.assertEqual(state, case["expected"])
                self.assertEqual(status, case["context"].get("expected_status", status))
                for violation in case["context"].get("violations", []):
                    with self.assertRaises(ValueError):
                        if violation == "multiple_questions":
                            check_turn(2, False)
                        elif violation == "after_question":
                            check_turn(1, True)
                        else:
                            replay(case, [violation])

    def test_first_value_must_end_the_turn(self):
        with self.assertRaises(ValueError):
            replay(dict(start="S2", context={}), ["value_delivered", "chat_chosen"])
        check_turn(1, False)


if __name__ == "__main__":
    unittest.main()
