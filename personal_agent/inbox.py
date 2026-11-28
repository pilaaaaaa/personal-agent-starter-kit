"""Validate immutable Connector requests and write receipts for the current checkout."""
import fcntl
import json
from datetime import datetime
from pathlib import Path
import re
from zoneinfo import ZoneInfoNotFoundError

from .lifecycle import maintain_checkout
from .intents import FIELDS as INTENT_FIELDS, current_intents
from .store import Conflict, UTC, atomic_write, digest, index, read, read_snapshot, require, write

FIELDS = {
    "execution_context": {"day", "inputs"},
    "execution_draft": {"day", "inputs", "edits"},
    "intent": INTENT_FIELDS,
    "capture_turn": {"conversation_id", "observed_at", "timezone", "observations", "supersedes", "intake"},
    "maintain_tasks": set(),
    "intake_commitments": {"intake", "source", "spans", "units"},
    "plan": {"day", "items", "decision", "effective_at", "execution", "publication", "patch"},
    "actual": {"id", "name", "category", "status", "start", "end", "timezone", "precision",
               "source", "estimate_basis", "reported_day", "reason", "task_ids", "complete_task",
               "plan_item_ids", "parallel_ids", "source_event_id", "duration_minutes", "effort"},
    "delete_actual": {"id", "reason"},
    "correction": {"version_id", "reason", "source", "annotation"},
}
ITEM_FIELDS = {"id", "title", "start", "end", "status", "notes", "task_ids", "parallel_to"}


def operation(root, request, ident):
    require(set(request) == {"id", "kind", "record", "expected"} and request["id"] == ident,
            "Request fields must be id, kind, record and expected; filename must equal id")
    kind, record = request["kind"], request["record"]
    require(kind in FIELDS and isinstance(record, dict) and set(record) <= FIELDS[kind],
            "Unsupported operation or unexpected record fields")
    now = datetime.now(UTC)
    if kind == "maintain_tasks":
        require(request["expected"] is None, "Maintenance uses expected=null")
        return maintain_checkout(root, now)
    if kind == "execution_context":
        maintain_checkout(root, now)
    journal = root / "personal_data/journal.json"
    if kind in {"execution_context", "execution_draft"}:
        from .admin_shadow import planning_advice
        from .execution import build_context, build_draft
        from .sources import draft_inputs, live_inputs, read_knowledge
        from .proactive import signals

        require(request["expected"] is None, "Context capture uses expected=null")
        require(journal.is_file(), "Journal source is missing")
        data, tasks = read_snapshot(journal, root / "tasks.json")
        fetch = draft_inputs if kind == "execution_draft" else live_inputs
        settings = fetch(record["day"], record["inputs"], now)
        context = build_context(data, tasks, record["day"], settings, now, read_knowledge(root))
        administration = planning_advice(data, tasks, settings, now, context)
        if kind == "execution_draft":
            return {"status": "draft_available", **build_draft(context, record.get("edits", [])),
                    "administration": administration}
        # Tasks/Journal/knowledge are already accessible at the same Git ref. Keep
        # the mutable receipt limited to the exact evidence needed for Plan.
        fields = {"revision", "captured_at", "due", "ongoing", "closed", "conflicts", "calibration",
                  "legacy_slots", "replan_impact", "overdue_deadlines", "waiting_tasks",
                  "candidates", "applicable_details", "constraints"}
        return {"status": "context_available", "inputs": settings, "journal_revision": digest(data),
                "pool_ids": sorted(context["pool"]), "proactive": signals(context, now),
                "administration": administration,
                "information_versions": {ident: row["version"]
                                         for ident, row in context["information_scope"].items()},
                **{key: value for key, value in context.items() if key in fields}}
    if kind == "capture_turn":
        require(ident.startswith("capture-turn-") and request["expected"] is None,
                "Capture turn requires a capture-turn ID and expected=null")
        supersedes = record.get("supersedes")
        if supersedes is not None:
            require(re.fullmatch(r"capture-turn-[A-Za-z0-9_-]{1,115}", supersedes) and supersedes != ident,
                    "Invalid superseded capture ID")
            previous = root / "operations/results" / (supersedes + ".json")
            try:
                failure = json.loads(previous.read_text())
                original = json.loads((root / "operations/inbox" / previous.name).read_text())
            except FileNotFoundError as exc:
                raise ValueError("Superseded capture request and receipt required") from exc
            require(failure["status"] == "rejected"
                    and original["record"]["conversation_id"] == record["conversation_id"],
                    "Repair must refer to a rejected request in the same conversation")
            from .capture import FACTS

            require({item["key"] for item in original["record"]["observations"] if item["state"] in FACTS}
                    <= {item["key"] for item in record["observations"]}, "Repair must account for original facts")
        result = write(journal, root / "tasks.json", {
            "id": ident, "kind": kind, "record": record,
        }, root=root)
        return {"status": "accepted", "fingerprint": digest(record), **result["entry"]["record"]}
    data = read(journal)
    plans, *_ = index(data)
    current = None
    if kind == "plan":
        items = record["patch"]["upsert"] if "patch" in record else record["items"]
        require(all(isinstance(item, dict) and set(item) <= ITEM_FIELDS for item in items),
                "Unexpected plan item fields")
        versions = plans.get(record["day"], [])
        current = versions[-1]["id"] if versions else None
    elif kind == "intent":
        current = current_intents(data).get(record["id"], {}).get("revision")
    elif kind in {"actual", "delete_actual"}:
        current = next((entry["id"] for entry in reversed(data["operations"])
                        if entry["kind"] in {"actual", "delete_actual"}
                        and entry["record"]["id"] == record["id"]), None)
    require(current == request["expected"], "Target revision changed; read latest main and submit a new operation")
    result = write(journal, root / "tasks.json", {
        "id": ident, "kind": kind, "record": record, "base": digest(data),
    }, datetime.now(UTC), root=root)
    return "accepted" if result["changed"] else "no_change"


