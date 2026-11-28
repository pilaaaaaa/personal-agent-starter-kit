"""Structured task mutations used by the personal harness."""
from datetime import date
import re

from .capture import KEY
from .store import digest, mutate_tasks, require


def update_tasks(root, rows, turn, zone):
    def update(_journal, document):
        existing = {task["id"]: task for task in document["tasks"]}
        seen = set()
        for row in rows:
            require(set(row) <= {"key", "title", "day", "time", "notes", "task_id", "deadline_day"}
                    and KEY.fullmatch(row["key"]) and row["key"] not in seen, "Invalid task update")
            seen.add(row["key"])
            ident = row.get("task_id") or "pa-" + digest([turn, row["key"]])[:32]
            task = existing.get(ident)
            if row.get("task_id"):
                require(task and task["status"] == "todo", "Task change requires an open stable ID")
            elif task is None:
                require(isinstance(row.get("title"), str) and row["title"].strip(), "New task needs a title")
                task = {"id": ident, "title": row["title"], "status": "todo"}
                document["tasks"].append(task)
                existing[ident] = task
            else:
                continue
            if "title" in row:
                require(isinstance(row["title"], str) and row["title"].strip(), "Task title is required")
                task["title"] = row["title"]
            if "notes" in row:
                require(isinstance(row["notes"], str), "Task notes must be text")
                task["notes"] = row["notes"]
            day, clock = row.get("day"), row.get("time")
            require(not clock or day, "Reminder time needs a date")
            if day:
                require(date.fromisoformat(day).isoformat() == day, "Invalid local date")
                due = {"date": day, "timezone": zone.key}
                prior_clock = (task.get("due") or {}).get("time")
                if clock:
                    require(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock), "Invalid reminder time")
                    due["time"] = clock
                elif prior_clock:
                    due["time"] = prior_clock
                task["execution_date"] = day
                if not task.get("reminder_fixed") or clock:
                    task["due"] = due
            if "deadline_day" in row:
                deadline = row["deadline_day"]
                require(date.fromisoformat(deadline).isoformat() == deadline, "Invalid deadline date")
                task["deadline"] = {**(task.get("deadline") or {}), "date": deadline, "timezone": zone.key}
        return {}
    return mutate_tasks(root, update)
