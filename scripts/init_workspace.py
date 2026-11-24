"""Initialize a new, empty knowledge base without copying personal or Git state."""
import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
FILES = ("SKILL.md", "AGENTS.md", "LICENSE", ".gitignore", "docs/protocol.md",
         "docs/onboarding.md",
         "integrations/README.md", "examples/scenarios.md",
         "scripts/validate_tasks.py")
PLANNING = ("preferences.md", "context.md", "data_sources.md", "decision_log.md",
            "routines.md", "permissions.md")


def initialize(target):
    if target.resolve() == ROOT or (target.exists() and any(target.iterdir())):
        raise ValueError("Choose a new or empty directory")
    sources = {name: name for name in FILES}
    sources.update({f"methods/{p.name}": f"methods/{p.name}"
                    for p in (ROOT / "methods").glob("*.md")})
    sources["tasks.json"] = "templates/tasks.json"
    sources.update({f"planning/{name}": f"templates/planning/{name}" for name in PLANNING})
    for name, source in sources.items():
        if not (ROOT / source).is_file():
            raise ValueError("Run initialization from the starter kit")
    for name, source in sources.items():
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        initialize(args.path)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Initialized empty workspace at {args.path}")
