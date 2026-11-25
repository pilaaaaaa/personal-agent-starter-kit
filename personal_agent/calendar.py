"""Reconcile optional calendar projections through a provider-neutral interface."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, timedelta

from .store import effective_sequence, index, instant


@dataclass(frozen=True)
class Capabilities:
    multiple_calendars: bool = True
    color: bool = True
    free_busy: bool = True
    suppress_reminders: bool = True


class CalendarAdapter(ABC):
    capabilities = Capabilities()

    @abstractmethod
    def authorization(self, target):
        """Check that the configured target is writable."""

    @abstractmethod
    def calendars(self):
        """List calendars visible under the granted scope."""

    @abstractmethod
    def events(self, target, *, managed=False, start=None, end=None):
        """List events or return managed events indexed by stable projection key."""

    @abstractmethod
    def create(self, target, key, event):
        """Create idempotently or fail on a conflicting remote identity."""

    @abstractmethod
    def update(self, target, key, existing, event):
        """Update only a verified owned event using its concurrency token."""

    @abstractmethod
    def delete(self, target, key, existing):
        """Delete only a verified owned event using its concurrency token."""

    def matches(self, existing, event):
        return existing["fields"] == event


def projection(data, now, *, external_tasks=False):
    plans, actual, *_ = index(data)
    desired = {"plan": {}, "actual": {}}
    pending = []
    unlocated = {}
    # Keep the first recorded calendar key when an independent block moves to another day.
    original_keys = {}
    for entry in data["operations"]:
        if entry["kind"] == "plan":
            version = entry["record"]
            for item in version["items"]:
                original_keys.setdefault(item["id"], f"plan:{version['day']}:{item['id']}")
    active_items = {}
    versions = (version for history in plans.values() for version in effective_sequence(history)
                if instant(version["effective_at"]) <= now)
    for plan in sorted(versions, key=lambda version: (instant(version["effective_at"]),
                                                       instant(version["created_at"]))):
        day = plan["day"]
        items = {item["id"]: item for item in plan["items"]}
        for item_id, (owner, _) in list(active_items.items()):
            if owner["day"] == day and item_id not in items:
                del active_items[item_id]
        for item_id in items:
            active_items[item_id] = (plan, items[item_id])
    for plan, item in active_items.values():
        if (item.get("status", "scheduled") == "scheduled"
                and not item.get("parallel_to")
                and instant(item["end"]) > now
                and not (external_tasks and item.get("task_ids"))):
            desired["plan"][original_keys[item["id"]]] = {
                "title": item["title"], "start": item["start"], "end": item["end"], "free": False,
                "description": "\n".join(filter(None, (
                    item.get("notes"), *(f"Parallel activity: {other['title']}" for other in plan["items"]
                    if other.get("parallel_to") == item["id"]
                    and other.get("status", "scheduled") == "scheduled")))),
            }
    for ident, record in actual.items():
        if record.get("deleted") or record["status"] == "cancelled":
            continue
        start, end = record.get("start"), record.get("end")
        precision = record["precision"]
        if not (start and end) or precision == "unknown":
            pending.append(ident)
        if precision == "unknown" or not (start or end):
            unlocated.setdefault(record["reported_day"], []).append(record["name"])
            continue
        point = not (start and end)
        if point:
            anchor = instant(start or end)
            start = anchor.isoformat()
            end = (anchor + timedelta(minutes=1)).isoformat()
        label = "≈ " if precision == "estimated" else ""
        if point:
            label += "[Start only] " if record.get("start") else "[End only] "
        description = record.get("estimate_basis") if precision == "estimated" else ""
        if point:
            description = "\n".join(filter(None, (description, "Calendar placeholder excluded from duration totals.")))
        desired["actual"][f"actual:{ident}"] = {
            "title": label + record["name"], "start": start, "end": end, "free": True,
            "timezone": record["timezone"], "description": description or "",
        }
    for day, names in unlocated.items():
        desired["actual"][f"actual:day-summary:{day}"] = {
            "title": f"[Time unknown] {len(names)} activities",
            "start": day, "end": (date.fromisoformat(day) + timedelta(days=1)).isoformat(),
            "all_day": True, "free": True, "description": "\n".join(sorted(names)),
        }
    return desired, pending


def task_projection(tasks, now):
    """Project existing task time blocks using the task ID across reschedules."""
    desired, known = {}, set()
    for task in tasks["tasks"]:
        key = f"plan:task:{task['id']}"
        if key in known:
            raise ValueError("Duplicate task ID")
        known.add(key)
        start, end = instant(task.get("start_time")), instant(task.get("end_time"))
        if (not start or not end or end <= now
                or task["status"] in {"done", "completed", "cancelled"}):
            continue
        if start >= end:
            raise ValueError("Scheduled task end must follow start")
        desired[key] = {"title": task.get("calendar_title") or task["title"],
                        "start": start.isoformat(), "end": end.isoformat(), "free": False,
                        "description": task.get("calendar_notes") or ""}
    return desired, known


def reconcile(adapter, data, targets, now, *, apply=False, colors=None,
              external_tasks=False, tasks=None, allow_delete=False):
    if adapter is None:
        return {"status": "disconnected"}
    desired, pending = projection(data, now, external_tasks=external_tasks or tasks is not None)
    plans, actual, *_ = index(data)
    known = {f"plan:{day}:{item['id']}" for day, versions in plans.items()
             for version in versions for item in version["items"]}
    known.update(f"actual:{ident}" for ident in actual)
    known.update(f"actual:day-summary:{entry['record']['reported_day']}"
                 for entry in data["operations"] if entry["kind"] == "actual"
                 and entry["record"].get("reported_day"))
    if tasks is not None and targets.get("plan") and not external_tasks:
        desired_tasks, known_tasks = task_projection(tasks, now)
        desired["plan"].update(desired_tasks)
        known.update(known_tasks)
    colors = colors or {}
    grouped = {}
    tracks = {}
    capabilities = adapter.capabilities
    for track, target in targets.items():
        if not target:
            continue
        shared = list(targets.values()).count(target) > 1 or not capabilities.multiple_calendars
        # Avoid exporting actual activities if the provider cannot guarantee no reminders.
        if track == "actual" and not capabilities.suppress_reminders:
            pending.extend(key.removeprefix("actual:") for key in desired[track])
            continue
        grouped.setdefault(target, {})
        tracks.setdefault(target, set()).add(track)
        for key, event in desired[track].items():
            event = dict(event, reminders=False)
            if shared:
                event["title"] = f"[{'Planned' if track == 'plan' else 'Actual'}] {event['title']}"
            if capabilities.color and colors.get(track):
                event["color"] = colors[track]
            if not capabilities.free_busy:
                del event["free"]
            grouped[target][key] = event
    actions = []
    for target, wanted in grouped.items():
        adapter.authorization(target)
        existing = adapter.events(target, managed=True)
        for key, remote in existing.items():
            if key in known and key not in wanted and key.split(":", 1)[0] in tracks[target]:
                actions.append(("delete", target, key, remote, None))
        for key, event in wanted.items():
            if key not in existing:
                actions.append(("create", target, key, None, event))
            elif not adapter.matches(existing[key], event):
                actions.append(("update", target, key, existing[key], event))
    if apply and not allow_delete and any(action == "delete" for action, *_ in actions):
        raise ValueError("Preview and authorize managed calendar deletions before applying")
    if apply:
        for action, target, key, remote, event in actions:
            if action == "create":
                adapter.create(target, key, event)
            elif action == "update":
                adapter.update(target, key, remote, event)
            else:
                adapter.delete(target, key, remote)
    return {"status": "applied" if apply else "preview", "pending": len(set(pending)),
            "actions": [{"action": action, "key": key} for action, _, key, _, _ in actions],
            "disabled_tracks": sorted(set(desired) - {track for track, target in targets.items() if target})}
