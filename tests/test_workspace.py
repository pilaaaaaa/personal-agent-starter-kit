import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from init_workspace import initialize
from validate_tasks import validate


def git(path, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=Test Agent",
         "-c", "user.email=agent@example.test", *args],
        check=check, capture_output=True, text=True,
    )


class WorkspaceTests(unittest.TestCase):
    def test_task_lifecycle_and_waiting_dependencies(self):
        doc = json.loads((ROOT / "templates/tasks.json").read_text())
        self.assertEqual(validate(doc), {})
        key = str(uuid4())
        task = dict(id=key, title="Print gallery labels", priority="P2", status="todo")
        doc["tasks"].append(task)
        followup = dict(id="paper-approval", title="Check paper approval", priority="P1",
                        status="todo", waiting={"on": "Supplier approval",
                                                "review_at": "2030-05-10T11:00Z"})
        doc["tasks"].append(followup)
        task.update(title="Print corrected labels", depends_on=[followup["id"]],
                    due={"date": "2030-05-10", "timezone": "UTC"},
                    reminder_at="2030-05-10T09:00Z",
                    start_time="2030-05-10T12:00Z", end_time="2030-05-10T13:00Z")
        self.assertEqual(validate(doc)[key]["title"], "Print corrected labels")
        task.update(start_time="2030-05-10T13:00Z", end_time="2030-05-10T14:00Z")
        followup.update(status="done", waiting=None)
        self.assertEqual(validate(doc)[followup["id"]]["status"], "done")
        task.update(status="cancelled", start_time=None, end_time=None)
        self.assertEqual(validate(doc)[key]["status"], "cancelled")
        followup["depends_on"] = [key]
        with self.assertRaises(ValueError):
            validate(doc)

    def test_initialized_skill_references_are_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "private"
            initialize(workspace)
            entrypoint = (workspace / "SKILL.md").read_text()
            self.assertIn("(docs/onboarding.md)", entrypoint)
            self.assertEqual(
                (workspace / "docs/onboarding.md").read_text(),
                (ROOT / "docs/onboarding.md").read_text(),
            )

    def test_initialized_release_config_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "private"
            initialize(workspace)
            config = json.loads((workspace / "agent-config.json").read_text())
            version = (ROOT / "VERSION").read_text().strip()
            self.assertRegex(version, r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
            self.assertEqual(config["installed_version"], version)
            self.assertIn(f"## {version}\n", (ROOT / "CHANGELOG.md").read_text())
            template = json.loads((ROOT / "templates/agent-config.json").read_text())
            self.assertIsNone(template["installed_version"])
            self.assertEqual(config["updates"], template["updates"])
            self.assertEqual(config["updates"]["check_policy"], "daily")
            self.assertFalse(config["updates"]["auto_update"])
            manifest = (workspace / "FRAMEWORK.txt").read_text().splitlines()
            self.assertEqual(len(manifest), len(set(manifest)))
            for name in manifest:
                self.assertNotIn(name, {"tasks.json", "agent-config.json"})
                self.assertFalse(name.startswith(("planning/", "templates/", ".github/")))
                self.assertEqual((workspace / name).read_bytes(), (ROOT / name).read_bytes())

    def test_empty_init_and_concurrent_writers(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            first, second, bare = (parent / name for name in ("one", "two", "remote"))
            initialize(first)
            with self.assertRaises(ValueError):
                initialize(first)
            git(parent, "init", "--bare", "-b", "main", str(bare))
            git(first, "init", "-b", "main")
            git(first, "add", ".")
            git(first, "commit", "-m", "Initialize")
            git(first, "remote", "add", "origin", str(bare))
            git(first, "push", "-u", "origin", "main")
            git(parent, "clone", str(bare), str(second))

            def add_task(path, key):
                file = path / "tasks.json"
                doc = json.loads(file.read_text())
                doc["tasks"].append(dict(id=key, title=f"Fictional {key}", priority="P2", status="todo"))
                validate(doc)
                file.write_text(json.dumps(doc))
                git(path, "commit", "-am", f"Add {key}")

            add_task(first, "labels")
            add_task(second, "poster")
            git(first, "push")
            self.assertNotEqual(git(second, "push", check=False).returncode, 0)
            git(second, "fetch", "origin")
            git(second, "reset", "--hard", "origin/main")
            add_task(second, "poster")
            git(second, "push")
            git(first, "pull", "--ff-only")
            final = validate(json.loads((first / "tasks.json").read_text()))
            self.assertEqual(set(final), {"labels", "poster"})


if __name__ == "__main__":
    unittest.main()
