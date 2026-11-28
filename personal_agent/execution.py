"""Conserved execution context, plan validation, and read-only progress reporting."""
from datetime import date, datetime, time, timedelta
from itertools import combinations
import math
from statistics import median
from zoneinfo import ZoneInfo

from .lifecycle import date_risk, deadline_cutoff, waiting_tasks
from .intents import cancellations, current_intents
from .store import UTC, digest, effective, index, instant, require, union_seconds, validate_interval

TERMINAL = {"completed", "done", "cancelled"}
MAX_AGE = timedelta(minutes=30)
DISPOSITIONS = {"scheduled", "in_progress", "unscheduled", "conflict", "deferred", "external"}
SOURCES = {"plan_calendar", "actual_calendar", "conversation"}


def identity(item):
    ids = item.get("task_ids", [])
    require(isinstance(ids, list) and len(ids) <= 1, "Each block needs at most one task ID")
    return f"task:{ids[0]}" if ids else f"item:{item['id']}"


def evidence(record):
    require(isinstance(record.get("evidence"), str) and record["evidence"].strip(),
            "Evidence reference required")


def bounds(day, zone):
    first = date.fromisoformat(day)
    return tuple(datetime.combine(value, time.min, zone).astimezone(UTC)
                 for value in (first, first + timedelta(days=1)))


def overlaps(left, right):
    return max(left[0], right[0]) < min(left[1], right[1])


