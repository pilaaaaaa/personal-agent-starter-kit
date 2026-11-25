import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_workspace import initialize


class OnboardingStarTests(unittest.TestCase):
    def test_installed_evaluator_uses_only_new_milestones_and_explicit_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            initialize(workspace)
            config = workspace / "agent-config.json"
            checkpoint = workspace / "personal_data/onboarding_state.json"
            original_checkpoint = checkpoint.read_bytes()
            original_config = config.read_bytes()
            sidecar = workspace / "personal_data/onboarding_star.json"

            def turn(*args):
                before = sidecar.read_text() if sidecar.exists() else None
                result = subprocess.run(
                    [sys.executable, str(workspace / "scripts/onboarding_star.py"), *args],
                    input=before or "null", text=True,
                    capture_output=True, check=True,
                )
                self.assertEqual(config.read_bytes(), original_config)
                self.assertEqual(sidecar.read_text() if sidecar.exists() else None, before)
                self.assertEqual(checkpoint.read_bytes(), original_checkpoint)
                response = json.loads(result.stdout)
                # Simulate the authorized caller saving the returned metadata.
                sidecar.write_text(json.dumps(response["state"]))
                if response["invitation"]:
                    self.assertEqual(
                        response["invitation"]["url"],
                        "https://github.com/pilaaaaaa/personal-agent-starter-kit",
                    )
                    return response["invitation"]["milestone"]
                return None

            self.assertEqual(turn("--milestone", "welcome", "--milestone", "first_value"), "welcome")
            self.assertIsNone(turn("--milestone", "welcome"))
            self.assertIsNone(turn())  # No queued first-value offer.
            self.assertEqual(turn("--milestone", "core_configured"), "core_configured")
            self.assertEqual(
                turn("--milestone", "core_configured", "--milestone", "onboarding_completed"),
                "onboarding_completed",
            )
            self.assertIsNone(turn("--milestone", "onboarding_completed"))
            self.assertIsNone(turn("--feedback", "declined"))
            self.assertIsNone(turn("--milestone", "first_value"))
            self.assertEqual(json.loads(sidecar.read_text())["feedback"], "declined")
            self.assertIsNone(turn("--feedback", "starred"))
            self.assertIsNone(turn("--milestone", "first_value"))

    def test_early_feedback_and_first_value_reminder(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            initialize(workspace)
            script = workspace / "scripts/onboarding_star.py"

            def run(payload, *args, check=True):
                return subprocess.run(
                    [sys.executable, str(script), *args],
                    input=json.dumps(payload), text=True, capture_output=True, check=check,
                )

            first_value = json.loads(run(None, "--milestone", "first_value").stdout)
            self.assertEqual(first_value["invitation"]["milestone"], "first_value")
            for feedback in ["starred", "declined"]:
                with self.subTest(feedback=feedback):
                    result = json.loads(run(None, "--feedback", feedback, "--milestone", "welcome").stdout)
                    self.assertEqual(result["state"], {"feedback": feedback, "offered": []})
                    self.assertIsNone(result["invitation"])
            self.assertEqual(run({"offered": []}, check=False).returncode, 2)


if __name__ == "__main__":
    unittest.main()
