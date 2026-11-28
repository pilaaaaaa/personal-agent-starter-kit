"""Read the repository's planning knowledge and optional live Calendar evidence."""
from datetime import datetime, time
import os
import re
from zoneinfo import ZoneInfo

from .execution import bounds
from .store import digest, instant, require


def method_directory(root):
    custom = root / "planning" / "methods"
    return custom if (custom / "README.md").is_file() else root / "methods"


def read_knowledge(root):
    files = root / "planning"
    knowledge = {name: (files / f"{name}.md").read_text()
                 for name in ("preferences", "context", "decision_log")}
    directory = method_directory(root)
    index = directory / "README.md"
    if index.is_file():
        catalog = {}
        for name in sorted(set(re.findall(r"\]\(([a-z][a-z0-9-]+)\.md\)", index.read_text()))):
            path = directory / (name + ".md")
            if path.is_file():
                content = path.read_text()
                catalog[name] = {"enabled": bool(re.search(r"^(?:状态：启用|Status: enabled)\s*$", content, re.M | re.I)),
                                 "revision": digest(content)}
        knowledge["methods"] = catalog
    return knowledge


def event_time(event, name, zone):
    field = event[name]
    if field.get("dateTime"):
        return instant(field["dateTime"]).isoformat()
    return datetime.combine(datetime.fromisoformat(field["date"]).date(), time.min, zone).isoformat()


def live_inputs(day, inputs, now):
    """Rebuild external evidence at both capture and commit. Managed copies are not new tasks."""
    provider = os.getenv("PERSONAL_CALENDAR_PROVIDER") or "none"
    merged = {**inputs, "sources": dict(inputs["sources"])}
    for key in ("calendar_commitments", "calendar_constraints", "actual_evidence"):
        merged.pop(key, None)
    if provider == "none":
        for name in ("plan_calendar", "actual_calendar"):
            require(merged["sources"][name]["status"] == "projection_only",
                    f"{name}: no live provider configured; explicitly use projection_only")
        return merged

    require(provider == "google", "Unknown calendar provider")
    from .google import GoogleAdapter, legacy

    adapter = GoogleAdapter(os.environ, os.environ["PERSONAL_CALENDAR_NAMESPACE"])
    targets = {"plan": os.environ["GOOGLE_CALENDAR_ID"], "actual": os.environ["GOOGLE_ACTUAL_CALENDAR_ID"]}
    require(all(targets.values()) and targets["plan"] != targets["actual"],
            "Live planning requires distinct Plan and Actual sources")
    zone = ZoneInfo(inputs["timezone"])
    start, end = bounds(day, zone)
    end = max([end, *(instant(window["end"]) for window in inputs["availability"])])
    rows = {"plan": [], "actual": []}
    for track, target in targets.items():
        adapter.authorization(target)
        seen = set()
        for event in adapter.events(target, start=start.isoformat(), end=end.isoformat(), expand=True):
            if event.get("status") == "cancelled":
                continue
            event_start, event_end = (event_time(event, field, zone) for field in ("start", "end"))
            key = f"calendar:{track}:{event['id']}"
            require(key not in seen, f"Duplicate external calendar identity: {key}")
            seen.add(key)
            props = event.get("extendedProperties", {}).get("private", {})
            owner_key = props.get("paKey")
            if props.get("paOwner") == adapter.namespace:
                require((owner_key or "").startswith(track + ":") and adapter.owned(event, owner_key),
                        f"Unverified or wrong-track managed Calendar event: {key}")
                continue
            if track == "plan" and props.get("personalAgentOwner") == legacy.OWNER:
                legacy.check_owned(event, props["taskId"])
                # These still represent a real commitment until Journal projection
                # migrates them. Retain the evidence, without creating a second task.
                row = {"key": key, "start": event_start, "end": event_end, "legacy_task": props["taskId"]}
                rows[track].append(row)
                continue
            row = {"key": key, "start": event_start, "end": event_end}
            if track == "plan":
                row.update(title=event.get("summary") or "(untitled)",
                           busy=event.get("transparency") != "transparent")
            rows[track].append(row)
        rows[track].sort(key=lambda row: row["key"])
        source = track + "_calendar"
        old = merged["sources"].get(source, {})
        checksum = digest(rows[track])
        merged["sources"][source] = old if old.get("checksum") == checksum and old.get("status") == "ok" else {
            "status": "ok", "evidence": "Google Calendar live read", "checksum": checksum,
            "observed_at": now.isoformat(),
        }
    merged["calendar_commitments"] = [row for row in rows["plan"] if not row.get("legacy_task")]
    merged["calendar_constraints"] = [
        {"id": row["key"], "start": instant(row["start"]).astimezone(zone).isoformat(),
         "end": instant(row["end"]).astimezone(zone).isoformat(),
         "kind": "hard", "evidence": "Busy event in live Plan Calendar"}
        for row in rows["plan"] if row.get("busy")]
    merged["actual_evidence"] = rows["actual"]
    return merged


def draft_inputs(day, inputs, now):
    """Keep known candidates available if the live calendar cannot be read."""
    if os.getenv("PERSONAL_CALENDAR_PROVIDER") != "google":
        return live_inputs(day, inputs, now)
    from .google import legacy

    try:
        return live_inputs(day, inputs, now)
    except (legacy.SyncError, OSError):
        fields = {"calendar_commitments", "calendar_constraints", "actual_evidence"}
        sources = dict(inputs["sources"])
        for name in ("plan_calendar", "actual_calendar"):
            sources[name] = {"status": "failed", "evidence": "Calendar read unavailable",
                             "observed_at": now.isoformat()}
        return {**{key: value for key, value in inputs.items() if key not in fields}, "sources": sources}