def build_context(data, tasks, day, settings, now, knowledge=None):
    """A deterministic inventory and revision, with time-sensitive evidence checked separately."""
    require(set(settings) <= {"timezone", "availability", "sources", "intake", "constraints",
                              "calendar_commitments", "calendar_constraints", "actual_evidence", "intake_sources"},
            "Unexpected execution input fields")
    zone = ZoneInfo(settings["timezone"])
    start, end = bounds(day, zone)
    plans, actual, _, feedback, _, completions = index(data)
    pool, closed, task_rows, ongoing, legacy_slots = {}, {}, {}, set(), {}
    conflicts = {}
    observed_completions = {
        entry["id"]: instant(entry["record"].get("capture_observed_at"))
        for entry in data["operations"] if entry["kind"] == "actual"
    }
    for task in tasks["tasks"]:
        raw = task["id"]
        require(isinstance(raw, str) and raw.strip() and raw not in task_rows,
                f"Invalid or duplicate task ID: {raw}")
        task_rows[raw] = task
        key = "task:" + raw
        status = task["status"]
        require(status in TERMINAL | {"todo"}, f"Invalid task status: {key}")
        receipt = task.get("activity_completion_receipt")
        observed_status = instant(task.get("capture_task_observed_at"))
        observed_completion = observed_completions.get(completions.get(raw))
        status_supersedes_actual = bool(observed_status and observed_completion and observed_status > observed_completion)
        inconsistent = not status_supersedes_actual and (
            (raw in completions and status not in {"done", "completed"}) or bool(
                receipt and receipt != completions.get(raw) and task.get("completion_source") in {None, "actual"}))
        if inconsistent:
            conflicts[key] = "Task status and explicit Actual completion disagree"
        if status in TERMINAL and not inconsistent:
            closed[key] = status
            continue
        row = pool[key] = {"title": task.get("title", raw), "sources": ["tasks"], "day": None}
        row["due"] = task.get("due")
        row["task"] = task
        row["deadline"] = task.get("deadline")
        row["overdue"] = date_risk(task, now)
        row["day"] = task.get("execution_date")
        if row["due"] and not row["day"] and not task.get("reminder_fixed"):
            row["day"] = date.fromisoformat(row["due"]["date"]).isoformat()
        if task.get("start_time"):
            scheduled_day = instant(task["start_time"]).astimezone(zone).date().isoformat()
            if not task.get("execution_date"):
                row["day"] = min(row["day"], scheduled_day) if row["day"] else scheduled_day
            if task.get("end_time") and instant(task["end_time"]) > now:
                legacy_slots[key] = {"start": task["start_time"], "end": task["end_time"]}

    for record in actual.values():
        if not record.get("deleted") and record.get("status") == "ongoing":
            ongoing.update("task:" + ident for ident in record.get("task_ids", []))
            ongoing.update("item:" + link["item_id"] for link in record.get("plan_links", []))
        if not record.get("deleted") and record.get("status") == "completed":
            for link in record.get("plan_links", []):
                closed["item:" + link["item_id"]] = "completed"

    prior_items, published, authorized, previously_known = {}, {}, {}, set()
    for entry in data["operations"]:
        record = entry["record"]
        if entry["kind"] == "authorize_execution":
            authorized[entry["id"]] = record
            continue
        if entry["kind"] == "resolve_commitment":
            closed[record["key"]] = record["status"]
            continue
        if entry["kind"] == "intake_commitments":
            published.update((row["id"], row) for row in record.get("intake", []))
            continue
        if entry["kind"] != "plan":
            continue
        execution = record.get("execution", {})
        previously_known.update(key for key in execution.get("dispositions", {}) if key.startswith("task:"))
        for addition in execution.get("inputs", {}).get("intake", []):
            published[addition["id"]] = addition
        # Every independently committed block remains in inventory, even if a legacy
        # version dropped it. Task-linked blocks reconcile through the stable task ID.
        for item in record["items"]:
            key = identity(item)
            if key.startswith("task:") and key[5:] not in task_rows:
                conflicts[key] = "Task referenced by Journal is missing from tasks.json"
            prior_items[key] = (record["day"], item)

    receipts = settings["sources"]
    require(set(receipts) == SOURCES, "Source receipts required for both calendars and conversation")
    for name, receipt in receipts.items():
        require(set(receipt) <= {"status", "evidence", "observed_at", "checksum"},
                f"Unexpected source fields: {name}")
        evidence(receipt)
        statuses = {"ok", "failed"} if name == "conversation" else {"ok", "failed", "projection_only"}
        require(receipt.get("status") in statuses, f"Invalid source status: {name}")
        observed = instant(receipt.get("observed_at"))
        require(observed is not None, f"Observation time required: {name}")
    incoming = settings.get("intake", [])
    seen = set()
    for addition in incoming:
        key = addition["id"]
        require(isinstance(key, str) and key.startswith("item:") and key[5:].strip()
                and key not in seen, f"Invalid or duplicate intake identity: {key}")
        require(isinstance(addition.get("title"), str) and addition["title"].strip(), f"Missing title: {key}")
        require(date.fromisoformat(addition["day"]).isoformat() == addition["day"], f"Invalid intake date: {key}")
        require(set(addition) == {"id", "title", "day", "evidence"}, f"Unexpected intake fields: {key}")
        evidence(addition)
        require(key not in published or addition == published[key], f"Intake identity already used: {key}")
        seen.add(key)
        published[key] = addition
    for key in sorted(previously_known):
        if key not in pool and key not in closed:
            pool[key] = {"title": key, "sources": ["prior_execution"], "day": day}
            conflicts[key] = "Previously known task is missing from tasks.json"
    for key, (_, item) in prior_items.items():
        if key.startswith("item:") and item.get("status") == "cancelled":
            closed.setdefault(key, "cancelled")
    from .intake import constraint_windows, current as current_information, independent_commitments, plan_scope

    for key, unit in independent_commitments(data).items():
        published[key] = {"title": unit["summary"], "day": unit.get("day"),
                          "evidence": "intake:" + unit["source"]["id"]}
    cancelled = {key for key, at in cancellations(data).items() if at <= now}
    closed.update(dict.fromkeys(cancelled, "cancelled"))
    for key, addition in published.items():
        if key not in closed:
            pool[key] = {"title": addition["title"], "day": addition["day"],
                         "sources": ["intake"], "evidence": addition["evidence"]}
    for key, (item_day, item) in prior_items.items():
        if key in closed:
            continue
        row = pool.setdefault(key, {"title": item["title"], "sources": [], "day": None})
        row["sources"].append("plan")
        if not row.get("task", {}).get("execution_date"):
            row["day"] = min(row["day"], item_day) if row["day"] else item_day

    intents = current_intents(data)
    for ident, fact in intents.items():
        if fact["kind"] != "action":
            continue
        key = "item:" + ident
        if fact["status"] != "open":
            closed[key] = fact["status"]
        if key in cancelled:
            closed[key] = "cancelled"
        if key in closed:
            pool.pop(key, None)
            continue
        expired = fact.get("expires_at") and instant(fact["expires_at"]) <= max(start, now)
        if expired and key not in pool:
            continue
        row = pool.setdefault(key, {"title": fact["title"], "day": fact.get("day"), "sources": []})
        row["title"] = fact["title"]
        row["sources"].append("intent")
        row["intent"] = fact
        row["optional"] = fact["certainty"] != "confirmed" and key not in prior_items and key not in published

    available = []
    following_end = bounds((date.fromisoformat(day) + timedelta(days=1)).isoformat(), zone)[1]
    for window in settings["availability"]:
        left, right = validate_interval(window, zone)
        require(left and right and start <= left < end and right <= following_end,
                "Availability must start on the plan day and end by the following midnight")
        available.append((left, right))
    for left, right in combinations(available, 2):
        require(not overlaps(left, right), "Duplicate or overlapping availability windows")

    horizon = (start, max([end, *(right for _, right in available)]))
    applicable = {}
    for ident, fact in intents.items():
        if fact["kind"] == "action" or fact["status"] != "open":
            continue
        if fact.get("day") and fact["day"] != day and (
                not fact.get("window") or not overlaps(horizon, validate_interval(fact["window"]))):
            continue
        if fact.get("expires_at") and instant(fact["expires_at"]) <= max(start, now):
            continue
        applicable[ident] = fact
        if fact.get("target") in pool:
            pool[fact["target"]].setdefault("details", []).append(fact)

    active = {}
    # Use effective versions for live monitoring and the latest submitted version
    # for replacement protection, including versions still queued to take effect.
    latest, deferred = {}, {}
    for plan_day, history in plans.items():
        current = effective(history, now)
        if current:
            for key, disposition in current.get("execution", {}).get("dispositions", {}).items():
                if disposition["status"] == "deferred" and disposition["until_day"] > day:
                    deferred[key] = max(deferred.get(key, ""), disposition["until_day"])
            active.update((identity(item), item) for item in current["items"]
                          if item.get("status", "scheduled") == "scheduled"
                          and identity(item) not in cancelled)
        latest[plan_day] = {identity(item): item for item in history[-1]["items"]
                            if item.get("status", "scheduled") == "scheduled"
                            and identity(item) not in cancelled}

    formal_keys = {key for rows in latest.values() for key in rows}
    legacy_slots = {key: slot for key, slot in legacy_slots.items() if key not in formal_keys}

    model = {}
    for record in actual.values():
        if (record.get("deleted") or record.get("effort") == "passive"
                or record.get("status") != "completed" or record.get("precision") != "exact"):
            continue
        left, right = validate_interval(record)
        if record.get("complete_task") and left and right:
            model.setdefault("task:" + record["complete_task"], []).append((right - left).total_seconds() / 60)
    calibration = {key: {"samples": len(values), "median_minutes": median(values)}
                   for key, values in model.items()}
    information = current_information(data)
    scoped = plan_scope(information, day, now)
    waiting = {"task:" + ident for ident in waiting_tasks(information, now)}
    intake_sources = settings.get("intake_sources", [])
    processed = {entry["record"]["source"]["id"] for entry in data["operations"]
                 if entry["kind"] == "intake_commitments" and "source" in entry["record"]}
    require(isinstance(intake_sources, list) and all(ident in processed for ident in intake_sources)
            and len(set(intake_sources)) == len(intake_sources), "Conversation intake source is missing or duplicated")
    for row in scoped.values():
        target, planned_day = row.get("target"), row.get("day")
        if (target in pool and planned_day and row["kind"] in {"task", "commitment"}
                and row["disposition"] == "recorded"):
            entry = pool[target]
            if not entry.get("task", {}).get("execution_date"):
                entry["day"] = min(entry["day"], planned_day) if entry["day"] else planned_day
    standing_rules = constraint_windows(scoped, zone)
    intent_constraints = [
        {**fact["window"], "id": "intent:" + fact["id"], "evidence": fact["evidence"],
         "kind": "temporary_" + fact["window"]["kind"]}
        for fact in applicable.values() if fact.get("window") and fact["certainty"] == "confirmed"
    ]
    constraints = constraints_for(settings) + standing_rules + intent_constraints
    constraint_ids = set()
    for rule in constraints:
        require(rule["id"] not in constraint_ids, f"Duplicate constraint: {rule['id']}")
        constraint_ids.add(rule["id"])
        require(rule["kind"] in {"hard", "soft", "temporary_hard", "temporary_soft"},
                f"Invalid constraint kind: {rule['id']}")
        evidence(rule)
        left, right = validate_interval(rule, zone if not rule["id"].startswith("intent:") else None)
        require(left and right, f"Constraint requires both boundaries: {rule['id']}")
    for linked in settings.get("calendar_commitments", []):
        key = linked["key"]
        require(key.startswith("calendar:") and key not in pool, f"Duplicate calendar event: {key}")
        pool[key] = {"title": linked["title"], "day": day, "sources": ["plan_calendar"],
                     "slot": {field: linked[field] for field in ("start", "end") if field in linked}}
    inputs = {"journal": digest(data), "tasks": digest(tasks), "day": day,
              "settings": settings, "knowledge": knowledge or {}}
    current_plan = effective(plans.get(day, []), now) or {}
    current_execution = current_plan.get("execution", {})
    impact = replan_impact(latest.get(day, {}), constraints, current_execution, scoped, now)
    return {"information": information, "information_scope": scoped, "constraints": constraints,
            "replan_impact": impact,
            "planned_timing": current_plan.get("execution", {}).get("assessment", {}).get("timing", {}),
            "revision": digest(inputs), "captured_at": now.isoformat(), "day": day,
            "pool": pool, "closed": closed, "conflicts": conflicts,
            "due": sorted(key for key, row in pool.items() if (key not in waiting and row["day"] and row["day"] <= day and not row.get("optional")) or row.get("overdue")
                          or (row.get("deadline") and row["deadline"]["date"] <= day)),
            "waiting_tasks": sorted(waiting & pool.keys()),
            "overdue_deadlines": sorted(key for key, row in pool.items() if row.get("overdue")),
            "ongoing": sorted(ongoing & pool.keys()), "active": active, "latest": latest,
            "prior_items": {key: item for key, (_, item) in prior_items.items()},
            "legacy_slots": legacy_slots, "deferred_tasks": deferred,
            "intents": intents, "applicable_details": applicable,
            "sources": receipts,
            "plan_versions": plans, "candidates": candidate_rows(
                pool, {**legacy_slots, **active}, ongoing, conflicts, deferred, day, now),
            "actual": actual, "calibration": calibration, "authorizations": authorized,
            "knowledge": knowledge or {}, "active_feedback": [
                item for item in feedback.values() if item["status"] == "active"
                and (not item.get("expires_at") or instant(item["expires_at"]) > now)]}


