"""Synthetic input builder shared by regression tests."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from personal_agent.execution import SOURCES, bounds, build_context, identity
from personal_agent.store import instant


def settings(day, now, *, zone="UTC", **override):
    start, _ = bounds(day, ZoneInfo(zone))
    _, end = bounds((date.fromisoformat(day) + timedelta(days=1)).isoformat(), ZoneInfo(zone))
    return {
        "timezone": zone,
        "availability": [{"start": start.astimezone(ZoneInfo(zone)).isoformat(),
                          "end": end.astimezone(ZoneInfo(zone)).isoformat()}],
        "sources": {name: {"status": "ok" if name == "conversation" else "projection_only",
                           "evidence": "synthetic", "observed_at": now.isoformat()} for name in SOURCES},
        **override,
    }


def budget(normal=20):
    return dict(optimistic=normal, normal=normal, conservative=normal + 10,
                source="estimate", evidence="synthetic minutes")


def proposal(data, tasks, record, now, inputs=None, knowledge=None):
    day = record["day"]
    inputs = inputs or settings(day, now)
    context = build_context(data, tasks, day, inputs, now, knowledge)
    scheduled = {identity(item) for item in record["items"] if instant(item["end"]) > now}
    scheduled |= {key for other_day, items in context["latest"].items() if other_day != day
                  for key, item in items.items() if instant(item["end"]) > now}
    return dict(record, publication="Synthetic user approval", execution={
        "revision": context["revision"], "captured_at": context["captured_at"], "inputs": inputs,
        "dispositions": {
            key: {"status": "external" if key.startswith("calendar:") else "in_progress" if key in context["ongoing"] else
                  "scheduled" if key in scheduled else "conflict" if key in context["conflicts"] else "unscheduled",
                  "evidence": "synthetic"}
            for key in context["pool"]
        },
        "budgets": {key: budget() for key in context["pool"] if not key.startswith("calendar:")},
    })
