"""Coverage of source text and versioned information units in the execution Journal."""
from collections import Counter
from datetime import date
import re

from .store import Conflict, digest, instant, require

KINDS = {"task", "commitment", "idea", "preference", "constraint", "actual",
         "completion", "waiting", "question"}
DISPOSITIONS = {"handled", "scheduled", "recorded", "waiting", "deferred",
                "no_action", "needs_confirmation", "conflict"}
TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def token(value):
    return isinstance(value, str) and bool(TOKEN.fullmatch(value))


def preview(request):
    """Check an independent source-span inventory against the host's semantic units."""
    source, text, spans, units = (request[key] for key in ("source", "text", "spans", "units"))
    require(isinstance(text, str) and 0 < len(text) <= 50_000, "Source text is required")
    require(token(source["id"]) and instant(source["observed_at"]), "Source identity and time required")
    coverage, cursor = {}, 0
    for span in spans:
        start, end = span["start"], span["end"]
        require(type(start) is int and type(end) is int and cursor == start < end <= len(text),
                "Source spans must exactly partition the text")
        owners = span["units"]
        require(isinstance(owners, list) and all(token(ident) for ident in owners)
                and len(set(owners)) == len(owners), "Invalid span owners")
        require(bool(owners) != bool(span.get("no_action")),
                "Every source span needs unit ownership or an explicit no-action reason")
        if not owners:
            require(isinstance(span["no_action"], str) and span["no_action"].strip(),
                    "No-action evidence is required")
        for ident in owners:
            coverage.setdefault(ident, []).append([start, end])
        cursor = end
    require(cursor == len(text), "Uncovered source text")
    require(Counter(coverage.keys()) == Counter(row["id"] for row in units),
            "Source spans and information unit identities differ")
    return {"source": {"id": source["id"], "observed_at": source["observed_at"],
                       "digest": digest(text), "length": len(text)},
            "spans": spans, "units": units}


def current(data):
    return {unit["id"]: {**unit, "version": entry["id"], "source": entry["record"]["source"]}
            for entry in data["operations"] if entry["kind"] == "intake_commitments"
            for unit in entry["record"].get("units", [])}


def independent_commitments(data):
    """Derive independent execution identities explicitly committed through intake."""
    return {unit["target"]: unit for unit in current(data).values()
            if unit.get("target") == "item:intake-" + unit["id"]}