def replan_impact(current, rules, previous, information, now):
    """Report directly affected future blocks and dependent work, retaining other identities."""
    reasons = {}
    past = previous.get("assessment", {}).get("information", {})
    for key, block in current.items():
        left, right = validate_interval(block)
        if right <= now:
            continue
        causes = [rule["id"] for rule in rules if rule["kind"].endswith("hard")
                  and overlaps((max(now, left), right), validate_interval(rule))]
        causes += ["intake:" + ident for ident, row in information.items()
                   if row.get("target") == key and past.get(ident, {}).get("version") != row["version"]]
        if causes:
            reasons[key] = sorted(set(causes))
    pending = {key: set(row.get("depends_on", [])) for key, row in previous.get("budgets", {}).items()}
    while True:
        downstream = {key: sorted(deps & reasons.keys()) for key, deps in pending.items()
                      if key not in reasons and deps & reasons.keys()}
        if not downstream:
            break
        reasons.update(downstream)
    return {"affected": reasons,
            "preserve": sorted(key for key, block in current.items()
                               if key not in reasons and instant(block["end"]) > now)}


def candidate_rows(pool, slots, ongoing, conflicts, deferred, day, now):
    rows = []
    for key, row in pool.items():
        slot = slots.get(key) or row.get("slot")
        if key in conflicts:
            status, reason = "conflict", conflicts[key]
        elif key.startswith("calendar:"):
            status, reason = "external", "Fixed on source calendar"
        elif key in ongoing:
            status, reason = "in_progress", "Reported in Actual"
        elif slot and instant(slot["end"]) > now:
            status, reason = "scheduled", "Existing schedule"
        elif key in deferred:
            status, reason = "deferred", f"Deferred until {deferred[key]} by an effective Plan"
        elif row["day"] and row["day"] > day:
            status, reason = "unscheduled", f"Recorded for {row['day']}"
        elif row.get("optional"):
            status, reason = "unscheduled", "Conversation intent awaiting selection or clarification"
        else:
            status, reason = "unscheduled", "Open item awaiting allocation"
        rows.append({"key": key, "title": row["title"], "status": status, "reason": reason,
                     "slot": slot, "day": row["day"], "sources": row["sources"],
                     "intent": row.get("intent"), "details": row.get("details", [])})
    return rows


