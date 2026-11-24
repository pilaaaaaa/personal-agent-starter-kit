"""Validate a private task snapshot without network access."""
import argparse
from datetime import date, datetime, time
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
ZONED = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d{1,6})?)?(Z|[+-]\d\d:?\d\d)")


def instant(value):
    if not isinstance(value, str) or not ZONED.fullmatch(value):
        raise ValueError("Expected ISO timestamp with UTC offset")
    zone = ZONED.fullmatch(value)[1]
    if zone != "Z":
        hours, minutes = int(zone[1:3]), int(zone[-2:])
        if minutes > 59 or hours * 60 + minutes > 1080:
            raise ValueError("Invalid UTC offset")
    return datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))


def validate(doc):
    if (not isinstance(doc, dict) or type(doc.get("schema_version")) is not int
            or doc["schema_version"] not in (1, 2) or not isinstance(doc.get("tasks"), list)):
        raise ValueError("Expected schema version 1 or 2 and a tasks array")
    policy = doc.get("sync_policy")
    if policy is not None and (not isinstance(policy, dict)
            or policy.get("direction") != "github_to_apple_reminders"
            or policy.get("delete_behavior") not in (
                "never_delete_automatically", "delete_verified_managed_reminders")):
        raise ValueError("Invalid Reminders sync policy")
    tasks = {}
    for task in doc["tasks"]:
        key = task.get("id") if isinstance(task, dict) else None
        if not isinstance(key, str) or not ID.fullmatch(key) or key in tasks:
            raise ValueError("Missing, invalid or duplicate task ID")
        tasks[key] = task
        try:
            validate_task(task)
        except (ValueError, TypeError, KeyError, ZoneInfoNotFoundError) as exc:
            raise ValueError(f"{key}: invalid task fields") from exc
    # Resolve dependencies in topological order; include historical completed tasks.
    from graphlib import TopologicalSorter
    graph = {key: task.get("depends_on", []) for key, task in tasks.items()}
    if any(dependency not in tasks for deps in graph.values() for dependency in deps):
        raise ValueError("Unknown dependency ID")
    list(TopologicalSorter(graph).static_order())
    return tasks


def validate_task(task):
    if not isinstance(task.get("title"), str) or not task["title"].strip():
        raise ValueError("Title is required")
    if task.get("status") not in ("todo", "done", "completed", "cancelled"):
        raise ValueError("Invalid status")
    if task.get("priority") not in ("P0", "P1", "P2", "P3"):
        raise ValueError("Invalid priority")
    for name in ("project", "notes", "reminder_list"):
        if task.get(name) is not None and not isinstance(task[name], str):
            raise ValueError("Invalid text")
    if "[Personal Agent ID:" in (task.get("notes") or ""):
        raise ValueError("Reserved ownership marker")
    start, end = task.get("start_time"), task.get("end_time")
    if (start is not None or end is not None) and instant(start) >= instant(end):
        raise ValueError("Invalid schedule")
    if task.get("reminder_at") is not None:
        instant(task["reminder_at"])
    due = task.get("due")
    if due is not None:
        if not isinstance(due, dict) or date.fromisoformat(due["date"]).isoformat() != due["date"]:
            raise ValueError("Invalid due date")
        ZoneInfo(due["timezone"])
        if due.get("time") is not None:
            if not re.fullmatch(r"\d\d:\d\d", due["time"]):
                raise ValueError("Expected HH:MM")
            time.fromisoformat(due["time"])
    dependencies = task.get("depends_on", [])
    if not isinstance(dependencies, list) or any(
            not isinstance(item, str) or not ID.fullmatch(item) for item in dependencies):
        raise ValueError("Invalid dependencies")
    if len(set(dependencies)) != len(dependencies):
        raise ValueError("Duplicate dependencies")
    waiting = task.get("waiting")
    if waiting is not None:
        if task["status"] != "todo" or not isinstance(waiting, dict):
            raise ValueError("Waiting requires an open task")
        if not isinstance(waiting.get("on"), str) or not waiting["on"].strip():
            raise ValueError("Missing waiting condition")
        instant(waiting.get("review_at"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, nargs="?", default=Path("tasks.json"))
    args = parser.parse_args()
    try:
        tasks = validate(json.loads(args.path.read_text()))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Validation failed: {exc}\n")
    print(f"Validated {len(tasks)} task records")
