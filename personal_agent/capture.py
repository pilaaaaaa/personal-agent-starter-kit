"""Translate host-interpreted turns into audited Actual versions."""
from datetime import date, datetime, time, timedelta
import re
import unicodedata
from zoneinfo import ZoneInfo

from .store import UTC, apply, digest, index, instant, require

ACTIVITY_STATES = {"ongoing", "occurred", "completed", "cancelled"}
TASK_STATES = {"task_cancelled": "cancelled", "task_reopened": "todo"}
FACTS = ACTIVITY_STATES | TASK_STATES.keys()
OBSERVATION_FIELDS = {"key", "state", "evidence", "name", "day", "task_id", "complete_task",
                      "occurrence", "actual_id", "activity_key", "parallel_keys", "progress",
                      "correction", "category", "effort", "start", "end", "duration_minutes", "estimate_basis"}
NON_FACTS = {"future", "uncertain", "unrelated"}
KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")


def activity_name(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def estimate(item, previous, observed, zone, day):
    start, end = (instant(item.get(field)) for field in ("start", "end"))
    duration = item.get("duration_minutes")
    require(duration is None or (type(duration) in {float, int} and 0 < duration <= 1440),
            "Duration must be between zero and 1440 minutes")
    if start and end:
        precision = "estimated" if item.get("estimate_basis") else "exact"
        return start, end, precision, item.get("estimate_basis")
    anchor = observed
    previously_started = instant(previous.get("start")) if previous else None
    earlier_day = day < observed.astimezone(zone).date()
    if earlier_day:
        anchor = datetime.combine(day, time(12), zone).astimezone(UTC)
    basis = ["当地报告日期的正午占位" if earlier_day else "消息时间"]
    if end is None:
        if start is not None and duration and item["state"] != "ongoing":
            end = start + timedelta(minutes=duration)
        elif start is not None:
            end = start + timedelta(minutes=1) if earlier_day else observed
        else:
            end = anchor
    if start is None:
        if duration:
            start = end - timedelta(minutes=duration)
        elif previously_started and previously_started < end and (not earlier_day or
                previously_started.astimezone(zone).date() == day):
            start = previously_started
            basis.append("沿用上一版本起点")
        else:
            start = end - timedelta(minutes=1)
            basis.append("缺少已发生时长，暂占一分钟")
    if start == end == observed and item["state"] == "ongoing":
        start -= timedelta(minutes=1)
        basis.append("报告时刚开始，暂占一分钟")
    require(start < end <= observed, "Actual interval must end by the message time and have positive length")
    if duration:
        basis.append("已报告的实际时长")
    if item.get("start") or item.get("end"):
        basis.append("已报告的时间边界")
    if item.get("estimate_basis"):
        basis.append(item["estimate_basis"])
    return start, end, "estimated", "；".join(basis)


def on_turn(data, tasks, turn_id, turn, now):
    """Run under the existing Journal lock. Any validation failure aborts the batch."""
    require(set(turn) - {"supersedes"} == {"conversation_id", "observed_at", "timezone", "observations"},
            "Turn requires conversation_id, observed_at, timezone and observations")
    conversation = turn["conversation_id"]
    require(isinstance(conversation, str) and conversation.strip(), "Conversation ID required")
    zone = ZoneInfo(turn["timezone"])
    observed = instant(turn["observed_at"])
    require(observed and observed <= now, "Message timestamp must be available and in the past")
    local_day = observed.astimezone(zone).date()
    observations = turn["observations"]
    require(isinstance(observations, list), "Observations must be a list")
    actual = index(data)[1]
    task_rows = {task["id"]: task for task in tasks["tasks"]}
    task_mutations = set()
    ids, prepared, outcomes = {}, [], {}
    for item in observations:
        require(isinstance(item, dict) and set(item) <= OBSERVATION_FIELDS, "Unknown observation fields")
        key, state = item["key"], item["state"]
        require(KEY.fullmatch(key) and key not in outcomes, "Observation keys must be unique and filename-safe")
        require(isinstance(item["evidence"], str) and item["evidence"].strip(), "User evidence required")
        require(state in FACTS | NON_FACTS, "Unsupported observation state")
        if state in NON_FACTS:
            outcomes[key] = {"status": "excluded", "reason": state}
            continue
        day = date.fromisoformat(item.get("day", local_day.isoformat()))
        require(day <= local_day, "Actual day cannot be in the future")
        task = item.get("task_id")
        require(task is None or task in task_rows, "Unknown task ID")
        if state in TASK_STATES or item.get("complete_task"):
            require(task and task not in task_mutations, "One explicit status change per task per turn")
            task_mutations.add(task)
        if state in TASK_STATES:
            target = task_rows[task]
            before, after = target["status"], TASK_STATES[state]
            latest = instant(target.get("capture_task_observed_at"))
            result = {"task_id": task, "before": before, "after": before, "status": "task_unchanged"}
            if not latest or observed >= latest:
                target.update(status=after, capture_task_observed_at=observed.isoformat())
                result.update(after=after, status="task_updated" if before != after else "task_unchanged")
            outcomes[key] = result
            continue
        require(activity_name(item["name"]), "Activity name required")
        require(not item.get("complete_task") or (task and state == "completed"),
                "Task completion requires explicit evidence and a task ID")
        occurrence = item.get("occurrence")
        if item.get("actual_id"):
            ident = item["actual_id"]
            require(ident in actual and not actual[ident].get("deleted"), "Unknown or deleted Actual ID")
        else:
            subject = item.get("activity_key") or ("task:" + task if task else activity_name(item["name"]))
            ident = "capture-" + digest([subject, occurrence or day.isoformat()])[:32]
            if not occurrence and ident not in actual and "day" not in item:
                ongoing = [other for other, row in actual.items()
                           if row.get("status") == "ongoing" and row.get("capture_conversation_id") == conversation
                           and (task in row.get("task_ids", []) if task else
                                activity_name(row["name"]) == activity_name(item["name"]))]
                require(len(ongoing) <= 1, "Ambiguous ongoing activity; supply its Actual ID")
                if ongoing:
                    ident = ongoing[0]
            # Existing/manual records can be selected safely by their stable ID.
            # A unique exact interval also deduplicates reports across entry points.
            if not occurrence and item.get("start") and item.get("end") and ident not in actual:
                matches = [other for other, row in actual.items() if not row.get("deleted")
                           and (task in row.get("task_ids", []) if task else
                                activity_name(row["name"]) == activity_name(item["name"]))
                           and instant(row.get("start")) == instant(item["start"])
                           and instant(row.get("end")) == instant(item["end"])]
                require(len(matches) <= 1, "Ambiguous legacy activity; supply its Actual ID")
                if matches:
                    ident = matches[0]
        require(ident not in ids.values(), "Multiple reports of one activity in the same turn")
        ids[key] = ident
        prepared.append((key, item, ident, task, day))
        outcomes[key] = None

    for key, item, ident, task, day in prepared:
        previous = actual.get(ident, {})
        state = item["state"]
        references = item.get("parallel_keys", [])
        require(isinstance(references, list) and all(ref in ids and ref != key for ref in references),
                "Parallel references must identify other factual observations in this turn")
        parallel = sorted(set(previous.get("parallel_ids", [])) | {ids[ref] for ref in references})
        progress = item.get("progress", previous.get("progress"))
        require(progress is None or isinstance(progress, str), "Progress must be text")
        require(type(item.get("complete_task", False)) is bool, "Task completion must be boolean")
        effort = item.get("effort", previous.get("effort", "active"))
        require(effort in {"active", "passive"}, "Invalid activity effort")
        correction = item.get("correction", False)
        require(type(correction) is bool, "Correction must be boolean")
        prior_stamp = instant(previous.get("capture_observed_at"))
        old_status = previous.get("status")
        stale = prior_stamp and observed < prior_stamp
        regresses = old_status in {"completed", "occurred", "cancelled"} and state == "ongoing"
        weaker = old_status == "completed" and state == "occurred"
        task_links = sorted(set(previous.get("task_ids", [])) | ({task} if task else set()))
        completion = task if item.get("complete_task") else previous.get("complete_task")
        if state != "completed":
            completion = None
        changed_day = bool(item.get("day") and item["day"] != previous.get("reported_day"))
        changed_duration = item.get("duration_minutes") is not None and (
            item["duration_minutes"] != previous.get("duration_minutes"))
        timing_change = changed_day or changed_duration or any(
            item.get(field) and instant(item[field]) != instant(previous.get(field))
            for field in ("start", "end"))
        task_already_completed = not item.get("complete_task") or task_rows[task]["status"] in {"completed", "done"}
        same = (task_already_completed and state == old_status and progress == previous.get("progress")
                and effort == previous.get("effort", "active")
                and parallel == sorted(previous.get("parallel_ids", []))
                and task_links == sorted(previous.get("task_ids", []))
                and completion == previous.get("complete_task"))
        if (not correction and (stale or regresses or weaker)) or (
                not correction and same and not timing_change and state != "ongoing"):
            outcomes[key] = {"status": "duplicate", "actual_id": ident}
            continue
        if previous and not timing_change and same and state == "ongoing" and observed == prior_stamp:
            outcomes[key] = {"status": "duplicate", "actual_id": ident}
            continue
        if previous and previous.get("start") and previous.get("end") and (
                state == "cancelled" or (not timing_change and state == old_status and state != "ongoing")):
            start, end = (instant(previous[field]) for field in ("start", "end"))
            precision, basis = previous["precision"], previous.get("estimate_basis")
        elif state == "cancelled":
            # A cancellation is a state observation. It supplies no elapsed activity.
            start = end = basis = None
            precision = "unknown"
        else:
            if effort == "passive" and not item.get("start") and not item.get("duration_minutes"):
                # A reported outcome supplies a time marker, not elapsed active work.
                start, end, precision, basis = estimate(item, {}, observed, zone, day)
            else:
                start, end, precision, basis = estimate(item, previous, observed, zone, day)
            require(start < end <= observed, "Actual interval cannot include future time")
        operation_id = "capture-event-" + digest([turn_id, key])[:32]
        record = {
            "id": ident, "name": item["name"], "category": item.get("category", previous.get("category", "activity")),
            "status": state, "effort": effort, "start": start.astimezone(zone).isoformat() if start else None,
            "end": end.astimezone(zone).isoformat() if end else None, "timezone": zone.key,
            "precision": precision, "estimate_basis": basis, "reported_day": previous.get("reported_day", day.isoformat())
            if not correction else day.isoformat(),
            "source": item["evidence"], "capture_observed_at": observed.isoformat(),
            "capture_conversation_id": conversation, "capture_turn_id": turn_id,
            "reason": "Conversation update", "progress": progress, "task_ids": task_links,
            "complete_task": completion, "parallel_ids": parallel,
        }
        if item.get("duration_minutes") is not None:
            record["duration_minutes"] = item["duration_minutes"]
        if completion and (item.get("complete_task") or not stale):
            task_row = task_rows[completion]
            latest = instant(task_row.get("capture_task_observed_at"))
            if latest and observed < latest:
                record["complete_task"] = None
            elif item.get("complete_task"):
                task_row["capture_task_observed_at"] = observed.isoformat()
        apply(data, {"id": operation_id, "kind": "actual", "record": record, "base": digest(data)}, tasks, now)
        outcomes[key] = {"status": "written", "actual_id": ident, "operation_id": operation_id}
    require(len(outcomes) == len(observations) and all(outcomes.values()), "Incomplete capture dispositions")
    return outcomes