def build_draft(context, edits):
    """Apply ordered, reversible choices to a complete read-only inventory."""
    candidates = {row["key"]: row for row in context["candidates"]}
    ordered = {}
    for edit in edits:
        key, status = edit["key"], edit["status"]
        require(key in candidates and key not in ordered, f"Unknown or duplicate candidate: {key}")
        require(set(edit) <= {"key", "status", "reason", "start", "end", "until_day", "assumptions"}
                and status in {"suggested", "omitted", "deferred", "unscheduled", "conflict"},
                f"Invalid draft choice: {key}")
        evidence({"evidence": edit.get("reason")})
        if "start" in edit or "end" in edit:
            require(all(validate_interval(edit)), f"Suggested slot needs both boundaries: {key}")
        if "until_day" in edit:
            date.fromisoformat(edit["until_day"])
        ordered[key] = {**candidates[key], "choice": edit}
    return {"stage": "recommended" if edits else "candidate", "revision": context["revision"],
            "day": context["day"], "details": context["applicable_details"],
            "constraints": context["constraints"], "sources": context["sources"],
            "items": list(ordered.values()) +
            [row for key, row in candidates.items() if key not in ordered]}


def constraints_for(settings):
    return settings.get("constraints", []) + settings.get("calendar_constraints", [])


def available_minutes(settings, now, include_soft=False, *, constraints=None):
    windows = [(max(instant(window["start"]), now), instant(window["end"]))
               for window in settings["availability"]]
    blocked = [validate_interval(rule) for rule in (constraints_for(settings) if constraints is None else constraints)
               if include_soft or rule["kind"].endswith("hard")]
    total = 0
    for left, right in windows:
        if left < right:
            total += (right - left).total_seconds() - union_seconds(
                [(max(left, a), min(right, b)) for a, b in blocked if overlaps((left, right), (a, b))])
    return total / 60


def minutes(value):
    return type(value) in {int, float} and math.isfinite(value) and value > 0


def check_budget(key, budget, context):
    low, normal, high = (budget[name] for name in ("optimistic", "normal", "conservative"))
    require(all(minutes(value) for value in (low, normal, high)) and low <= normal <= high,
            f"Invalid duration budget: {key}")
    require(budget["source"] in {"user", "estimate", "history"}, f"Invalid duration source: {key}")
    evidence(budget)
    reference = context["calibration"].get(key)
    if budget["source"] == "history":
        require(reference, f"No historical duration samples: {key}")
    if (reference and budget["source"] != "user" and key not in context["ongoing"]):
        require(normal >= reference["median_minutes"],
                f"Estimate below measured median: {key}")
    return normal


def authorized(context, key, disposition, day):
    auth = context["authorizations"].get(disposition.get("authorization"))
    return bool(auth and auth["key"] == key and auth["day"] == day
                and auth["until_day"] == disposition.get("until_day"))


