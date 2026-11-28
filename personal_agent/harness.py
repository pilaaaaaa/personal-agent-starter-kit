"""Model-free Personal Agent protocol and business dispatch, independent of MCP and Git."""
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .admin_shadow import planning_advice
from .capture import FACTS, KEY, OBSERVATION_FIELDS
from .execution import SOURCES, bounds, build_context, monitor
from .inbox import FIELDS, operation
from .interaction import check_receipt, request_for
from .lifecycle import maintain_checkout, maintain_journal
from .sources import draft_inputs, method_directory, read_knowledge
from .store import UTC, digest, index, instant, read_snapshot, require
from .task_updates import update_tasks

PROTOCOLS = (
    "AGENTS.md", "docs/protocol.md", "docs/harness.md", "docs/operations.md",
    "methods/README.md",
)
JOURNAL_KINDS = {"plan", "intent", "correction", "delete_actual", "intake_commitments"}
REQUIRED = {
    "plan": {"day", "decision", "execution", "publication"},
    "intent": {"id", "kind", "status", "title", "evidence", "certainty"},
    "correction": {"version_id", "reason", "source"},
    "delete_actual": {"id", "reason"},
}


def protocol(root):
    knowledge = read_knowledge(root)
    return {
        "protocol_version": 1,
        "protocols": {name: (root / name).read_text() for name in PROTOCOLS},
        "knowledge": knowledge,
        "methods": {name: (method_directory(root) / (name + ".md")).read_text()
                    for name, entry in knowledge.get("methods", {}).items() if entry["enabled"]},
    }


def snapshot(root, zone, day=None, *, planning=False, availability=None):
    now = datetime.now(UTC)
    day = day or now.astimezone(zone).date().isoformat()
    require(date.fromisoformat(day).isoformat() == day, "Use YYYY-MM-DD")
    start, end = bounds(day, zone)
    settings = {
        "timezone": zone.key,
        "availability": [{"start": start.astimezone(zone).isoformat(), "end": end.astimezone(zone).isoformat()}],
        "sources": {name: {"status": "ok" if name == "conversation" else "projection_only",
                           "observed_at": now.isoformat(), "evidence": "Personal Agent snapshot"} for name in SOURCES},
    }
    if availability is not None:
        require(planning, "Explicit availability requires for_planning=true")
        settings["availability"] = availability
    data, tasks = read_snapshot(root / "personal_data/journal.json", root / "tasks.json")
    maintain_journal(data, tasks, now)
    knowledge = read_knowledge(root)
    if planning:
        settings = draft_inputs(day, settings, now)
    context = build_context(data, tasks, day, settings, now, knowledge)
    versions = index(data)[0].get(day, [])
    return {
        "day": day, "now": now.isoformat(), "inputs": settings, "tasks": tasks["tasks"],
        "task_revisions": {row["id"]: digest(row) for row in tasks["tasks"]},
        "plan_expected": versions[-1]["id"] if versions else None,
        "context": context,
        "administration": planning_advice(data, tasks, settings, now, context),
        "supervision": monitor(data, tasks, day, zone.key, now, knowledge=knowledge),
    }


