"""Maintain execution dates independently of scheduling and provider projections."""
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ZONE = "UTC"
TERMINAL = {"completed", "done", "cancelled"}


def deadline_cutoff(deadline):
    zone = ZoneInfo(deadline["timezone"])
    day = date.fromisoformat(deadline["date"])
    clock = deadline.get("time")
    if clock:
        return datetime.combine(day, time.fromisoformat(clock), zone)
    return datetime.combine(day + timedelta(days=1), time.min, zone)


def date_risk(task, now):
    deadline = task.get("deadline")
    return bool(deadline and task["status"] not in TERMINAL and now >= deadline_cutoff(deadline))


def policy(tasks):
    config = tasks.get("lifecycle", {})
    return config.get("enabled", True), ZoneInfo(config.get("timezone", os.getenv("PERSONAL_AGENT_TIMEZONE", ZONE)))


def set_execution_day(task, day, *, reminder=True):
    old = task.get("execution_date")
    task["execution_date"] = day
    if reminder and not old and task.get("due"):
        task.setdefault("reminder_fixed", True)
    if not reminder or task.get("reminder_fixed") or not old:
        return
    due = task.get("due")
    if not due:
        return
    shift = date.fromisoformat(day) - date.fromisoformat(old)
    task["due"] = {**due, "date": (date.fromisoformat(due["date"]) + shift).isoformat()}


def maintain(tasks, now, *, not_before=None, waiting=()):
    enabled, zone = policy(tasks)
    today = now.astimezone(zone).date()
    changes = []
    for task in tasks["tasks"]:
        if (not enabled or task["status"] != "todo" or not task.get("execution_date")
                or task.get("rollover") == "paused" or task["id"] in waiting):
            continue
        old = date.fromisoformat(task["execution_date"])
        if old >= today:
            continue
        target = max(today, (not_before or {}).get(task["id"], today))
        before = task.get("due")
        set_execution_day(task, target.isoformat())
        if before != task.get("due"):
            previous = task.get("automatic_reminder_move", {})
            task["automatic_reminder_move"] = {
                "from": previous["from"] if previous and previous["to"] == before else before,
                "to": task["due"],
            }
        changes.append({"id": task["id"], "from": old.isoformat(), "to": target.isoformat()})
    return {"status": "maintained" if enabled else "disabled", "day": today.isoformat(),
            "moves": changes, "overdue": [t["id"] for t in tasks["tasks"] if date_risk(t, now)]}


def maintain_checkout(root, now):
    from .store import mutate_tasks

    return mutate_tasks(root, lambda data, tasks: maintain_journal(data, tasks, now))


def maintain_journal(data, tasks, now):
    from .intake import current
    from .store import effective, index, instant

    enabled, zone = policy(tasks)
    if not enabled:
        return maintain(tasks, now)
    plans = index(data)[0]
    versions = [effective(history, now) for history in plans.values()]
    latest = {}
    for version in sorted(filter(None, versions), key=lambda row: instant(row["effective_at"])):
        latest.update(version.get("execution", {}).get("dispositions", {}))
    deferred = {key[5:]: date.fromisoformat(row["until_day"]) for key, row in latest.items()
                if key.startswith("task:") and row["status"] == "deferred" and row.get("until_day")}
    for task in tasks["tasks"]:
        if task.get("end_time") and instant(task["end_time"]) > now:
            day = instant(task["start_time"]).astimezone(zone).date()
            deferred[task["id"]] = max(day, deferred.get(task["id"], day))
    return maintain(tasks, now, not_before=deferred, waiting=waiting_tasks(current(data), now))


def waiting_tasks(information, now):
    from .store import instant

    return {row["target"][5:] for row in information.values()
            if (row.get("target") or "").startswith("task:") and row["disposition"] == "waiting"
            and (not row.get("expires_at") or instant(row["expires_at"]) > now)
            and (not row.get("trigger_at") or instant(row["trigger_at"]) > now)}
