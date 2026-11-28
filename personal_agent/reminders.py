"""Three-way merge of the managed Apple Reminder fields."""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from .lifecycle import maintain_journal, set_execution_day
from .store import instant, mutate_tasks, require


def due_key(value):
    if value is None:
        return None
    day = date.fromisoformat(value["date"])
    require(day.isoformat() == value["date"], "Invalid reminder date")
    zone = ZoneInfo(value["timezone"])
    clock = value.get("time")
    if clock:
        require(time.fromisoformat(clock).isoformat(timespec="minutes") == clock,
                "Reminder time requires HH:MM")
    return day, clock, zone.key if clock else None


def same_due(left, right):
    if left == right:
        return True
    if not left or not right or not left[1] or not right[1]:
        return False
    left_date = datetime.combine(left[0], time.fromisoformat(left[1]), ZoneInfo(left[2]))
    right_date = datetime.combine(right[0], time.fromisoformat(right[1]), ZoneInfo(right[2]))
    return left_date.astimezone(timezone.utc) == right_date.astimezone(timezone.utc)


def automatic_move_contains(move, baseline, desired):
    start, end = due_key(move.get("from")), due_key(move.get("to"))
    if not all((start, end, baseline)) or end != desired:
        return False
    return start[1:] == baseline[1:] == end[1:] and start[0] <= baseline[0] <= end[0]


def merge(tasks, observation, now):
    by_id = {row["id"]: row for row in observation["items"]}
    require(len(by_id) == len(observation["items"]), "Duplicate Reminder observation")
    changes, conflicts = [], []
    for task in tasks["tasks"]:
        row = by_id.get(task["id"])
        if not row or task["status"] == "cancelled":
            continue
        current = row["current"]
        baseline = row.get("baseline")
        apple_completed = current["completed"]
        github_completed = task["status"] in {"done", "completed"}
        require(type(apple_completed) is bool, "Invalid Reminder completion observation")
        fields = []
        if apple_completed and not github_completed:
            # A previously projected completion must not be replayed after an explicit correction.
            previous = baseline["githubCompleted"] if baseline else row.get("legacyCompleted")
            if previous is False and not row.get("uncertain"):
                task.update(status="completed", completion_source="apple_reminders")
                if current.get("completedAt"):
                    completion = instant(current["completedAt"])
                    require(completion <= now, "Future Apple completion timestamp")
                    task["completed_at"] = completion.isoformat()
                else:
                    task.pop("completed_at", None)
                fields.append("status")
        if task["status"] == "todo":
            apple, github = due_key(current.get("due")), due_key(task.get("due"))
            automatic = task.get("automatic_reminder_move") or {}
            base_due = baseline.get("githubDue") if baseline else (
                automatic.get("from") if automatic else task.get("due"))
            observed_base = due_key(baseline["apple"].get("due")) if baseline else due_key(base_due)
            if row.get("uncertain") or (not same_due(apple, observed_base) and not same_due(apple, github)):
                automatic_only = automatic_move_contains(automatic, due_key(base_due), github)
                if baseline and not row.get("uncertain") and (same_due(github, due_key(base_due)) or automatic_only):
                    if task.get("execution_date") and not task.get("reminder_fixed"):
                        if apple:
                            previous_day = date.fromisoformat(task["execution_date"])
                            local_day = apple[0] + (previous_day - github[0]) if github else apple[0]
                            set_execution_day(task, local_day.isoformat(), reminder=False)
                        else:
                            task.pop("execution_date", None)
                    task["due"] = current.get("due")
                    task.pop("automatic_reminder_move", None)
                    fields.append("due")
                else:
                    conflicts.append({"id": task["id"], "field": "due", "base": base_due,
                                      "github": task.get("due"), "apple": current.get("due")})
        if fields:
            changes.append({"id": task["id"], "fields": fields})
    return {"status": "conflict" if conflicts else "reconciled", "changes": changes, "conflicts": conflicts}


def import_checkout(root, observation, now):
    require(observation.get("version") == 1, "Unsupported Reminder observation")
    def update(data, tasks):
        result = merge(tasks, observation, now)
        result["lifecycle"] = maintain_journal(data, tasks, now)
        return result

    return mutate_tasks(root, update)
