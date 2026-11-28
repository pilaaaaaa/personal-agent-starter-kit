"""Initialize a new, empty knowledge base without copying personal or Git state."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
PLANNING = ("preferences.md", "context.md", "data_sources.md", "decision_log.md",
            "routines.md", "permissions.md")


def initialize(target):
    if target.resolve() == ROOT or (target.exists() and any(target.iterdir())):
        raise ValueError("Choose a new or empty directory")
    framework = (ROOT / "FRAMEWORK.txt").read_text(encoding="utf-8").splitlines()
    sources = {name: name for name in framework}
    config = json.loads((ROOT / "templates/agent-config.json").read_text(encoding="utf-8"))
    config["installed_version"] = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    checkpoint = json.loads((ROOT / "templates/onboarding_state.json").read_text(encoding="utf-8"))
    checkpoint["last_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    sources["tasks.json"] = "templates/tasks.json"
    sources.update({f"planning/{name}": f"templates/planning/{name}" for name in PLANNING})
    for name, source in sources.items():
        if not (ROOT / source).is_file():
            raise ValueError("Run initialization from the starter kit")
    for name, source in sources.items():
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, output)
    (target / "personal_data").mkdir()
    (target / "personal_data/journal.json").write_text(
        '{"schema_version": 1, "operations": []}\n', encoding="utf-8"
    )
    (target / "personal_data/onboarding_state.json").write_text(
        json.dumps(checkpoint, indent=2) + "\n", encoding="utf-8"
    )
    (target / "agent-config.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        initialize(args.path)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Initialized empty workspace at {args.path}")