def expand_patch(data, record):
    """Resolve stable-ID changes against the latest submitted version."""
    if "patch" not in record:
        return record
    patch = record["patch"]
    require("items" not in record and set(patch) == {"base_version", "upsert", "remove"},
            "Patch requires a base version, upserts and removals")
    history = index(data)[0].get(record["day"], [])
    require(history and history[-1]["id"] == patch["base_version"], "Plan patch base changed")
    items = {item["id"]: item for item in history[-1]["items"]}
    upserts = {item["id"]: item for item in patch["upsert"]}
    removed = set(patch["remove"])
    require(len(upserts) == len(patch["upsert"]) and len(removed) == len(patch["remove"])
            and not removed & upserts.keys() and removed <= items.keys(), "Invalid Plan patch IDs")
    return {**{key: value for key, value in record.items() if key != "patch"},
            "items": [item for key, item in {**items, **upserts}.items() if key not in removed]}


def validate_plan(data, tasks, record, now, knowledge=None):
    record = expand_patch(data, record)
    proposal = record.get("execution")
    require(isinstance(proposal, dict), "Plan requires execution context and dispositions")
    settings = proposal["inputs"]
    context = build_context(data, tasks, record["day"], settings, now, knowledge)
    require(set(proposal) <= {"inputs", "revision", "captured_at", "dispositions", "budgets", "information", "local_replan"},
            "Unexpected execution fields")
    require(proposal.get("revision") == context["revision"], "Execution context changed; rebuild snapshot")
    captured = instant(proposal.get("captured_at"))
    require(captured and timedelta(0) <= now - captured <= MAX_AGE, "Execution snapshot missing or stale")
    for name, receipt in settings["sources"].items():
        require(receipt["status"] != "failed", f"Required source failed: {name}")
        require(timedelta(0) <= now - instant(receipt["observed_at"]) <= MAX_AGE,
                f"Source observation missing or stale: {name}")

    from .intake import plan_coverage

    dispositions = proposal["dispositions"]
    information = plan_coverage(context["information_scope"], proposal.get("information", {}),
                                dispositions, context["closed"], record["day"])
    methods = record.get("decision", {}).get("methods", [])
    catalog = (knowledge or {}).get("methods")
    require(isinstance(methods, list) and all(isinstance(name, str) for name in methods)
            and len(set(methods)) == len(methods), "Decision methods must be unique identifiers")
    if methods:
        require(catalog is not None and all(catalog.get(name, {}).get("enabled") for name in methods),
                "Decision references an unavailable or disabled method")
    pool = context["pool"]
    missing, extra = sorted(pool.keys() - dispositions.keys()), sorted(dispositions.keys() - pool.keys())
    require(not missing and not extra, f"Task conservation failed: missing={missing}, unexpected={extra}")
    zone = ZoneInfo(settings["timezone"])
    day = record["day"]
    first, end = bounds(day, zone)
    require(set(record) <= {"day", "effective_at", "items", "decision", "execution", "publication"},
            "Unexpected plan fields")
    effective_at = instant(record.get("effective_at")) or now
    available = [validate_interval(window) for window in settings["availability"]]
    scheduled, ids, prior = {}, {}, context["latest"].get(day, {})
    all_ids, retained_completed = set(), {}
    for item in record["items"]:
        require(set(item) <= {"id", "title", "start", "end", "status", "notes", "task_ids", "parallel_to"}
                and isinstance(item["id"], str) and item["id"].strip() and item["id"] not in all_ids,
                "Invalid or duplicate Plan item fields")
        all_ids.add(item["id"])
        key = identity(item)
        interval = validate_interval(item)
        require(all(interval), f"Plan block needs both boundaries: {key}")
        preserved = item == context["active"].get(key) or item == prior.get(key)
        if preserved and key not in scheduled:
            if interval[1] <= now:
                continue
            if context["closed"].get(key) in {"completed", "done"}:
                retained_completed[key] = item, interval
                continue  # Keep the unchanged Plan block while Actual owns completion state.
        require(item.get("status", "scheduled") == "scheduled", f"Use execution disposition for {key}")
        require(key not in scheduled and item["id"] not in ids, f"Duplicate plan block: {key}")
        previous = prior.get(key) or context["prior_items"].get(key)
        require(previous is None or previous["id"] == item["id"],
                f"Reuse the existing plan block ID: {key}")
        require(key in pool,
                f"Closed or unknown task cannot be scheduled: {key}")
        require(first <= interval[0], f"Block is assigned to the wrong local day: {key}")
        active = context["active"].get(key)
        same_slot = (active is not None and item["id"] == active["id"]
                     and interval == validate_interval(active))
        require(interval[0] >= effective_at or same_slot,
                f"New or changed plan may not claim elapsed time: {key}")
        require(any(left <= max(now, interval[0]) and interval[1] <= right for left, right in available),
                f"Block outside available time: {key}")
        for other_day, items in context["latest"].items():
            if other_day != day and key in items and instant(items[key]["end"]) > now:
                raise ValueError(f"Existing commitment on {other_day} prevents duplicate schedule: {key}")
        scheduled[key] = item, interval
        ids[item["id"]] = key

    retained_parents = {item["id"]: (item, interval) for item, interval in retained_completed.values()}
    for key, (item, interval) in scheduled.items():
        if item.get("parallel_to"):
            parent_id = item["parallel_to"]
            parent = scheduled.get(ids.get(parent_id)) or retained_parents.get(parent_id)
            require(parent and not parent[0].get("parallel_to") and parent[1][0] <= interval[0]
                    and interval[1] <= parent[1][1], f"Invalid parallel interval: {key}")
    for (key, (item, interval)), (other, (other_item, other_interval)) in combinations(scheduled.items(), 2):
        compatible = item.get("parallel_to") == other_item["id"] or other_item.get("parallel_to") == item["id"]
        require(compatible or not overlaps(interval, other_interval), f"Schedule overlap: {key}, {other}")
    for key, (item, interval) in scheduled.items():
        for other, (completed_item, completed_interval) in retained_completed.items():
            compatible = (item.get("parallel_to") == completed_item["id"]
                          or completed_item.get("parallel_to") == item["id"])
            require(compatible or not overlaps(interval, completed_interval),
                    f"Retained completed Plan block overlaps: {key}, {other}")
    for other_day, items in context["latest"].items():
        if other_day != day:
            for other, item in items.items():
                if instant(item["end"]) > now:
                    for key, (_, interval) in scheduled.items():
                        require(not overlaps(interval, validate_interval(item)),
                                f"Cross-day schedule overlap: {key}, {other}")

    for other, slot in context["legacy_slots"].items():
        if other not in scheduled:
            for key, (_, interval) in scheduled.items():
                require(not overlaps(interval, validate_interval(slot)),
                        f"Existing legacy commitment overlaps: {key}, {other}")

    require(type(proposal.get("local_replan", False)) is bool, "local_replan must be boolean")
    if proposal.get("local_replan"):
        for key in context["replan_impact"]["preserve"]:
            prior_block = context["latest"][day][key]
            require(key in scheduled and scheduled[key][0] == prior_block,
                    f"Local replan must preserve unaffected block: {key}")
    soft_conflicts = []
    for key, (_, interval) in scheduled.items():
        for rule in context["constraints"]:
            if overlaps((max(now, interval[0]), interval[1]), validate_interval(rule)):
                require(not rule["kind"].endswith("hard"), f"Hard constraint conflict: {key}, {rule['id']}")
                soft_conflicts.append([key, rule["id"]])
    elsewhere = {key for other_day, items in context["latest"].items() if other_day != day
                 for key, item in items.items() if instant(item["end"]) > now}
    elsewhere.update(context["legacy_slots"].keys() - scheduled.keys())
    required = {key for key in context["due"] if not key.startswith("calendar:")}
    required |= set(scheduled) | set(context["ongoing"])
    budgeted = {key for key, row in pool.items() if not key.startswith("calendar:")
                and (not row.get("optional") or key in required)}
    allowed_budgets = budgeted | {key for key, row in pool.items() if row.get("optional")}
    demand = 0
    budgets = proposal["budgets"]
    require(budgeted <= budgets.keys() <= allowed_budgets,
            f"Time budget coverage failed: missing={sorted(budgeted - budgets.keys())}, "
            f"unexpected={sorted(budgets.keys() - allowed_budgets)}")
    timing = {}
    for key, budget in budgets.items():
        normal = check_budget(key, budget, context)
        if key in required and not (key in scheduled and scheduled[key][0].get("parallel_to")):
            if dispositions.get(key, {}).get("status") != "deferred":
                demand += normal
        item, interval = scheduled.get(key, (None, None))
        if item:
            require((interval[1] - max(now, interval[0])).total_seconds() / 60 >= normal,
                    f"Scheduled duration shorter than remaining estimate: {key}")
        row = pool.get(key, {})
        deadline = deadline_cutoff(row["deadline"]).astimezone(UTC) if row.get("deadline") else None
        latest = max((right for _, right in available), default=end) if key in required else None
        if deadline:
            latest = min(latest, deadline) if latest else deadline
        planned_finish = max(now, interval[0]) + timedelta(minutes=normal) if interval else None
        timing[key] = {"latest_start": (latest - timedelta(minutes=normal)).isoformat() if latest else None,
                       "expected_finish": planned_finish.isoformat() if planned_finish else None,
                       "deadline": row.get("deadline"), "overdue": row.get("overdue", False),
                       "deadline_miss_minutes": max(0, (planned_finish - deadline).total_seconds() / 60)
                       if planned_finish and deadline else None}

    for key, disposition in dispositions.items():
        status = disposition["status"]
        require(set(disposition) <= {"status", "evidence", "authorization", "until_day"},
                f"Unexpected disposition fields: {key}")
        require(status in DISPOSITIONS, f"Invalid disposition: {key}")
        if status == "scheduled" or key in scheduled or key in elsewhere and key not in context["conflicts"]:
            require((key in scheduled or key in elsewhere) and status in {"scheduled", "in_progress"},
                    f"Scheduled block/disposition mismatch: {key}")
        require((status == "in_progress") == (key in context["ongoing"]) or
                key in context["conflicts"] and status == "conflict", f"Actual progress mismatch: {key}")
        if key in context["conflicts"]:
            require(status == "conflict", f"Resolve status conflict first: {key}")
        if status in {"conflict", "deferred"} or (status == "unscheduled" and (key in required or "intent" in pool[key]["sources"])):
            evidence(disposition)
        if status == "deferred":
            require(authorized(context, key, disposition, day), f"Unauthorized deferral: {key}")
        require((status == "external") == key.startswith("calendar:"),
                f"External calendar event must remain on its source calendar: {key}")
        old = prior.get(key)
        if old and instant(old["end"]) > now and key not in scheduled:
            require(status in {"conflict", "deferred", "in_progress"},
                    f"Existing commitment would be silently removed: {key}")
            if status == "conflict":
                evidence(disposition)
                future = (max(now, instant(old["start"])), instant(old["end"]))
                blockers = [validate_interval(rule) for rule in context["constraints"]
                            if rule["kind"].endswith("hard")]
                require(key in context["conflicts"] or any(overlaps(future, block) for block in blockers),
                        f"Existing commitment requires a concrete conflict or authorization: {key}")

    capacity = available_minutes(settings, now, constraints=context["constraints"])
    preferred_capacity = available_minutes(settings, now, include_soft=True, constraints=context["constraints"])
    conservative = sum(budgets[key]["conservative"] for key in required
                       if dispositions[key]["status"] != "deferred"
                       and not (key in scheduled and scheduled[key][0].get("parallel_to")))
    ranked = sorted(scheduled, key=lambda key: scheduled[key][1][0])
    for key, (_, interval) in scheduled.items():
        dependencies = set(budgets[key].get("depends_on", []))
        dependencies.update(dep for row in context["information_scope"].values()
                            if row.get("target") == key and row["kind"] in {"task", "commitment"}
                            and row["disposition"] == "recorded" for dep in row.get("depends_on", []))
        for dependency in sorted(dependencies):
            require(dependency != key and (context["closed"].get(dependency) in {"done", "completed"}
                    or dependency in scheduled and scheduled[dependency][1][1] <= interval[0]),
                    f"Unmet dependency: {key} requires {dependency}")
        delay = max(0, budgets[key]["conservative"] - (interval[1] - max(interval[0], now)).total_seconds() / 60)
        timing[key]["conservative_overrun_minutes"] = delay
        timing[key]["at_risk_if_conservative"] = [
            other for other in ranked if other != key
            and interval[1] <= scheduled[other][1][0] < interval[1] + timedelta(minutes=delay)]
    return {"information": information, "replan_impact": context["replan_impact"],
            "methods": {name: catalog[name]["revision"] for name in methods},
            "conservative_demand_minutes": conservative,
            "conservative_shortfall_minutes": max(0, conservative - capacity),
            "capacity_minutes": capacity, "preferred_capacity_minutes": preferred_capacity,
            "demand_minutes": demand, "shortfall_minutes": max(0, demand - capacity),
            "preferred_shortfall_minutes": max(0, demand - preferred_capacity),
            "soft_conflicts": soft_conflicts, "timing": timing,
            "unallocated_due": sorted(key for key in required if key not in scheduled
                                      and dispositions[key]["status"] not in {"deferred"}),
            "scheduled_elsewhere": sorted(elsewhere & pool.keys()),
            "legacy_unsynchronized_slots": context["legacy_slots"],
            "confirmed_closed": context["closed"], "status_conflicts": context["conflicts"]}


