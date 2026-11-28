"""Read-only maintenance allocation over the existing execution inventory."""
import os
from datetime import timedelta
from statistics import median
from zoneinfo import ZoneInfo

from .admin_inference import infer
from .execution import bounds, build_context
from .intake import current
from .lifecycle import deadline_cutoff
from .store import UTC, digest, instant, require

METHODS = {"aging-scheduling", "cost-of-delay", "critical-path-dependency-analysis",
           "personal-kanban", "attention-residue", "maintenance-budget"}


def checkpoint_history(data, now):
    fields, first, arrivals = {}, {}, {}
    events = []
    for order, entry in enumerate(data["operations"]):
        if entry["kind"] == "intake_commitments" and entry["record"].get("units"):
            record = entry["record"]
            observed = instant(record["source"]["observed_at"])
            if observed <= now:
                events.append((observed, order, record["units"]))
    for observed, _, units in sorted(events):
        for unit in units:
            key = unit.get("target")
            if not key or not key.startswith(("task:", "item:")):
                continue
            first.setdefault(key, observed)
            patch = unit.get("checkpoint", {})
            if patch.get("lane") == "maintenance":
                arrivals.setdefault(key, instant(patch.get("first_seen_at")) or first[key])
            fields.setdefault(key, {}).update(patch)
    return fields, first, arrivals


def free_windows(settings, context, day, now):
    _, end = bounds(day, now.tzinfo)
    end = end.astimezone(UTC)
    now = now.astimezone(UTC)
    occupied = [rule for rule in context["constraints"] if rule["kind"].endswith("hard")]
    occupied += list(context["active"].values()) + list(context["legacy_slots"].values())
    occupied += [item for plan in context["latest"].values() for item in plan.values()]
    blocked = sorted((instant(row["start"]), instant(row["end"])) for row in occupied)
    for window in settings["availability"]:
        start = max(now, instant(window["start"]))
        stop = min(end, instant(window["end"]))
        for left, right in blocked:
            if start < min(left, stop):
                yield start, min(left, stop)
            start = max(start, right) if left < stop else stop
            if start >= stop:
                break
        if start < stop:
            yield start, stop


def minutes(windows):
    return sum((right - left).total_seconds() / 60 for left, right in windows)


def next_slot(windows, duration, earliest, cutoff):
    for index, (left, right) in enumerate(windows):
        start = max(left, earliest)
        end = start + duration
        if end <= right and (cutoff is None or end <= cutoff):
            return index, start, end
    return None


def growth_reservation(rows, capacity, condition, urgent_minutes, share):
    if share is not None:
        return capacity * share, "explicit_comparison"
    growth = [row["effort_minutes"] for row in rows.values()
              if row["lane"] == "growth" and row["state"] in {"ready", "active"}
              and not row["scheduled"] and not row["dependencies"] and row["effort_minutes"]]
    if not growth:
        # Keep a small buffer for unobserved or newly arriving growth work.
        return min(capacity * 0.2, 30), "uncommitted_buffer"
    viable_block = min(max(growth), capacity)
    baseline = min(viable_block, capacity * (0.75 if condition == "normal" else 0.55))
    if urgent_minutes > capacity - baseline:
        return max(min(viable_block, capacity * 0.35), capacity - urgent_minutes), "urgent_pressure"
    return baseline, "observed_growth_demand"