def drain(root, only=None):
    require(only is None or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", only), "Invalid Inbox ID")
    lock_path = root / "personal_data/.inbox.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        drain_locked(root, only)


def drain_locked(root, only):
    maintenance = maintain_checkout(root, datetime.now(UTC)) if only is None else None
    if maintenance is not None:
        print("Lifecycle:", json.dumps(maintenance, ensure_ascii=False))
    inbox = root / "operations/inbox"
    receipts = root / "operations/results"
    receipts.mkdir(parents=True, exist_ok=True)
    paths = [inbox / (only + ".json")] if only else sorted(inbox.glob("*.json"))
    for path in paths:
        ident = path.stem
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
            print("Skipped invalid inbox filename")
            continue
        receipt = receipts / path.name
        last = json.loads(receipt.read_text()) if receipt.exists() else {}
        if last and last["status"] != "pending":
            if ident.startswith("capture-turn-") and last["status"] == "accepted":
                require(not path.is_symlink() and path.stat().st_size <= 128_000,
                        f"Invalid accepted capture request: {ident}")
                original = json.loads(path.read_text())
                require(original.get("id") == ident and original.get("kind") == "capture_turn"
                        and original.get("expected") is None
                        and digest(original["record"]) == last["fingerprint"],
                        f"Accepted capture request changed: {ident}")
            continue
        try:
            require(path.stat().st_size <= 128_000 and not path.is_symlink(), "Invalid inbox file")
            request = json.loads(path.read_text())
            status = operation(root, request, ident)
            result = status if isinstance(status, dict) else {"status": status}
            if request["kind"] == "maintain_tasks":
                result["batch_lifecycle"] = maintenance
        except (Conflict, OSError) as exc:
            status = "pending" if ident.startswith("capture-turn-") else "rejected"
            result = {"status": status, "error": str(exc)}
        except (ValueError, KeyError, TypeError, AttributeError, ZoneInfoNotFoundError) as exc:
            result = {"status": "rejected", "error": str(exc)}
        except Exception as exc:
            from .google import legacy

            if not isinstance(exc, legacy.SyncError):
                raise
            result = {"status": "rejected", "error": f"Required Calendar source failed: {exc}"}
        result.update(id=ident, processed_at=datetime.now(UTC).isoformat())
        if ident.startswith("capture-turn-"):
            result["attempts"] = last.get("attempts", 0) + 1
            if last.get("error"):
                result["last_failed_attempt"] = {"at": last["processed_at"], "error": last["error"]}
        atomic_write(receipt, result)
        if not only:
            print(f"{ident}: {result['status']}")


if __name__ == "__main__":
    drain(Path.cwd())