def compare_plans(data, tasks, candidates, now, knowledge=None):
    """Assess trade-offs within one snapshot. The caller chooses the plan."""
    first = next(iter(candidates.values()))
    snapshot = (first["day"], first["execution"]["revision"], first["execution"]["inputs"])
    result = {}
    for name, record in candidates.items():
        require((record["day"], record["execution"]["revision"], record["execution"]["inputs"]) == snapshot,
                "Candidates must share a planning snapshot")
        try:
            assessment = validate_plan(data, tasks, record, now, knowledge)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            result[name] = {"status": "infeasible", "reason": str(exc)}
            continue
        previous = effective(index(data)[0].get(record["day"], []), now) or {}
        prior = {identity(item): item for item in previous.get("items", []) if instant(item["end"]) > now}
        proposed = {identity(item): item for item in record["items"]}
        result[name] = {"status": "feasible", "assessment": assessment,
                        "changed_existing_blocks": sorted(key for key, item in prior.items()
                                                          if proposed.get(key) != item)}
    return result


def monitor(data, tasks, day, timezone_name, now, *, lead_minutes=10, knowledge=None):
    """Calculate expected work from Plan and observed work from Actual independently."""
    settings = {"timezone": timezone_name, "availability": [], "sources": {
        name: {"status": "ok", "evidence": "monitor uses journal only", "observed_at": now.isoformat()}
        for name in SOURCES}}
    context = build_context(data, tasks, day, settings, now, knowledge)
    demand = set(context["due"])
    require(0 <= lead_minutes <= 60, "Reminder lead must be between 0 and 60 minutes")
    legacy = {key: {**slot, "title": context["pool"][key]["title"]}
              for key, slot in context["legacy_slots"].items()}
    active = {**legacy, **context["active"]}
    known_blocks = {**context["prior_items"], **active}
    relevant = {key: item for key, item in known_blocks.items() if key in context["pool"]
                and (key in demand or instant(item["start"]) <= now < instant(item["end"]))}
    expected = sorted(key for key, item in relevant.items() if key in active
                      and instant(item["start"]) <= now < instant(item["end"]))
    overdue = sorted(key for key, item in relevant.items() if instant(item["end"]) <= now)
    ongoing = context["ongoing"]
    current = effective(index(data)[0].get(day, []), now)
    deferrals = (current or {}).get("execution", {}).get("dispositions", {})
    deferred = {key for key, item in deferrals.items() if item["status"] == "deferred"
                and item["until_day"] > now.astimezone(ZoneInfo(timezone_name)).date().isoformat()}
    overdue = [key for key in overdue if key not in deferred]
    demand -= deferred
    upcoming = sorted(((key, item) for key, item in relevant.items() if key in active and instant(item["start"]) > now),
                      key=lambda pair: instant(pair[1]["start"]))
    next_key, next_item = upcoming[0] if upcoming else (None, None)
    reminders = [{"kind": "overdue", "key": key} for key in overdue]
    reminders.extend({"kind": "hard_deadline_overdue", "key": key} for key in context["overdue_deadlines"])
    reminders.extend({"kind": "start", "key": key} for key in expected if key not in ongoing)
    if next_item and instant(next_item["start"]) - now <= timedelta(minutes=lead_minutes):
        reminders.append({"kind": "switch", "key": next_key})
    unresolved = sorted(key for key in demand if key not in expected and key not in ongoing
                        and key not in {entry[0] for entry in upcoming})
    deviation = bool(overdue or context["overdue_deadlines"] or unresolved or set(ongoing) - set(expected))
    budget = (current or {}).get("execution", {}).get("budgets", {})
    from .admin_shadow import planning_advice
    from .proactive import signals

    return {"checked_at": now.isoformat(), "proactive": signals(context, now),
            "administration": planning_advice(data, tasks, settings, now, context), "replan_impact": context["replan_impact"], "expected": expected, "ongoing": ongoing,
            "overdue": overdue, "overdue_deadlines": context["overdue_deadlines"],
            "unresolved_commitments": unresolved,
            "next": next_item, "reminders": reminders,
            "expected_remaining_minutes_at_plan": {key: budget[key]["normal"] for key in expected if key in budget},
            "affected_if_delay_continues": [key for key, _ in upcoming] if deviation else [],
            "deferred": sorted(deferred),
            "replan_required": deviation or bool(context["replan_impact"]["affected"]), "status_conflicts": context["conflicts"]}