def validate(batch):
    require(isinstance(batch, dict), "Batch must be an object")
    missing = sorted({"request_id", "conversation_id", "observed_at", "operations"} - batch.keys())
    if missing:
        raise KeyError(", ".join(missing))
    require(set(batch) == {"request_id", "conversation_id", "observed_at", "operations"},
            "Unexpected batch fields")
    require(isinstance(batch["request_id"], str) and KEY.fullmatch(batch["request_id"]), "Invalid request_id")
    require(isinstance(batch["observed_at"], str), "observed_at must be an RFC3339 string")
    require(isinstance(batch["conversation_id"], str) and 0 < len(batch["conversation_id"].strip()) <= 128,
            "Invalid conversation_id")
    require(instant(batch["observed_at"]) <= datetime.now(UTC), "observed_at must be a past RFC3339 timestamp")
    rows = batch["operations"]
    require(isinstance(rows, list) and 1 <= len(rows) <= 32, "Supply 1–32 operations")
    require(all(isinstance(row, dict) for row in rows), "Operations must be objects")
    seen = set()
    for row in rows:
        for name in ("kind", "key", "record"):
            if name not in row:
                raise KeyError(f"operation.{name}")
        kind, key, record = row["kind"], row["key"], row["record"]
        require(isinstance(key, str) and KEY.fullmatch(key) and key not in seen, "Invalid or duplicate operation key")
        seen.add(key)
        require(set(row) <= {"kind", "key", "record", "expected"} and isinstance(record, dict),
                "Unexpected operation fields")
        if kind == "actual":
            require("expected" not in row, "Actual observations use stable occurrence and correction evidence")
            require(set(record) <= OBSERVATION_FIELDS - {"key"}, "Unexpected Actual fields")
            fields = {"state", "evidence"}
            if record.get("state") in FACTS:
                fields.add("task_id" if record["state"].startswith("task_") else "name")
        elif kind == "task":
            require(set(record) <= {"task_id", "title", "day", "time", "notes", "deadline_day"},
                    "Unexpected task fields")
            fields = {"task_id"} if "task_id" in record else {"title"}
            if "task_id" in record and not row.get("expected"):
                raise KeyError(f"{key}.expected")
        elif kind == "preference":
            require(set(record) <= {"scope", "summary", "evidence"}, "Unexpected preference fields")
            fields = {"scope", "summary", "evidence"}
        else:
            require(kind in JOURNAL_KINDS and set(record) <= FIELDS[kind], "Unsupported operation or fields")
            fields = REQUIRED.get(kind, set())
            if kind in {"plan", "intent", "delete_actual"} and "expected" not in row:
                raise KeyError(f"{key}.expected")
            if kind == "plan" and not ({"items", "patch"} & record.keys()):
                raise KeyError(f"{key}.items or {key}.patch")
        missing = sorted(fields - record.keys())
        if missing:
            raise KeyError(f"{key}.record." + ", ".join(missing))


def append_knowledge(root, record, operation_id, day):
    require(record["scope"] in {"preferences", "decision_log"}, "Invalid knowledge scope")
    require(all(isinstance(record[key], str) and record[key].strip() for key in ("summary", "evidence")),
            "Knowledge requires a summary and explicit user evidence")
    path = root / "planning" / (record["scope"] + ".md")
    content = path.read_text()
    marker = f"<!-- {operation_id} -->"
    if marker not in content:
        summary, evidence = (" ".join(record[key].split()) for key in ("summary", "evidence"))
        path.write_text(content.rstrip() + f"\n\n- {day} · {summary} (user evidence: {evidence}) {marker}\n")


def apply_batch(root: Path, batch, zone: ZoneInfo):
    """Apply to a disposable checkout; callers publish only a successful batch."""
    maintain_checkout(root, datetime.now(UTC))
    operations = batch["operations"]
    turn = digest(batch["request_id"])[:32]
    outcomes = {}
    observations = [{"key": row["key"], **row["record"]} for row in operations if row["kind"] == "actual"]
    capture_done = False
    for row in operations:
        kind, key, record = row["kind"], row["key"], row["record"]
        ident = "pa-" + digest([turn, key])[:32]
        if kind == "actual":
            if not capture_done:
                request = request_for({**batch, "message_id": batch["request_id"], "timezone": zone.key}, observations)
                result = operation(root, request, request["id"])
                receipt = check_receipt(request, {**result, "id": request["id"]})
                outcomes.update(receipt["dispositions"])
                capture_done = True
        elif kind == "task":
            if record.get("task_id"):
                _, tasks = read_snapshot(root / "personal_data/journal.json", root / "tasks.json")
                target = next((task for task in tasks["tasks"] if task["id"] == record["task_id"]), None)
                require(target is not None and digest(target) == row["expected"],
                        "Task changed; read current state and use a new request_id")
            else:
                require(row.get("expected") is None, "New task uses expected=null")
            update_tasks(root, [{"key": key, **record}], turn, zone)
            outcomes[key] = {"status": "accepted", "task_id": record.get("task_id") or ident}
        elif kind == "preference":
            day = instant(batch["observed_at"]).astimezone(zone).date().isoformat()
            append_knowledge(root, record, ident, day)
            outcomes[key] = {"status": "accepted", "id": ident}
        else:
            if kind == "plan":
                require(isinstance(record["publication"], str) and record["publication"].strip(),
                        "Plan publication requires explicit user authorization")
            status = operation(root, {"id": ident, "kind": kind, "record": record,
                                      "expected": row.get("expected")}, ident)
            outcomes[key] = {"status": status, "id": ident}
    return outcomes