def analyze(data, tasks, day, settings, now, knowledge=None, options=None, *, previously_selected=(), context=None):
    options = {} if options is None else options
    require(set(options) <= {"disabled", "acknowledged", "condition", "growth_share", "resume", "infer_legacy"},
            "Unexpected shadow options")
    disabled = options.get("disabled", [])
    acknowledged = options.get("acknowledged", [])
    require(isinstance(disabled, list) and all(method in METHODS for method in disabled)
            and isinstance(acknowledged, list) and all(isinstance(item, str) for item in acknowledged),
            "Invalid shadow methods or acknowledgement IDs")
    methods = (knowledge or {}).get("methods", {})
    enabled = {method for method in METHODS if method not in disabled
               and methods.get(method, {}).get("enabled", True)}
    resume = options.get("resume")
    require(resume is None or isinstance(resume, str), "Resume requires a stable work identity")
    condition = options.get("condition", "normal")
    require(condition in {"normal", "low"}, "Invalid current condition")
    share = options.get("growth_share")
    require(share is None or type(share) in {int, float} and 0 <= share <= 1, "Invalid protected growth share")
    infer_legacy = options.get("infer_legacy", True)
    require(type(infer_legacy) is bool, "Legacy inference switch must be boolean")
    require(not data["operations"] or now >= instant(data["operations"][-1]["recorded_at"]),
            "Replay requires a snapshot recorded at or before the observation time")
    now = now.astimezone(ZoneInfo(settings["timezone"]))
    context = context if context is not None else build_context(data, tasks, day, settings, now, knowledge)
    fields, first_seen, arrivals = checkpoint_history(data, now)
    explicit_by_key = dict(fields)
    estimates = infer(data, tasks, context["actual"], now) if infer_legacy else {}
    for key, (values, _, _) in estimates.items():
        if key in context["pool"]:
            explicit = fields.get(key, {})
            fields[key] = {**values, **explicit}
    information = current(data)
    by_target = {}
    for row in information.values():
        key = row.get("target")
        if key in context["pool"] and instant(row["source"]["observed_at"]) <= now:
            by_target.setdefault(key, []).append(row)

    windows = sorted(free_windows(settings, context, day, now))
    _, horizon = bounds(day, now.tzinfo)
    capacity = minutes(windows)
    rows, follow_ups, future_reviews = {}, {}, []
    maintenance_completions = {}
    period = now - timedelta(days=14)
    actual_by_task = {}
    for actual in context["actual"].values():
        if actual.get("deleted") or actual.get("status") == "cancelled":
            continue
        for ident in actual.get("task_ids", []):
            actual_by_task.setdefault("task:" + ident, []).append(actual)
        key = "task:" + str(actual.get("complete_task"))
        end = instant(actual.get("end"))
        start = instant(actual.get("start"))
        explicit = explicit_by_key.get(key, {})
        inferred = estimates.get(key, ({}, {}, None))[0]
        lane = explicit.get("lane", inferred.get("lane"))
        if (lane == "maintenance" and actual.get("status") == "completed"
                and actual.get("precision") == "exact" and actual.get("effort") != "passive" and start and end and start < end
                and period <= end <= now):
            maintenance_completions[key] = (end - start).total_seconds() / 60

    # Propagate observed downstream cutoffs through explicit prerequisites.
    cutoffs, dependencies = {}, {}
    for key, entry in context["pool"].items():
        if entry.get("deadline"):
            cutoffs[key] = deadline_cutoff(entry["deadline"])
        dependencies[key] = {dep for row in by_target.get(key, []) if row["disposition"] not in
                             {"handled", "no_action"} for dep in row.get("depends_on", [])
                             if context["closed"].get(dep) not in {"done", "completed"}}
    inherited = dict(cutoffs)
    unresolved_graph = set()
    if "critical-path-dependency-analysis" in enabled:
        # Visit downstream work before prerequisites. Unresolved cycles and their
        # prerequisites retain any observed cutoffs without repeated subtraction.
        dependent_count = dict.fromkeys(dependencies, 0)
        for deps in dependencies.values():
            for dep in deps & dependencies.keys():
                dependent_count[dep] += 1
        pending = [key for key, count in dependent_count.items() if count == 0]
        while pending:
            key = pending.pop()
            checkpoint = fields.get(key, {})
            for dep in dependencies[key] & dependencies.keys():
                if key in inherited:
                    limit = inherited[key] - timedelta(days=checkpoint.get("lead_days") or 0,
                                                       minutes=checkpoint.get("effort_minutes") or 0)
                    inherited[dep] = min(inherited.get(dep, limit), limit)
                dependent_count[dep] -= 1
                if dependent_count[dep] == 0:
                    pending.append(dep)
        unresolved_graph = {key for key, count in dependent_count.items() if count}
    for key, entry in context["pool"].items():
        checkpoint = fields.get(key, {})
        evidence = by_target.get(key, [])
        waits = [row for row in evidence if row["disposition"] == "waiting"]
        probes = []
        for wait in waits:
            explicit = instant(wait.get("trigger_at") or wait.get("expires_at"))
            if explicit:
                probes.append(explicit)
                continue
            observed = instant(wait["source"]["observed_at"])
            fallback = observed + timedelta(days=checkpoint.get("lead_days") or 2)
            cutoff = inherited.get(key)
            if cutoff:
                fallback = max(observed, min(fallback, cutoff - timedelta(days=1)))
            probes.append(fallback)
        probe = min(probes, default=None)
        future_reviews.extend(at for at in probes if at > now)
        available_from = instant(checkpoint.get("available_from"))
        deferred_by_day = bool(entry["day"] and entry["day"] > day and key not in inherited)
        eligible_later = False
        status = "ready"
        # Use current evidence IDs for wait closure. Partial progress on another ID does not close a wait.
        if key in context["conflicts"] or any(row.get("blocking") and (not row.get("expires_at")
                                                           or instant(row["expires_at"]) > now) for row in evidence):
            status = "blocked"
        elif key in context["ongoing"]:
            status = "active"
        elif waits:
            status = "follow_up" if probe and probe <= now else "waiting"
        elif dependencies[key] or checkpoint.get("phase") == "blocked" or checkpoint.get("blocker"):
            status = "blocked"
        elif (deferred_by_day or key in context["deferred_tasks"]
              or entry.get("optional") or checkpoint.get("phase") == "deferred"):
            status = "deferred"
        elif available_from and available_from > now:
            status = "deferred"
            eligible_later = available_from < horizon
        elif checkpoint.get("phase") == "active":
            status = "active"
        task = entry.get("task", {})
        first = min(filter(None, [instant(checkpoint.get("first_seen_at")), first_seen.get(key)]), default=None)
        age = max(0, (now.date() - first.astimezone(now.tzinfo).date()).days) if first else None
        calibrated = context["calibration"].get(key, {}).get("median_minutes")
        _, provenance, inference_status = estimates.get(key, ({}, {}, "disabled"))
        explicit_fields = explicit_by_key.get(key, {})
        provenance = {field: source for field, source in provenance.items() if field not in explicit_fields}
        effort = (checkpoint.get("effort_minutes") if "effort_minutes" in explicit_fields
                  else calibrated or checkpoint.get("effort_minutes"))
        if calibrated and "effort_minutes" not in explicit_fields:
            provenance["effort_minutes"] = "exact_task_completion"
        change_at = instant(checkpoint.get("cost_change_at"))
        future = checkpoint.get("future_effort_minutes")
        if change_at and change_at <= now and future and "cost-of-delay" in enabled:
            effort = future
        cutoff = inherited.get(key, cutoffs.get(key)) if "cost-of-delay" in enabled else cutoffs.get(key)
        window_end = instant(checkpoint.get("window_end"))
        limits = list(filter(None, [cutoff, window_end]))
        action_end = min(limits) if limits else None
        lead = checkpoint.get("lead_days") or 0
        if action_end and "cost-of-delay" in enabled and cutoff:
            action_end = min(action_end, cutoff - timedelta(days=lead))
        latest_start = action_end - timedelta(minutes=effort) if action_end and effort else action_end
        aging_due = ("aging-scheduling" in enabled and age is not None
                     and age >= (checkpoint.get("aging_days") or 7))
        commitments = [plan[key] for plan in context["latest"].values() if key in plan]
        commitments.extend(part[key] for part in (context["active"], context["legacy_slots"]) if key in part)
        scheduled = any(instant(commitment["end"]) > now for commitment in commitments)
        timely_commitment = any(instant(commitment["end"]) > now and (
            action_end is None or instant(commitment["end"]) <= action_end) for commitment in commitments)
        actual = sorted(actual_by_task.get(key, []),
                        key=lambda row: instant(row.get("end") or row.get("start")) or now, reverse=True)
        row = rows[key] = {"state": status, "lane": checkpoint.get("lane"), "age_days": age,
                           "effort_minutes": effort, "deadline": task.get("deadline"),
                           "inference": {"fields": provenance, "status": inference_status},
                           "lane_source": "checkpoint" if "lane" in explicit_fields else provenance.get("lane"),
                           "eligible_later_today": eligible_later,
                           "latest_start": latest_start.isoformat() if latest_start else None,
                           "overdue": entry.get("overdue", False),
                           "slack_minutes": (latest_start - now).total_seconds() / 60 if latest_start else None,
                           "dependencies": sorted(dependencies[key]), "scheduled": scheduled,
                           "timely_commitment": timely_commitment,
                           "aging_due": aging_due, "external_start": bool(lead and checkpoint.get("unlocks_wait")),
                           "review_at": probe.isoformat() if probe else None, "checkpoint": {
                               **{field: value for field, value in checkpoint.items() if value is not None},
                               "task_notes": task.get("notes"),
                               "evidence": [{"summary": item["summary"], "source": item["source"]["id"]}
                                            for item in evidence],
                               "recent_actual": [{"id": item["id"], "title": item.get("name"),
                                                  "status": item["status"]} for item in actual[:5]]}}
        if status == "follow_up":
            pending_waits = []
            for wait, at in zip(waits, probes):
                signal = digest([key, wait["id"], wait["version"]])[:24]
                if at <= now and signal not in acknowledged:
                    pending_waits.append({"id": signal, "review_at": at.isoformat(),
                                          "estimated": not bool(wait.get("trigger_at") or wait.get("expires_at"))})
            if pending_waits:
                follow_ups[key] = {"key": key, "reviews": pending_waits,
                                   "next_action": checkpoint.get("next_action") or "Check for external response"}
        row["cost_change_near"] = bool("cost-of-delay" in enabled and change_at and now < change_at <= horizon
                                       and (not future or (effort and future > effort)))

    # Waiting and committed work keep their ledger identity without consuming another proposed slot.
    candidates = {key: row for key, row in rows.items()
                  if row["lane"] == "maintenance" and (row["state"] in {"ready", "active"}
                                                       or row["eligible_later_today"])
                  and not row["scheduled"] and key not in previously_selected}
    candidate_keys = set(candidates)
    demand = sum(row["effort_minutes"] or 0 for row in candidates.values())
    arrival_count = sum(period <= at <= now for at in arrivals.values())
    observed_efforts = [row["effort_minutes"] for row in rows.values()
                        if row["lane"] == "maintenance" and row["effort_minutes"]]
    known_arrival_efforts = [
        fields.get(key, {}).get("effort_minutes") for key, at in arrivals.items() if period <= at <= now
    ]
    pressure = max(0, arrival_count - len(maintenance_completions))
    queue_target = (demand / 3 + pressure / 14 * median(observed_efforts)) if observed_efforts else 0
    urgent = {key for key, row in candidates.items() if row["latest_start"]
              and instant(row["latest_start"]) < horizon and not row["overdue"]}
    cost_jumps = {key for key, row in candidates.items() if row["cost_change_near"]}
    urgent_minutes = sum(candidates[key]["effort_minutes"] or 0 for key in urgent | cost_jumps)
    aged = [row["effort_minutes"] or 0 for row in candidates.values() if row["aging_due"]]
    reserve, reserve_source = growth_reservation(rows, capacity, condition, urgent_minutes, share)
    growth_window = None
    if reserve and reserve_source in {"observed_growth_demand", "urgent_pressure"} and windows:
        index = max(range(len(windows)), key=lambda i: windows[i][1] - windows[i][0])
        left, right = windows[index]
        duration = min(right - left, timedelta(minutes=reserve))
        if duration:
            growth_window = {"start": (right - duration).isoformat(), "end": right.isoformat()}
            windows[index:index + 1] = [(left, right - duration)] if left < right - duration else []
    ceiling = (capacity - reserve) * (0.65 if condition == "low" else 1)
    fit = min((row["effort_minutes"] for row in candidates.values()
               if row["effort_minutes"] and row["effort_minutes"] <= ceiling), default=0)
    budget = (min(demand, ceiling, max(queue_target, urgent_minutes, max(aged, default=0), fit))
              if "maintenance-budget" in enabled else 0)

    selected, used, last_batch = [], 0, None
    active_backlog = {key for key, row in candidates.items() if row["state"] == "active"}
    wip_held = []
    while candidates and "maintenance-budget" in enabled:
        def rank(key):
            row = candidates[key]
            checkpoint = row["checkpoint"]
            tier = (0 if key in urgent else 1 if row["cost_change_near"] else 2 if row["aging_due"] else
                    3 if "personal-kanban" in enabled and row["state"] == "active" else
                    4 if "personal-kanban" in enabled and row["external_start"] else 5)
            return (tier, instant(row["latest_start"]) if key in urgent else horizon,
                    -(row["age_days"] or 0) if tier == 2 else 0,
                    not ("attention-residue" in enabled and last_batch and last_batch == checkpoint.get("batch")),
                    row["effort_minutes"] or float("inf"), key)

        key = min(candidates, key=rank)
        row = candidates.pop(key)
        effort = row["effort_minutes"]
        if ("personal-kanban" in enabled and active_backlog and row["state"] == "ready"
                and not (key in urgent or row["aging_due"] or row["cost_change_near"] or row["external_start"])):
            wip_held.append(key)
            continue
        if not effort or used + effort > budget + 1e-9:
            continue
        # Missed deadlines require reassessment, not fictitious retroactive placement.
        if row["latest_start"] and row["slack_minutes"] < 0:
            continue
        duration = timedelta(minutes=effort)
        cutoff = instant(row["latest_start"]) + duration if row["latest_start"] else None
        if key in cost_jumps:
            jump = instant(row["checkpoint"]["cost_change_at"])
            cutoff = min(cutoff, jump) if cutoff else jump
        earliest = max(now.astimezone(UTC), instant(row["checkpoint"].get("available_from")) or now)
        slot = next_slot(windows, duration, earliest, cutoff)
        if slot is None:
            continue
        index, start, end = slot
        left, right = windows[index]
        windows[index:index + 1] = [(a, b) for a, b in ((left, start), (end, right)) if a < b]
        selected.append({"key": key, "start": start.isoformat(), "end": end.isoformat()})
        used += effort
        active_backlog.discard(key)
        last_batch = row["checkpoint"].get("batch")

    selected_keys = {slot["key"] for slot in selected}
    unserved = sorted(key for key, row in rows.items() if row["latest_start"] and not row["timely_commitment"]
                      and instant(row["latest_start"]) < horizon and key not in selected_keys and key not in previously_selected)
    return {"status": "administration_advice", "revision": context["revision"], "methods": sorted(enabled), "work": rows,
            "resume": rows[resume]["checkpoint"] if resume in rows else None,
            "closed": context["closed"], "follow_ups": list(follow_ups.values()),
            "suggested_slots": selected, "protected_growth_window": growth_window, "unserved_time_risk": unserved,
            "unserved_cost_changes": sorted(cost_jumps - selected_keys),
            "unresolved_dependency_graph": sorted(unresolved_graph), "wip_held": wip_held,
            "unserved_aging": sorted(key for key, row in rows.items()
                                     if row["aging_due"] and row["lane"] == "maintenance"
                                     and key not in previously_selected and not row["scheduled"] and row["state"] in {"ready", "active"}
                                     and key not in selected_keys),
            "overdue": context["overdue_deadlines"],
            "next_wait_review_at": min(future_reviews).isoformat() if future_reviews else None,
            "metrics": {"free_minutes": capacity, "maintenance_budget_minutes": budget,
                        "protected_growth_minutes": reserve, "growth_reservation_source": reserve_source,
                        "inferred_lanes": sum("lane" in row["inference"]["fields"] for row in rows.values()),
                        "allocated_maintenance_minutes": used, "remaining_free_minutes": capacity - used,
                        "maintenance_capacity_minutes": ceiling,
                        "ready_maintenance_minutes": demand,
                        "ready_maintenance_count": len(candidate_keys),
                        "unknown_effort": sorted(key for key, row in rows.items() if row["lane"] == "maintenance"
                                                 and row["effort_minutes"] is None),
                        "tracked_tasks_unclassified": sum(key.startswith("task:") and row["lane"] is None
                                                          for key, row in rows.items()),
                        "unclassified": sum(row["lane"] is None for row in rows.values()),
                        "waiting": sum(row["state"] == "waiting" for row in rows.values()),
                        "blocked": sum(row["state"] == "blocked" for row in rows.values()),
                        "oldest_ready_maintenance_days": max((row["age_days"] or 0 for row in rows.values()
                            if row["lane"] == "maintenance" and row["state"] in {"ready", "active"}),
                            default=0),
                        "selected_with_next_action": sum(bool(rows[key]["checkpoint"].get("next_action"))
                                                         for key in selected_keys),
                        "active": sum(row["state"] == "active" for row in rows.values()),
                        "arrivals_14d": arrival_count, "completions_14d": len(maintenance_completions),
                        "arrival_effort_minutes_14d": sum(value for value in known_arrival_efforts if value),
                        "arrivals_with_effort_14d": sum(bool(value) for value in known_arrival_efforts),
                        "completed_effort_minutes_14d": sum(maintenance_completions.values()),
                        "visible_follow_ups": len(follow_ups), "suppressed_follow_ups": sum(
                            row["state"] == "follow_up" for row in rows.values()) - len(follow_ups)}}


def planning_advice(data, tasks, settings, now, context):
    """Return just the information needed by the normal planning and Inbox paths."""
    if os.getenv("PERSONAL_AGENT_ADMIN_ADVICE") != "true":
        return None
    advice = analyze(data, tasks, context["day"], settings, now, context["knowledge"], context=context)
    return {key: advice[key] for key in ("suggested_slots", "protected_growth_window", "follow_ups",
                                         "next_wait_review_at", "unserved_time_risk", "metrics")}