def review_execution(stages, timeline):
    """Summarize evidence-backed counts and comparable execution estimates."""
    versions = {stage["version"]["id"]: stage["version"] for stage in stages}
    covered = [version for version in versions.values() if version.get("execution")]
    observed = {link["item_id"] for row in timeline for link in row.get("plan_links", [])
                if link["version_id"] in versions}
    planned = {item["id"] for version in versions.values() for item in version["items"]
               if item.get("status", "scheduled") == "scheduled"}
    errors = []
    for row in timeline:
        if row.get("effort") == "passive" or row.get("precision") != "exact" or row.get("status") != "completed":
            continue
        start, end = validate_interval(row)
        if not (start and end):
            continue
        for link in row["plan_links"]:
            version = versions.get(link["version_id"], {})
            item = next((item for item in version.get("items", []) if item["id"] == link["item_id"]), None)
            if item is None or instant(version["effective_at"]) > start:
                continue
            budget = version.get("execution", {}).get("budgets", {}).get(identity(item))
            if budget and (row.get("complete_task") or not item.get("task_ids")):
                actual = (end - start).total_seconds() / 60
                errors.append({"key": identity(item), "actual_minutes": actual,
                               "estimate_minutes": budget["normal"], "error_minutes": actual - budget["normal"]})
                break
    intake_versions = [version["execution"]["assessment"].get("information", {}) for version in covered]
    return {"information_covered_versions": sum(bool(items) for items in intake_versions),
            "information_dispositions": {key: sum(row["disposition"] == key for items in intake_versions
                                                  for row in items.values())
                                         for key in sorted({row["disposition"] for items in intake_versions
                                                            for row in items.values()})},
            "guarded_plan_versions": len(covered), "legacy_plan_versions": len(versions) - len(covered),
            "planned_item_count": len(planned), "observed_planned_item_count": len(planned & observed),
            "execution_evidence_ratio": len(planned & observed) / len(planned) if planned else None,
            "authorized_deferrals": sorted({entry["authorization"] for version in covered
                                           for entry in version["execution"]["dispositions"].values()
                                           if entry["status"] == "deferred"}),
            "remaining_conflict_counts": [len(version["execution"]["assessment"]["status_conflicts"])
                                          for version in covered],
            "estimation_errors": errors}
