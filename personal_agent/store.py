"""Provider-independent Personal Agent journal and projections."""
import copy
import fcntl
import hashlib
import json
import os
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = timezone.utc
EMPTY = {"schema_version": 1, "operations": []}
FEEDBACK_TYPES = {
    "one_off", "temporary", "historical_decision", "pattern", "potential_preference",
    "confirmed_preference", "method_issue", "agent_issue",
}


class Conflict(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ValueError(message)


def instant(value):
    if value is None:
        return None
    result = parse_timestamp(value)
    require(result.utcoffset() is not None, "Timestamp requires an explicit UTC offset")
    return result.astimezone(UTC)


def parse_timestamp(value):
    return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def read(path):
    data = json.loads(path.read_text()) if path.exists() else copy.deepcopy(EMPTY)
    require(data["schema_version"] == 1, "Unsupported journal schema")
    return data


def atomic_write(path, value):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as out:
        temp = Path(out.name)
        try:
            json.dump(value, out, indent=2, ensure_ascii=False, allow_nan=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def index(data):
    plans, actual, reviews, feedback, corrections, completions = {}, {}, {}, {}, [], {}
    for entry in data["operations"]:
        kind, record = entry["kind"], entry["record"]
        if kind == "plan":
            plans.setdefault(record["day"], []).append(record)
        elif kind in {"actual", "delete_actual"}:
            actual[record["id"]] = record
        elif kind == "correction":
            corrections.append(record)
        elif kind == "review":
            reviews[record["id"]] = record
            feedback.update((item["id"], item) for item in record.get("feedback", []))
    for entry in data["operations"]:
        record = entry["record"]
        if entry["kind"] == "actual" and actual[record["id"]] is record:
            task = record.get("complete_task")
            if task and record.get("status") == "completed":
                completions[task] = entry["id"]
    return plans, actual, reviews, feedback, corrections, completions


def effective_sequence(versions):
    """Exclude future versions superseded before they could take effect."""
    superseded = {ident for version in versions for ident in version.get("supersedes", [])}
    return [version for version in versions if version["id"] not in superseded]


def effective(versions, at):
    return next((v for v in reversed(effective_sequence(versions)) if instant(v["effective_at"]) <= at), None)


def changes(previous, current):
    before = {item["id"]: item for item in previous}
    after = {item["id"]: item for item in current}
    return [{"id": key, "before": before.get(key), "after": after.get(key)}
            for key in sorted(before.keys() | after.keys()) if before.get(key) != after.get(key)]


def validate_interval(record, zone=None):
    start, end = (instant(record.get(key)) for key in ("start", "end"))
    require(start is None or end is None or end > start, "End must be later than start")
    if zone:
        for key in ("start", "end"):
            if record.get(key):
                value = parse_timestamp(record[key])
                require(value.astimezone(zone).utcoffset() == value.utcoffset(),
                        f"{key} offset does not match the activity timezone")
    return start, end


def actual_links(plans, record):
    start, end = validate_interval(record)
    if start is None and end is None:
        return []
    point = start or end - timedelta(microseconds=1)
    limit = end if start and end else point + timedelta(microseconds=1)
    item_ids = set(record.get("plan_item_ids", []))
    task_ids = set(record.get("task_ids", []))
    links = []
    zone = ZoneInfo(record["timezone"])
    first_day = point.astimezone(zone).date().isoformat()
    last_day = (limit - timedelta(microseconds=1)).astimezone(zone).date().isoformat()
    for day, history in plans.items():
        versions = effective_sequence(history)
        for i, version in enumerate(versions):
            left = max(point, instant(version["effective_at"]))
            right = min(limit, instant(versions[i + 1]["effective_at"])) if i + 1 < len(versions) else limit
            if left >= right:
                continue
            for item in version["items"]:
                if item.get("status", "scheduled") != "scheduled":
                    continue
                overlaps = instant(item["start"]) < right and instant(item["end"]) > left
                explicit = item["id"] in item_ids and (overlaps or first_day <= day <= last_day)
                if explicit or (overlaps and task_ids.intersection(item.get("task_ids", []))):
                    links.append({"version_id": version["id"], "item_id": item["id"]})
    return links


def apply(data, operation, tasks, now, *, knowledge=None):
    operation_id, kind = operation["id"], operation["kind"]
    require(isinstance(operation_id, str) and bool(operation_id.strip()), "Stable operation ID required")
    # A source-backed batch has per-unit compare-and-swap tokens. A retry may
    # carry a newer Journal digest for unrelated changes.
    source_intake = kind == "intake_commitments" and "source" in operation["record"]
    fingerprint = digest({key: value for key, value in operation.items() if key != "base"}
                         if source_intake else operation)
    for old in data["operations"]:
        if old["id"] == operation_id:
            if old["fingerprint"] != fingerprint:
                raise Conflict("Operation ID already used with different input")
            return old, False
    if operation["base"] != digest(data):
        raise Conflict("Journal changed. Read latest snapshot and rebase operation")
    require(not data["operations"] or now >= instant(data["operations"][-1]["recorded_at"]),
            "Writer clock is behind the latest journal entry")
    plans, actual, _, feedback, _, _ = index(data)
    record = copy.deepcopy(operation["record"])
    task_ids = {task["id"] for task in tasks["tasks"]}
    now_text = now.isoformat()
    if kind == "plan":
        from .execution import expand_patch, validate_plan
        from .intents import current_intents

        patch = record.get("patch")
        dispositions = record.get("execution", {}).get("dispositions", {})
        includes_intent = any("item:" + ident in dispositions and item["kind"] == "action"
                              for ident, item in current_intents(data).items())
        # Existing full-Plan clients already obtain authorization through their established protocol.
        if patch or includes_intent or "publication" in record:
            publication = record.get("publication")
            require(isinstance(publication, str) and publication.strip(),
                    "Formal Plan requires explicit publication authorization evidence")
        record = expand_patch(data, record)
        assessment = validate_plan(data, tasks, record, now, knowledge)
        if patch:
            record["patch_base_version"] = patch["base_version"]
        record["execution"]["assessment"] = assessment
        day = date.fromisoformat(record["day"])
        require(day.isoformat() == record["day"], "Use YYYY-MM-DD")
        versions = plans.get(record["day"], [])
        previous = versions[-1] if versions else None
        valid_from = instant(record.get("effective_at")) or now
        require(valid_from >= now, "Use a correction for historical facts")
        supersedes = [version["id"] for version in effective_sequence(versions)
                      if instant(version["effective_at"]) >= valid_from]
        require(not any(instant(version["effective_at"]) <= now and version["id"] in supersedes
                        for version in versions), "Cannot replace a version that already took effect")
        ids = set()
        record["items"].sort(key=lambda item: item["id"])
        for item in record["items"]:
            require(isinstance(item["id"], str) and bool(item["id"].strip()) and item["id"] not in ids,
                    "Unique stable item IDs required")
            require(isinstance(item.get("notes", ""), str), "Plan notes must be text")
            require(isinstance(item.get("task_ids", []), list), "Plan task IDs must be a list")
            ids.add(item["id"])
            require(all(old.get("task_ids", []) == item.get("task_ids", [])
                        for history in plans.values() for version in history for old in version["items"]
                        if old["id"] == item["id"]), "Plan item ID already belongs to another identity")
            require(isinstance(item.get("title"), str) and bool(item["title"].strip()), "Plan title required")
            require(set(item.get("task_ids", [])) <= task_ids, "Unknown task ID")
            require(item.get("status", "scheduled") in {"scheduled", "deferred", "cancelled", "blocked", "waiting"},
                    "Invalid plan status")
            require(all(validate_interval(item)), "Both planned times required")
        by_id = {item["id"]: item for item in record["items"]}
        for item in by_id.values():
            if item.get("parallel_to"):
                parent = by_id.get(item["parallel_to"])
                require(parent and parent is not item and not parent.get("parallel_to"),
                        "Parallel activity requires a primary plan item")
        diff = changes(previous["items"] if previous else [], record["items"])
        if previous and not diff and not supersedes:
            fields = ("dispositions", "budgets")
            previous_execution = previous.get("execution", {})
            if (assessment.get("information", {}) == previous_execution.get("assessment", {}).get("information", {})
                    and assessment.get("methods", {}) == previous_execution.get("assessment", {}).get("methods", {})
                    and not record["execution"]["inputs"].get("intake") and all(
                    record["execution"][field] == previous_execution.get(field) for field in fields)
                    and {key: value for key, value in record["execution"]["inputs"].items() if key != "sources"}
                    == {key: value for key, value in previous_execution.get("inputs", {}).items()
                        if key != "sources"}):
                return None, False
        decision = record["decision"]
        require(all(decision.get(key) for key in ("trigger", "reason")), "Decision trigger and reason required")
        require(isinstance(decision["information"], list) and isinstance(decision["constraints"], list),
                "Capture information and constraints as lists")
        affected = sorted({task for delta in diff for item in (delta["before"], delta["after"])
                           for task in (item or {}).get("task_ids", [])})
        record.update(id=operation_id, created_at=now_text, effective_at=valid_from.isoformat(),
                      previous_id=previous["id"] if previous else None, changes=diff,
                      affected_task_ids=affected, supersedes=supersedes)
    elif kind in {"actual", "delete_actual"}:
        ident = record["id"]
        require(isinstance(ident, str) and bool(ident.strip()), "Stable activity ID required")
        previous = actual.get(ident)
        if kind == "delete_actual":
            require(previous and not previous.get("deleted"), "Activity is missing or deleted")
            require(record.get("reason"), "Deletion reason required")
            record = {"id": ident, "deleted": True, "reason": record["reason"],
                      "source_event_id": previous.get("source_event_id")}
        else:
            require(not previous or not previous.get("deleted"), "Activity ID is tombstoned")
            require("source" in record, "Source required for each report or correction")
            require(not previous or record.get("reason"), "Activity correction reason required")
            supplied_completion = record.get("complete_task")
            require(not ident.startswith("day-summary:"), "Reserved activity ID")
            record = {**(previous or {}), **record}
            start, end = validate_interval(record, ZoneInfo(record["timezone"]))
            require((start is None or start <= now) and (end is None or end <= now),
                    "Actual timestamps cannot be in the future")
            source_id = record.get("source_event_id")
            if source_id and any(other != ident and entry.get("source_event_id") == source_id
                                 for other, entry in actual.items()):
                raise Conflict("Source event already belongs to another activity")
            precision = record["precision"]
            require(precision in {"exact", "estimated", "unknown"}, "Invalid time precision")
            require(precision == "unknown" or start or end, "Known precision requires a time boundary")
            require(precision != "unknown" or not (start or end), "An available boundary needs its precision")
            require(precision != "estimated" or record.get("estimate_basis"), "Estimate needs a basis")
            require(record.get("name") and record.get("category") and record.get("source"), "Activity metadata required")
            require(record["status"] in {"ongoing", "occurred", "completed", "cancelled"}, "Invalid activity status")
            require(record.get("effort", "active") in {"active", "passive"}, "Invalid activity effort")
            duration = record.get("duration_minutes")
            require(duration is None or (isinstance(duration, (int, float)) and not isinstance(duration, bool)
                                         and duration > 0), "Reported duration must be positive")
            require(set(record.get("task_ids", [])) <= task_ids, "Unknown task ID")
            if "complete_task" not in operation["record"]:
                previous_completion = record.get("complete_task")
                if record["status"] == "completed" and previous_completion in record.get("task_ids", []):
                    supplied_completion = previous_completion
            require(not supplied_completion or (record["status"] == "completed"
                    and supplied_completion in record.get("task_ids", [])), "Invalid completion evidence")
            require(ident not in record.get("parallel_ids", []), "Activity cannot be parallel to itself")
            record["reported_day"] = (date.fromisoformat(record["reported_day"]).isoformat()
                                      if record.get("reported_day") else now.astimezone(ZoneInfo(record["timezone"])).date().isoformat())
            record["plan_links"] = actual_links(plans, record)
            record["first_recorded_at"] = previous["first_recorded_at"] if previous else now_text
            if not supplied_completion:
                record.pop("complete_task", None)
        record["recorded_at"] = now_text
    elif kind == "intake_commitments" and "source" in record:
        from .intake import validate

        validate(data, tasks, record, now)
    elif kind == "intake_commitments":
        from .execution import SOURCES, build_context

        require(isinstance(record["intake"], list) and record["intake"], "Intake must have commitments")
        build_context(data, tasks, now.date().isoformat(), {
            "timezone": "UTC", "availability": [], "intake": record["intake"],
            "sources": {name: {"status": "ok", "evidence": "intake only", "observed_at": now_text}
                        for name in SOURCES},
        }, now)
    elif kind in {"intent", "authorize_execution", "resolve_commitment"}:
        from .execution import evidence
        from .intents import current_intents, validate_intent

        intents = current_intents(data)
        from .intake import independent_commitments

        known = {f"task:{task}" for task in task_ids} | independent_commitments(data).keys()
        known.update(f"item:{item['id']}" for history in plans.values() for version in history
                     for item in version["items"] if not item.get("task_ids"))
        known.update(addition["id"] for entry in data["operations"] if entry["kind"] in {"plan", "intake_commitments"}
                     for addition in (entry["record"].get("intake", []) if entry["kind"] == "intake_commitments"
                                      else entry["record"].get("execution", {}).get("inputs", {}).get("intake", [])))
        known.update("item:" + ident for ident, row in intents.items() if row["kind"] == "action")
        if kind == "intent":
            previous = intents.get(record["id"])
            validate_intent(record, previous, known)
            if previous and all(previous.get(field) == record.get(field)
                                for field in previous.keys() - {"revision", "recorded_at"} | record.keys()):
                return None, False
        else:
            evidence(record)
            key = record["key"]
            require(key in known, f"Unknown execution identity: {key}")
        if kind == "authorize_execution":
            require(date.fromisoformat(record["until_day"]) > date.fromisoformat(record["day"]),
                    "Deferral must have a later date")
        elif kind == "resolve_commitment":
            require(key.startswith("item:") and record["status"] in {"completed", "cancelled"},
                    "Only independent commitments can be resolved through this operation")
    elif kind == "correction":
        require(any(v["id"] == record["version_id"] for versions in plans.values() for v in versions),
                "Unknown historical plan version")
        require(record.get("reason") and record.get("source") and record.get("annotation"),
                "Provide reason, source and correction annotation")
        record.update(id=operation_id, recorded_at=now_text)
    elif kind == "review":
        require(date.fromisoformat(record["from"]) <= date.fromisoformat(record["through"]), "Invalid review range")
        require(record.get("findings"), "Review findings required")
        require(len({item["id"] for item in record.get("feedback", [])}) == len(record.get("feedback", [])),
                "Duplicate feedback ID")
        for item in record.get("feedback", []):
            require(item["type"] in FEEDBACK_TYPES and item["status"] in {"candidate", "active", "disabled"},
                    "Invalid feedback type/status")
            require(all(item.get(k) for k in ("id", "evidence", "scope", "confidence", "recheck", "rule")),
                    "Feedback evidence, scope, confidence, recheck and rule required")
            require(item["type"] not in {"one_off", "historical_decision", "potential_preference"}
                    or item["status"] != "active", "This feedback type cannot be applied automatically")
            require(not (item["type"] == "confirmed_preference" and item["status"] == "active")
                    or item.get("confirmed_by_user") is True, "Confirmed preference requires explicit confirmation")
            if item.get("expires_at"):
                instant(item["expires_at"])
            item["previous_review_id"] = feedback.get(item["id"], {}).get("review_id")
            item["review_id"] = operation_id
        record.update(id=operation_id, recorded_at=now_text, evidence_revision=operation["base"])
    else:
        raise ValueError(f"Unsupported operation: {kind}")
    entry = {"id": operation_id, "kind": kind, "recorded_at": now_text, "record": record,
             "fingerprint": fingerprint}
    data["operations"].append(entry)
    return entry, True


def pending_tasks(data, tasks):
    """Complete only explicitly reported tasks; record receipt to preserve later task edits."""
    completions = index(data)[-1]
    completion_ids = set(completions.values())
    completion_records = {entry["id"]: entry["record"] for entry in data["operations"]
                          if entry["id"] in completion_ids}
    changed = False
    for task in tasks["tasks"]:
        key = completions.get(task["id"])
        receipt = task.get("activity_completion_receipt")
        latest = instant(task.get("capture_task_observed_at"))
        completion = completion_records.get(key)
        reported = instant(completion.get("capture_observed_at")) if completion else None
        if latest and reported and latest > reported:
            continue
        if key and receipt != key and (receipt or task["status"] == "todo") and task["status"] != "cancelled":
            prior = task.get("activity_previous_status", task["status"]) if receipt else task["status"]
            task.update(status="completed", activity_completion_receipt=key,
                        activity_previous_status=prior, completion_source="actual")
            if completion.get("end"):
                task["completed_at"] = completion["end"]
            else:
                task.pop("completed_at", None)
            changed = True
        elif receipt and not key:
            prior = task.pop("activity_previous_status", "todo")
            if task.get("completion_source") in {None, "actual"} and task["status"] == "completed":
                task["status"] = prior
                task.pop("completed_at", None)
                task.pop("completion_source", None)
            del task["activity_completion_receipt"]
            changed = True
    return changed


def apply_task_schedule(data, tasks, version, now):
    """Mirror an immediate plan version into the existing task schedule fields."""
    from .lifecycle import policy, set_execution_day

    enabled, zone = policy(tasks)
    day = version["day"]
    linked = {}
    for item in version["items"]:
        ids = item.get("task_ids", [])
        require(len(ids) <= 1, "Each scheduled block can reference one task")
        if ids:
            key = ids[0]
            require(key not in linked, "A task can have one block per plan")
            linked[key] = item
    history = index(data)[0][day]
    previous = next((entry for entry in history if entry["id"] == version["previous_id"]), None)
    previous_ids = {key for item in (previous or {}).get("items", []) for key in item.get("task_ids", [])}
    if linked or previous_ids:
        require(instant(version["effective_at"]) == now, "Task-linked plans take effect immediately")
    changed = False
    for task in tasks["tasks"]:
        key = task["id"]
        item = linked.get(key)
        if key not in previous_ids and item is None:
            continue
        before = dict(task)
        active = (item and not item.get("parallel_to") and item.get("status", "scheduled") == "scheduled"
                  and task["status"] not in {"done", "completed", "cancelled"}
                  and instant(item["end"]) > now)
        if active:
            children = [f"并行活动：{other['title']}" for other in version["items"]
                        if other.get("parallel_to") == item["id"]
                        and other.get("status", "scheduled") == "scheduled"]
            task.update(start_time=item["start"], end_time=item["end"])
            if enabled:
                set_execution_day(task, instant(item["start"]).astimezone(zone).date().isoformat())
                task.pop("automatic_reminder_move", None)
            options = {"calendar_title": item["title"] if item["title"] != task.get("title") else "",
                       "calendar_notes": "\n".join(filter(None, [item.get("notes"), *children]))}
            for field, value in options.items():
                if value:
                    task[field] = value
                else:
                    task.pop(field, None)
        elif task.get("start_time") and task["start_time"][:10] == day:
            for field in ("start_time", "end_time", "calendar_title", "calendar_notes"):
                task.pop(field, None)
        changed |= task != before
    return changed


def read_snapshot(path, tasks_path):
    """Read paired state under the same lock cooperative writers use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(".journal.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        require(not path.with_name(".execution-transaction").exists(),
                "Interrupted write pending; resume it with the original write operation")
        return read(path), json.loads(tasks_path.read_text())


def recover_transaction(path, tasks_path):
    """Resume an interrupted paired write without overwriting an unrelated revision."""
    pending = path.with_name(".execution-transaction")
    if not pending.exists():
        return
    transaction = json.loads(pending.read_text())
    current_journal = digest(read(path))
    current_tasks = digest(json.loads(tasks_path.read_text()))
    next_journal, next_tasks = digest(transaction["journal"]), digest(transaction["tasks"])
    require(current_journal in {transaction["before"], next_journal}
            and current_tasks in {transaction["tasks_before"], next_tasks},
            "Interrupted transaction conflicts with an external edit")
    if current_journal != next_journal:
        atomic_write(path, transaction["journal"])
    if current_tasks != next_tasks:
        atomic_write(tasks_path, transaction["tasks"])
    pending.unlink()


def mutate_tasks(root, update):
    """Serialize a task-only change with Journal writers."""
    path = root / "personal_data/journal.json"
    tasks_path = root / "tasks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(".journal.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        recover_transaction(path, tasks_path)
        before = tasks_path.read_bytes()
        tasks = json.loads(before)
        revision = digest(tasks)
        result = update(read(path), tasks)
        if tasks_path.read_bytes() != before:
            raise Conflict("Tasks changed during maintenance")
        result["changed"] = revision != digest(tasks)
        if result["changed"]:
            atomic_write(tasks_path, tasks)
        result["tasks_revision"] = digest(tasks)
        return result


def write(path, tasks_path, operation, now=None, *, root=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(".journal.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        recover_transaction(path, tasks_path)
        data = read(path)
        tasks = json.loads(tasks_path.read_text())
        tasks_before = digest(tasks)
        now = now or datetime.now(UTC)
        from .lifecycle import maintain_journal

        kind = operation["kind"]
        maintenance = None
        if kind in {"plan", "review"}:
            maintenance = maintain_journal(data, tasks, now)
        knowledge = None
        if operation["kind"] == "plan" and root is not None and not any(
                old["id"] == operation["id"] for old in data["operations"]):
            from .sources import live_inputs, read_knowledge

            require(path.is_file(), "Journal source is missing")
            execution = operation["record"].get("execution")
            require(isinstance(execution, dict), "Plan requires execution context and dispositions")
            inputs = execution["inputs"]
            require(inputs == live_inputs(operation["record"]["day"], inputs, now),
                    "Calendar evidence changed; rebuild snapshot")
            knowledge = read_knowledge(root)
        if operation["kind"] == "capture_turn":
            from .capture import on_turn

            ident = operation["id"]
            fingerprint = digest(operation["record"])
            entry = next((old for old in data["operations"] if old["id"] == ident), None)
            changed = entry is None
            if changed:
                if data["operations"]:
                    require(now >= instant(data["operations"][-1]["recorded_at"]),
                            "Writer clock is behind the latest journal entry")
                envelope = operation["record"]
                dispositions = on_turn(data, tasks, ident, {key: value for key, value in envelope.items()
                                                            if key != "intake"}, now)
                receipt = {"dispositions": dispositions}
                if "intake" in envelope:
                    intake_id = ident + "-intake"
                    apply(data, {"id": intake_id, "kind": "intake_commitments", "base": digest(data),
                                 "record": envelope["intake"]}, tasks, now)
                    receipt["intake_operation_id"] = intake_id
                entry = {"id": ident, "kind": "capture_turn", "recorded_at": now.isoformat(),
                         "fingerprint": fingerprint, "record": receipt}
                data["operations"].append(entry)
            else:
                require(entry["kind"] == "capture_turn" and entry["fingerprint"] == fingerprint,
                        "Capture turn ID already used with different input")
        else:
            entry, changed = apply(data, operation, tasks, now, knowledge=knowledge)
        if changed and entry["kind"] == "plan":
            apply_task_schedule(data, tasks, entry["record"], now)
        if changed and entry["kind"] in {"actual", "delete_actual", "capture_turn"}:
            pending_tasks(data, tasks)
        if kind in {"actual", "delete_actual"}:
            maintenance = maintain_journal(data, tasks, now)
        tasks_changed = digest(tasks) != tasks_before
        if changed or tasks_changed:
            if digest(json.loads(tasks_path.read_text())) != tasks_before:
                raise Conflict("Tasks changed during validation")
            if tasks_changed:
                atomic_write(path.with_name(".execution-transaction"), {
                    "before": digest(read(path)), "tasks_before": tasks_before,
                    "journal": data, "tasks": tasks,
                })
                recover_transaction(path, tasks_path)
            else:
                atomic_write(path, data)
        return {"entry": entry, "changed": changed, "revision": digest(data), "lifecycle": maintenance}


def union_seconds(intervals):
    total, last = 0, None
    for start, end in sorted(intervals):
        if last is None or end > last:
            total += (end - (max(start, last) if last else start)).total_seconds()
            last = end
    return total


def review_data(data, tasks, first, last, timezone_name, now):
    require(first <= last, "Invalid review range")
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(first, time.min, zone).astimezone(UTC)
    end = datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(UTC)
    plans, actual, reviews, feedback, corrections, _ = index(data)
    timeline, buckets, stages = [], {}, []
    exact, estimated = [], []
    unknown = []
    intervals = {}
    for record in actual.values():
        if record.get("deleted") or record["status"] == "cancelled":
            continue
        left, right = validate_interval(record)
        complete = left is not None and right is not None
        anchor = left or right
        included = (left < end and right > start if complete else
                    start <= anchor < end if anchor else first.isoformat() <= record["reported_day"] <= last.isoformat())
        if not included:
            continue
        timeline.append(record)
        if record.get("effort") == "passive":
            continue
        if not complete or record["precision"] == "unknown":
            unknown.append(record["id"])
            continue
        interval = max(left, start), min(right, end)
        intervals[record["id"]] = interval
        buckets.setdefault(record["category"], []).append(interval)
        (exact if record["precision"] == "exact" else estimated).append(interval)

    relevant_ids = set()
    initial, rolling = {}, {}
    for day, versions in sorted(plans.items()):
        if not first.isoformat() <= day <= last.isoformat():
            continue
        sequence = effective_sequence(versions)
        initial[day] = sequence[0]
        rolling[day] = effective(versions, now)
        bounds = {version["id"]: sequence[i + 1]["effective_at"] if i + 1 < len(sequence) else None
                  for i, version in enumerate(sequence)}
        for version in versions:
            relevant_ids.add(version["id"])
            valid_to = bounds.get(version["id"])
            matched = [r["id"] for r in timeline if any(
                link["version_id"] == version["id"] for link in r["plan_links"])]
            stages.append({"version": version, "effective_until": valid_to, "actual_ids": matched,
                           "superseded_before_effective": version["id"] not in bounds})
    timeline.sort(key=lambda item: (instant(item.get("start") or item.get("end")) or datetime.max.replace(tzinfo=UTC),
                                    item["id"]))
    covered = union_seconds(exact + estimated)
    elapsed = max(0, (min(now, end) - start).total_seconds())
    elapsed_intervals = [(a, min(b, now)) for a, b in exact + estimated if a < now]
    parallel = [{"activity_ids": [a, b], "overlap_seconds": (min(ar, br) - max(al, bl)).total_seconds()}
                for a, (al, ar) in intervals.items() for b, (bl, br) in intervals.items()
                if a < b and max(al, bl) < min(ar, br)]
    from .execution import review_execution

    return {"execution": review_execution(stages, timeline),
            "from": first.isoformat(), "through": last.isoformat(), "timezone": timezone_name,
            "initial": initial, "rolling": rolling, "stages": stages, "timeline": timeline,
            "corrections": [c for c in corrections if c["version_id"] in relevant_ids],
            "tasks": tasks["tasks"], "unknown_duration_ids": unknown, "parallel": parallel,
            "category_seconds": {key: union_seconds(value) for key, value in buckets.items()},
            "known_seconds": covered, "exact_seconds": union_seconds(exact),
            "estimated_only_seconds": covered - union_seconds(exact),
            "elapsed_window_seconds": elapsed, "uncovered_elapsed_seconds": elapsed - union_seconds(elapsed_intervals),
            "elapsed_coverage": union_seconds(elapsed_intervals) / elapsed if elapsed else None,
            "prior_reviews": list(reviews.values()),
            "active_feedback": [item for item in feedback.values() if item["status"] == "active"
                                and (not item.get("expires_at") or instant(item["expires_at"]) > now)]}
