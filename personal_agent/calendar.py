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


def projection(data, now):
    from .execution import identity
    from .intents import cancellations

    cancelled = cancellations(data)
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
    # A later snapshot can drop future blocks, but elapsed blocks remain historical.
    retained = []
    for versions in plans.values():
        history = [version for version in effective_sequence(versions)
                   if instant(version["effective_at"]) <= now]
        for version, successor in zip(history, history[1:]):
            boundary = instant(successor["effective_at"])
            for item in version["items"]:
                cancellation = cancelled.get(identity(item))
                began_before_cancellation = cancellation and instant(item["start"]) < cancellation <= boundary
                if (item.get("status", "scheduled") == "scheduled"
                        and (instant(item["end"]) <= boundary or began_before_cancellation)):
                    retained.append((version, item))
        if history:
            for item in history[-1]["items"]:
                if item.get("status", "scheduled") == "scheduled":
                    retained.append((history[-1], item))
    current = {item["id"]: (version, item) for version, item in
               sorted(retained, key=lambda entry: instant(entry[0]["effective_at"]))}
    def visible(item):
        cancellation = cancelled.get(identity(item))
        return cancellation is None or now < cancellation or instant(item["start"]) < cancellation

    for plan, item in current.values():
        if not visible(item):
            continue
        parent = current.get(item.get("parallel_to"))
        if parent and visible(parent[1]):
            continue
        desired["plan"][original_keys[item["id"]]] = {
            "title": item["title"], "start": item["start"], "end": item["end"], "free": False,
            "description": "\n".join(filter(None, (
                item.get("notes"), *(f"并行活动：{other['title']}" for other in plan["items"]
                if other.get("parallel_to") == item["id"] and visible(other)
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
            label += "[开始时间点] " if record.get("start") else "[结束时间点] "
        description = record.get("estimate_basis") if precision == "estimated" else ""
        if record.get("effort") == "passive":
            description = "\n".join(filter(None, (description, "被动过程或结果，不计入主动用时。")))
        if point:
            description = "\n".join(filter(None, (description, "日历占位，不计入实际时长。")))
        desired["actual"][f"actual:{ident}"] = {
            "title": label + record["name"], "start": start, "end": end, "free": True,
            "timezone": record["timezone"], "description": description or "",
        }
    for day, names in unlocated.items():
        desired["actual"][f"actual:day-summary:{day}"] = {
            "title": f"[时间未知] {len(names)} 项实际活动",
            "start": day, "end": (date.fromisoformat(day) + timedelta(days=1)).isoformat(),
            "all_day": True, "free": True, "description": "\n".join(sorted(names)),
        }
    return desired, pending


def reconcile(adapter, data, targets, now, *, apply=False, colors=None, plan_alert_minutes=None):
    if adapter is None:
        return {"status": "disconnected"}
    desired, pending = projection(data, now)
    plans, actual, *_ = index(data)
    known = {f"plan:{day}:{item['id']}" for day, versions in plans.items()
             for version in versions for item in version["items"]}
    known.update(f"actual:{ident}" for ident in actual)
    known.update(f"actual:day-summary:{entry['record']['reported_day']}"
                 for entry in data["operations"] if entry["kind"] == "actual"
                 and entry["record"].get("reported_day"))
    colors = colors or {}
    grouped = {}
    tracks = {}
    capabilities = adapter.capabilities
    for track, target in targets.items():
        if not target:
            continue
        grouped.setdefault(target, {})
        tracks.setdefault(target, set()).add(track)
        shared = list(targets.values()).count(target) > 1 or not capabilities.multiple_calendars
        # Avoid exporting actual activities if the provider cannot guarantee no reminders.
        if track == "actual" and not capabilities.suppress_reminders:
            pending.extend(key.removeprefix("actual:") for key in desired[track])
            continue
        for key, event in desired[track].items():
            event = dict(event, reminders=False)
            if track == "plan" and plan_alert_minutes is not None and instant(event["start"]) > now:
                event["popup_minutes"] = plan_alert_minutes
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
    if apply:
        for action, target, key, remote, event in sorted(actions, key=lambda row: row[0] == "delete"):
            if action == "create":
                adapter.create(target, key, event)
            elif action == "update":
                adapter.update(target, key, remote, event)
            else:
                adapter.delete(target, key, remote)
    return {"status": "applied" if apply else "preview", "pending": len(set(pending)),
            "actions": [{"action": action, "key": key} for action, _, key, _, _ in actions],
            "disabled_tracks": sorted(set(desired) - {track for track, target in targets.items() if target})}


def verify_actual_projection(adapter, data, target, now, *, color=None):
    """Check the authoritative Actual projection after apply, at this Journal revision."""
    from .store import digest

    report = {"journal_revision": digest(data)}
    if not target or not adapter.capabilities.suppress_reminders:
        return {**report, "status": "disconnected"}
    preview = reconcile(adapter, data, {"actual": target}, now, colors={"actual": color})
    if preview["actions"]:
        return {**report, "status": "pending", "drift": preview["actions"],
                "incomplete_intervals": preview["pending"]}
    return {**report, "status": "verified", "incomplete_intervals": preview["pending"]}
