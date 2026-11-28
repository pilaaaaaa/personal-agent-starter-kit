"""Check setup gates against authored user and host evidence."""
import json
import unittest

from jsonschema import Draft202012Validator

from scripts.onboarding_setup import next_gate
from test_workspace import ROOT


class SetupTests(unittest.TestCase):
    def test_first_value_through_personalization(self):
        checkpoint = {"current_state": "S2", "overall_status": "active"}
        setup = {}
        self.assertIsNone(next_gate(checkpoint, setup))
        checkpoint["current_state"] = "S3"
        self.assertEqual("agent_name", next_gate(checkpoint, setup))
        setup["naming"] = "confirmed"
        self.assertIsNone(next_gate(checkpoint, setup))
        checkpoint["current_state"] = "S4"
        self.assertEqual("plugin_offer", next_gate(checkpoint, setup))
        setup["plugin"] = "selected"
        self.assertEqual("plugin_grant", next_gate(checkpoint, setup))
        for unavailable in ("failed", "unavailable"):
            setup["plugin"] = unavailable
            self.assertEqual("plugin_retry", next_gate(checkpoint, setup))
        for resolved in ("declined", "deferred", "ready"):
            setup["plugin"] = resolved
            self.assertIsNone(next_gate(checkpoint, setup))
            self.assertEqual("personalization_offer",
                             next_gate(checkpoint, setup, integrations_resolved=True))
        setup["personalization"] = "manual_pending"
        self.assertIsNone(next_gate(checkpoint, setup, integrations_resolved=True))

    def test_legacy_sync_trigger_and_pause(self):
        checkpoint = {"current_state": "S5", "overall_status": "completed"}
        setup = {"version": 1, "naming": "confirmed", "plugin": "deferred",
                 "personalization": "verified", "pending_question": None}
        schema = json.loads((ROOT / "onboarding/agent-setup.schema.json").read_text())
        Draft202012Validator(schema).validate(setup)
        self.assertIsNone(next_gate(checkpoint, setup))
        self.assertEqual("plugin_offer", next_gate(checkpoint, setup, external_sync=True))
        self.assertIsNone(next_gate(checkpoint, setup, external_sync=True,
                                    plugin_reviewed_for_request=True))
        checkpoint.update(current_state="S4", overall_status="paused",
                          pending_question={"state": "S4", "key": "integration_provider"})
        self.assertIsNone(next_gate(checkpoint, {}))
        checkpoint["overall_status"] = "active"
        self.assertIsNone(next_gate(checkpoint, {}))
        checkpoint["pending_question"] = None
        self.assertEqual("agent_name", next_gate(checkpoint, {}))
