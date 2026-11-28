"""Replay independent conversational facts from the existing Journal."""
from datetime import date
import re

from .store import instant, require, validate_interval

TERMINAL = {"completed", "cancelled"}
FIELDS = {"id", "kind", "title", "evidence", "certainty", "status", "target",
          "day", "expires_at", "details", "window"}


def current_intents(data):
    return {entry["record"]["id"]: {**entry["record"], "revision": entry["id"],
                                    "recorded_at": entry["recorded_at"]}
            for entry in data["operations"] if entry["kind"] == "intent"}


def validate_intent(record, previous, known):
    require(set(record) <= FIELDS, "Unexpected intent fields")
    for name in ("id", "title", "evidence"):
        require(isinstance(record.get(name), str) and record[name].strip(),
                f"Intent requires {name}")
    require(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", record["id"]), "Invalid intent ID")
    require(record["kind"] in {"action", "detail", "constraint", "state"}, "Invalid intent kind")
    require(record["certainty"] in {"tentative", "stated", "confirmed"}, "Invalid certainty")
    require(record["status"] in {"open", *TERMINAL}, "Invalid intent status")
    target = record.get("target")
    require(target is None or target in known, "Unknown intent target")
    require(record["kind"] != "detail" or target, "A detail needs a target")
    require(record["kind"] != "action" or target is None,
            "Use a detail to enrich an existing identity")
    if record.get("day") is not None:
        require(date.fromisoformat(record["day"]).isoformat() == record["day"], "Invalid intent day")
    if record.get("expires_at") is not None:
        require(instant(record["expires_at"]), "Invalid intent expiration")
    require(isinstance(record.get("details", ""), str), "Intent details must be text")
    if "window" in record:
        window = record["window"]
        require(record["kind"] == "constraint" and isinstance(window, dict)
                and set(window) == {"start", "end", "kind"} and window["kind"] in {"hard", "soft"}
                and all(validate_interval(window)), "Invalid intent constraint window")
    if previous:
        require(all(previous.get(key) == record.get(key) for key in ("kind", "target")),
                "Intent kind and target are stable")
        require(previous["status"] not in TERMINAL or previous["status"] == record["status"],
                "Resolved intent is closed; use a new identity for a new occurrence")


def cancellations(data):
    """Timestamp explicit cancellations, for future Plan projection only."""
    result = {}
    superseded = {ident for entry in data["operations"] if entry["kind"] == "plan"
                  for ident in entry["record"].get("supersedes", [])}
    for entry in data["operations"]:
        record = entry["record"]
        if entry["kind"] == "plan":
            if record["id"] in superseded:
                continue
            for item in record["items"]:
                if item.get("status") == "cancelled" and not item.get("task_ids"):
                    key, at = "item:" + item["id"], instant(record["effective_at"])
                    result[key] = min(result.get(key, at), at)
            continue
        if record.get("status") != "cancelled":
            continue
        if entry["kind"] == "resolve_commitment":
            key = record["key"]
        elif entry["kind"] == "intent" and record["kind"] == "action":
            key = "item:" + record["id"]
        else:
            continue
        at = instant(entry["recorded_at"])
        result[key] = min(result.get(key, at), at)
    return result
