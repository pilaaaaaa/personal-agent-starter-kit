"""Validate immutable Connector requests and write receipts for the current checkout."""
import json
from datetime import datetime
from pathlib import Path
import re
from zoneinfo import ZoneInfoNotFoundError

from .store import UTC, atomic_write, digest, index, recover, require, workspace_snapshot, write

FIELDS = {
    "plan": {"day", "items", "decision", "effective_at"},
    "actual": {"id", "name", "category", "status", "start", "end", "timezone", "precision",
               "source", "estimate_basis", "reported_day", "reason", "task_ids", "complete_task",
               "plan_item_ids", "parallel_ids", "source_event_id", "duration_minutes", "outputs"},
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
    journal = root / "personal_data/journal.json"
    data, tasks = workspace_snapshot(journal, root / "tasks.json")
    request_fingerprint = digest(request)
    saved = next((entry for entry in data["operations"] if entry["id"] == ident), None)
    if saved:
        require(saved.get("request_fingerprint") == request_fingerprint, "Operation ID conflict")
        return "accepted"
    plans, *_ = index(data)
    current = None
    if kind == "plan":
        require(all(isinstance(item, dict) and set(item) <= ITEM_FIELDS for item in record["items"]),
                "Unexpected plan item fields")
        versions = plans.get(record["day"], [])
        current = versions[-1]["id"] if versions else None
    elif kind in {"actual", "delete_actual"}:
        current = next((entry["id"] for entry in reversed(data["operations"])
                        if entry["kind"] in {"actual", "delete_actual"}
                        and entry["record"]["id"] == record["id"]), None)
    require(current == request["expected"], "Target revision changed; read latest main and submit a new operation")
    result = write(journal, root / "tasks.json", {
        "id": ident, "kind": kind, "record": record, "base": digest(data), "tasks_base": digest(tasks),
        "request_fingerprint": request_fingerprint,
    }, datetime.now(UTC))
    return "accepted" if result["changed"] else "no_change"


def drain(root):
    inbox = root / "operations/inbox"
    receipts = root / "operations/results"
    recover(root / "personal_data/journal.json", root / "tasks.json")
    receipts.mkdir(parents=True, exist_ok=True)
    for path in sorted(inbox.glob("*.json")):
        ident = path.stem
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
            print("Skipped invalid inbox filename")
            continue
        receipt = receipts / path.name
        if receipt.exists():
            continue
        try:
            require(path.stat().st_size <= 128_000 and not path.is_symlink(), "Invalid inbox file")
            request = json.loads(path.read_text())
            status = operation(root, request, ident)
            result = {"status": status}
        except (ValueError, KeyError, TypeError, AttributeError, ZoneInfoNotFoundError) as exc:
            result = {"status": "rejected", "error": str(exc)}
        result.update(id=ident, processed_at=datetime.now(UTC).isoformat())
        atomic_write(receipt, result)
        print(f"{ident}: {result['status']}")


if __name__ == "__main__":
    drain(Path.cwd())
