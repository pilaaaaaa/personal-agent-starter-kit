"""Evidence-backed signals for the agent's existing decision methods."""
from datetime import timedelta

from .store import digest, instant


def signals(context, now):
    rows = []
    methods = context["knowledge"].get("methods")

    def emit(kind, category, key, method, evidence, next_step, revision=None):
        method_enabled = methods is None or methods.get(method, {}).get("enabled")
        if not method_enabled and category != "risk":
            return
        rows.append({"id": digest([kind, key, revision or evidence])[:24], "kind": kind,
                     "category": category, "key": key, "method": method if method_enabled else None, "evidence": evidence,
                     "next_step": next_step, "authorization": "advisory_only"})

    for key, reason in sorted(context["conflicts"].items()):
        emit("state_conflict", "risk", key, "context-audit", reason, "reconcile_evidence")

    for key in context.get("overdue_deadlines", []):
        emit("hard_deadline_overdue", "risk", key, "cost-of-delay",
             context["pool"][key]["deadline"], "reassess_deadline_and_scope")

    for ident, row in sorted(context["information"].items()):
        status, target = row["disposition"], row.get("target")
        if target in context["closed"] or status in {"handled", "no_action"}:
            continue
        if row.get("day") and row["day"] > context["day"]:
            continue
        trigger, expiry = instant(row.get("trigger_at")), instant(row.get("expires_at"))
        evidence = {"source": row["source"]["id"], "version": row["version"], "trigger_at": row.get("trigger_at"),
                    "expires_at": row.get("expires_at")}
        if expiry and now >= expiry:
            if row["kind"] in {"task", "commitment", "constraint", "waiting"}:
                emit("expired_evidence", "suggestion", ident, "event-driven-decision-chain",
                     evidence, "reassess", row["version"])
            continue
        if row.get("blocking"):
            emit("blocking_question", "risk", ident, "value-of-information",
                 evidence, "clarify", row["version"])
        if trigger is None or now >= trigger:
            if status == "waiting" and trigger:
                emit("follow_up_window", "risk" if row.get("depends_on") else "suggestion",
                     ident, "event-driven-decision-chain", evidence, "check_if_reply_arrived", row["version"])
            if row["kind"] == "idea" and status == "recorded":
                emit("optional_opportunity", "opportunity", ident, "preference-opportunity-prioritization",
                     evidence, "check_fit_and_compatibility", row["version"])
        dependencies = [key for key in row.get("depends_on", [])
                        if context["closed"].get(key) not in {"completed", "done"}]
        if dependencies and status != "waiting":
            emit("unmet_dependency", "risk" if row.get("day") and row["day"] <= context["day"] else "suggestion", ident, "critical-path-dependency-analysis",
                 {**evidence, "dependencies": dependencies}, "verify_prerequisite", row["version"])

    for key, timing in sorted(context.get("planned_timing", {}).items()):
        latest = instant(timing.get("latest_start"))
        if key in context["pool"] and key not in context["ongoing"] and latest and (
                now - timedelta(hours=2) <= latest <= now + timedelta(hours=1)):
            emit("latest_start_window", "risk", key, "cost-of-delay",
                 {"latest_start": timing["latest_start"]}, "review_start_or_scope")
    # Stable IDs let consumers suppress repeats. Never suppress critical findings.
    priority = {"risk": 0, "opportunity": 1, "suggestion": 2}
    rows.sort(key=lambda row: (priority[row["category"]], row["kind"], row["key"]))
    risks = [row for row in rows if row["category"] == "risk"]
    discretionary = [row for row in rows if row["category"] != "risk"]
    return {"signals": risks + discretionary[:3], "additional_discretionary": max(0, len(discretionary) - 3)}