def validate(data, tasks, record, now):
    """Validate an entire batch before the existing Journal writer appends it."""
    require(set(record) == {"source", "spans", "units"}, "Unexpected intake envelope fields")
    source, units = record["source"], record["units"]
    require(set(source) == {"id", "observed_at", "digest", "length"} and token(source["id"])
            and instant(source["observed_at"]) <= now and re.fullmatch("[0-9a-f]{64}", source["digest"])
            and type(source["length"]) is int and 0 < source["length"] <= 50_000,
            "Invalid intake source")
    for entry in data["operations"]:
        prior = entry["record"].get("source")
        if entry["kind"] == "intake_commitments" and isinstance(prior, dict) and prior["id"] == source["id"]:
            raise Conflict("Source message already recorded; use a new source ID for corrections")
    known = current(data)
    owners, cursor = set(), 0
    for span in record["spans"]:
        require(set(span) <= {"start", "end", "units", "no_action"}, "Unexpected source span fields")
        start, end = span["start"], span["end"]
        require(type(start) is int and type(end) is int and cursor == start < end <= source["length"],
                "Source spans must exactly partition the text")
        ids = span["units"]
        reason = span.get("no_action")
        require(isinstance(ids, list) and all(token(ident) for ident in ids)
                and len(ids) == len(set(ids))
                and ((ids and reason is None) or (not ids and isinstance(reason, str) and reason.strip())),
                "Every span needs valid owners or no-action evidence")
        owners.update(ids)
        cursor = end
    require(cursor == source["length"] and Counter(owners) == Counter(row["id"] for row in units),
            "Incomplete information coverage")
    keys = {"task:" + task["id"] for task in tasks["tasks"]}
    keys.update("item:" + item["id"] for entry in data["operations"] if entry["kind"] == "plan"
                for item in entry["record"]["items"] if not item.get("task_ids"))
    keys.update(item["id"] for entry in data["operations"] if entry["kind"] == "intake_commitments"
                for item in entry["record"].get("intake", []))
    keys.update(item["id"] for entry in data["operations"] if entry["kind"] == "plan"
                for item in entry["record"].get("execution", {}).get("inputs", {}).get("intake", []))
    independent = independent_commitments(data)
    keys.update(independent)
    from .intents import current_intents

    keys.update("item:" + ident for ident, intent in current_intents(data).items() if intent["kind"] == "action")
    keys.update("actual:" + entry["record"]["id"] for entry in data["operations"]
                if entry["kind"] == "actual")
    fields = {"id", "expected", "kind", "disposition", "summary", "day", "target",
              "blocking", "trigger_at", "expires_at", "depends_on", "window", "checkpoint"}
    for row in units:
        ident = row["id"]
        require(set(row) <= fields and row["kind"] in KINDS and row["disposition"] in DISPOSITIONS
                and isinstance(row["summary"], str) and 0 < len(row["summary"].strip()) <= 1000,
                "Invalid information unit")
        previous = known.get(ident)
        if row["expected"] != (previous["version"] if previous else None):
            raise Conflict(f"Information unit changed: {ident}")
        if previous and instant(source["observed_at"]) < instant(previous["source"]["observed_at"]):
            raise Conflict(f"Information source predates current unit: {ident}")
        if row.get("day"):
            require(date.fromisoformat(row["day"]).isoformat() == row["day"], "Use local YYYY-MM-DD")
        require(type(row.get("blocking", False)) is bool, "Blocking flag must be boolean")
        require(not row.get("blocking") or row["disposition"] in {"needs_confirmation", "conflict"},
                "Blocking flag requires unresolved evidence")
        for field in ("trigger_at", "expires_at"):
            if field in row:
                require(instant(row[field]), f"Missing {field}")
        require(not all(row.get(k) for k in ("trigger_at", "expires_at"))
                or instant(row["trigger_at"]) < instant(row["expires_at"]), "Trigger must precede expiry")
        if row["disposition"] in {"scheduled", "deferred"}:
            raise ValueError("Scheduling and deferral dispositions are assigned by validated Plan")
        target = row.get("target")
        if target is not None:
            own = target == "item:intake-" + ident
            if own and target not in independent:
                require(target not in keys, "Independent identity collides with a legacy commitment")
                require(row["kind"] in {"task", "commitment"} and row["disposition"] == "recorded"
                        and (row.get("day") or row.get("checkpoint", {}).get("lane")),
                        "New independent identity requires an explicit dated commitment or operational lane")
            else:
                require(isinstance(target, str) and target in keys, "Unknown information target")
            if target.startswith("actual:"):
                require(row["kind"] in {"actual", "completion"}, "Only Actual evidence can reference an activity")
            if own:
                require(row["kind"] in {"task", "commitment"}
                        and (row.get("day") or previous and not previous.get("day")
                             or row.get("checkpoint", {}).get("lane")),
                        "Independent commitment must retain its kind and date or operational lane")
        if previous and previous.get("target"):
            require(previous["target"] == row.get("target"), "Existing information target cannot be reassigned")
        if "checkpoint" in row:
            from .admin_checkpoint import validate as validate_checkpoint

            validate_checkpoint(row["checkpoint"], target, instant(source["observed_at"]))
        window = row.get("window")
        if window is not None:
            require(row["kind"] == "constraint" and set(window) == {"start", "end", "hard"}
                    and type(window["hard"]) is bool and instant(window["start"]) < instant(window["end"]),
                    "Invalid constraint window")
        deps = row.get("depends_on", [])
        require(isinstance(deps, list) and all(key in keys for key in deps)
                and row.get("target") not in deps and len(set(deps)) == len(deps), "Invalid dependencies")


def plan_scope(information, day, now):
    """Keep dated closed evidence for its day and active evidence until resolved."""
    return {ident: row for ident, row in information.items()
            if (not row.get("expires_at") or instant(row["expires_at"]) > now)
            and ((row.get("day") or day) >= day or row["disposition"] not in {"handled", "no_action"})}


def plan_coverage(information, acknowledgements, dispositions, closed, day):
    require(information.keys() == acknowledgements.keys(),
            f"Information coverage failed: missing={sorted(information.keys() - acknowledgements.keys())}, "
            f"unexpected={sorted(acknowledgements.keys() - information.keys())}")
    outcomes = {}
    for ident, row in information.items():
        receipt = acknowledgements[ident]
        require(set(receipt) <= {"version", "reason", "revisit"}
                and receipt["version"] == row["version"], f"Information version changed: {ident}")
        require(not row.get("blocking") or (row.get("day") and row["day"] > day),
                f"Blocking information must be clarified: {ident}")
        status, target = row["disposition"], row.get("target")
        execution = dispositions.get(target, {}).get("status")
        if status == "recorded" and row["kind"] in {"task", "commitment"}:
            status = {"scheduled": "scheduled", "deferred": "deferred",
                      "in_progress": "handled", "conflict": "conflict"}.get(execution, "recorded")
            if target in closed:
                status = "handled"
            if status == "recorded":
                require(all(isinstance(receipt.get(key), str) and receipt[key].strip()
                            for key in ("reason", "revisit")), f"Unscheduled information needs reason and trigger: {ident}")
        for key in ("reason", "revisit"):
            if key in receipt:
                require(isinstance(receipt[key], str) and receipt[key].strip(), f"Invalid coverage {key}: {ident}")
        outcomes[ident] = {**receipt, "disposition": status, "source": row["source"]["id"]}
    return outcomes


def constraint_windows(information, zone):
    return [{"id": "intake:" + ident, "evidence": row["source"]["id"],
             "kind": "temporary_hard" if row["window"]["hard"] else "temporary_soft",
             **{key: instant(row["window"][key]).astimezone(zone).isoformat() for key in ("start", "end")}}
            for ident, row in sorted(information.items())
            if row["kind"] == "constraint" and row["disposition"] == "recorded" and row.get("window")]
