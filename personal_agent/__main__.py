"""JSON command interface for Personal Agent."""
import argparse
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfoNotFoundError

from .store import UTC, atomic_write, digest, instant, read, read_snapshot, review_data, write
from .timeline import markdown

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--root", type=Path, default=Path.cwd())
parser.add_argument("--no-maintenance", action="store_true",
                    help="Read a pinned checkout without persisting lifecycle changes")
commands = parser.add_subparsers(dest="command", required=True)
commands.add_parser("snapshot")
commands.add_parser("maintain")
admin = commands.add_parser("administration", aliases=["administration-shadow"], help="Read a pinned snapshot and suggest maintenance allocation")
admin.add_argument("--day", type=date.fromisoformat, required=True)
admin.add_argument("--at", help="Replay time with an explicit UTC offset")
commands.add_parser("reminders-import")
commands.add_parser("intake-preview", help="Check text span coverage and emit a minimized intake record")
turn = commands.add_parser("turn", help="Capture host-interpreted observations from stdin")
turn.add_argument("--enqueue", action="store_true", help="Stage without processing")
turn.add_argument("--request-only", action="store_true", help="Emit an immutable Connector request")
capture = commands.add_parser("capture-status", help="Inspect one turn or list unresolved turns")
capture.add_argument("id", nargs="?")
commands.add_parser("write", help="Read one structured operation from stdin")
context = commands.add_parser("execution-context", help="Read planning settings JSON from stdin")
context.add_argument("--day", required=True)
draft = commands.add_parser("execution-draft", help="Return read-only candidate or edited recommendation")
draft.add_argument("--day", required=True)
check = commands.add_parser("execution-check", help="Monitor the Plan against reported Actual activity")
check.add_argument("--timezone", required=True)
check.add_argument("--day", type=date.fromisoformat)
check.add_argument("--lead-minutes", type=int, default=10)
commands.add_parser("execution-validate", help="Preview a full proposed plan record from stdin")
commands.add_parser("execution-compare", help="Compare candidate plans against the same source snapshot")
review = commands.add_parser("review")
review.add_argument("--from", dest="first", type=date.fromisoformat, required=True)
review.add_argument("--through", type=date.fromisoformat)
review.add_argument("--timezone", required=True)
review.add_argument("--format", choices=("json", "markdown"), default="json")
calendar = commands.add_parser("calendar")
calendar.add_argument("--apply", action="store_true")
calendar.add_argument("--verify-projection", action="store_true", help="Read back both calendar tracks after apply")
calendar.add_argument("--projection-receipt", type=Path, help="Apply and verify Actual against this Journal snapshot")
calendar.add_argument("--audit-plan", action="store_true",
                      help="Report same-window unmanaged Plan events for review")
calendar.add_argument("--events", nargs=2, metavar=("START", "END"),
                      help="Query configured calendars for RFC3339 bounds")


def run(args):
    path = args.root / "personal_data/journal.json"
    tasks_path = args.root / "tasks.json"
    now = datetime.now(UTC)
    if args.command == "intake-preview":
        from .intake import preview

        return preview(json.load(sys.stdin))
    if args.command in {"administration", "administration-shadow"}:
        if os.getenv("PERSONAL_AGENT_ADMIN_ADVICE") != "true":
            raise ValueError("Experimental administration advice is disabled")
        from .admin_shadow import analyze
        from .sources import read_knowledge
        from .store import require

        request = json.load(sys.stdin)
        require(set(request) <= {"settings", "days", "options"}
                and ("settings" in request) != ("days" in request), "Provide settings or rolling days")
        knowledge = read_knowledge(args.root)
        now = instant(args.at) if args.at else now
        data, tasks = read_snapshot(path, tasks_path)
        if "days" in request:
            from .admin_rolling import forecast

            require(args.day.isoformat() == min(request["days"], default=""),
                    "Day must match the start of the forecast")
            return forecast(data, tasks, request["days"], now, knowledge, request.get("options"))
        return analyze(data, tasks, args.day.isoformat(), request["settings"], now,
                       knowledge, request.get("options"))
    from .lifecycle import maintain_checkout
    if args.command == "reminders-import":
        from .reminders import import_checkout
        return import_checkout(args.root, json.load(sys.stdin), now)
    if args.command == "maintain":
        return maintain_checkout(args.root, now)
    if args.command in {"turn", "capture-status"}:
        from .interaction import LocalInbox, capture_turn, request_for

        transport = LocalInbox(args.root)
        if args.command == "capture-status":
            return transport.status(args.id) if args.id else transport.pending()
        message = json.load(sys.stdin)
        if args.request_only:
            return request_for(message, message["observations"])
        return capture_turn(message, message["observations"],
                            lambda request: transport.submit(request, process=not args.enqueue))
    if args.command == "write":
        return write(path, tasks_path, json.load(sys.stdin), root=args.root)
    if args.command == "calendar":
        data = read(path)
        if args.verify_projection and (not args.apply or args.audit_plan or args.events):
            raise ValueError("Projection verification requires calendar apply")
        if args.projection_receipt:
            if not args.apply or args.audit_plan or args.events:
                raise ValueError("Projection receipt requires --apply")
            atomic_write(args.projection_receipt, {"status": "failed", "journal_revision": digest(data)})
        if (os.getenv("PERSONAL_CALENDAR_PROVIDER") or "none") == "none":
            result = {"status": "disconnected"}
            if args.verify_projection:
                raise ValueError("Projection verification requires a configured Calendar provider")
            if args.projection_receipt:
                atomic_write(args.projection_receipt, {**result, "journal_revision": digest(data)})
            return result
        if os.environ["PERSONAL_CALENDAR_PROVIDER"] != "google":
            raise ValueError("Unknown calendar provider")
        from .google import GoogleAdapter
        namespace = os.environ["PERSONAL_CALENDAR_NAMESPACE"]
        if not namespace or len(namespace) > 128:
            raise ValueError("Invalid calendar namespace")
        if args.apply and os.getenv("PERSONAL_CALENDAR_LIVE_ENABLED") != "true":
            raise ValueError("Personal calendar writes are disabled")
        targets = {"plan": os.environ["GOOGLE_CALENDAR_ID"],
                   "actual": os.getenv("GOOGLE_ACTUAL_CALENDAR_ID")}
        if args.apply and (not all(targets.values()) or targets["plan"] == targets["actual"]):
            raise ValueError("Live Plan and Actual require distinct calendar targets")
        colors = {"plan": os.getenv("PERSONAL_PLAN_COLOR") or "9",
                  "actual": os.getenv("PERSONAL_ACTUAL_COLOR") or "10"}
        adapter = GoogleAdapter(os.environ, namespace)
        if args.audit_plan:
            if args.apply or args.events:
                raise ValueError("Plan audit is read-only")
            target = targets["plan"]
            adapter.authorization(target)
            return {"status": "plan_audit", "candidates": adapter.plan_duplicate_candidates(
                target, (now - timedelta(days=1)).isoformat(), (now + timedelta(days=7)).isoformat())}
        if args.events:
            start, end = args.events
            if args.apply or instant(start) >= instant(end):
                raise ValueError("Events query requires increasing bounds and read-only mode")
            events = {}
            for target in dict.fromkeys(filter(None, targets.values())):
                adapter.authorization(target)
                events[target] = adapter.events(target, start=start, end=end)
            return {"status": "read", "events": events}
        alert = os.getenv("PERSONAL_PLAN_ALERT_MINUTES")
        alert_minutes = int(alert) if alert else None
        if alert_minutes is not None and not 0 <= alert_minutes <= 60:
            raise ValueError("Plan popup lead must be between 0 and 60 minutes")
        if args.verify_projection and not all(targets.get(track) for track in ("plan", "actual")):
            raise ValueError("Projection verification requires both calendar tracks")
        result = adapter.sync(data, targets, now, apply=args.apply, colors=colors, plan_alert_minutes=alert_minutes)
        if args.verify_projection:
            preview = adapter.sync(data, targets, now, colors=colors, plan_alert_minutes=alert_minutes)
            if preview["actions"] or preview["disabled_tracks"]:
                raise ValueError("Calendar readback did not match the committed projection")
            result["projection_verified"] = True
        if args.projection_receipt:
            from .calendar import verify_actual_projection
            projection = verify_actual_projection(adapter, data, targets["actual"], now, color=colors["actual"])
            atomic_write(args.projection_receipt, projection)
            if projection["status"] != "verified":
                raise ValueError("Actual projection readback did not verify this snapshot")
        return result
    lifecycle = None if args.no_maintenance else maintain_checkout(args.root, now)
    data, tasks = read_snapshot(path, tasks_path)
    if args.command in {"execution-context", "execution-check", "execution-validate", "execution-compare", "execution-draft"}:
        from zoneinfo import ZoneInfo

        from .admin_shadow import planning_advice
        from .execution import build_context, build_draft, monitor, validate_plan
        from .sources import draft_inputs, live_inputs, read_knowledge
        from .store import require

        require(path.is_file(), "Journal source is missing")
        if args.command == "execution-check":
            day = args.day or now.astimezone(ZoneInfo(args.timezone)).date()
            return monitor(data, tasks, day.isoformat(), args.timezone, now, lead_minutes=args.lead_minutes,
                           knowledge=read_knowledge(args.root))
        knowledge = read_knowledge(args.root)
        if args.command == "execution-compare":
            from .execution import compare_plans

            request = json.load(sys.stdin)
            require(isinstance(request, dict) and 1 <= len(request) <= 8, "Provide 1 to 8 named candidates")
            first = next(iter(request.values()))
            inputs = first["execution"]["inputs"]
            require(inputs == live_inputs(first["day"], inputs, now),
                    "Calendar evidence changed; rebuild snapshot")
            return compare_plans(data, tasks, request, now, knowledge)
        if args.command == "execution-validate":
            record = json.load(sys.stdin)
            inputs = record["execution"]["inputs"]
            require(inputs == live_inputs(record["day"], inputs, now), "Calendar evidence changed; rebuild snapshot")
            return validate_plan(data, tasks, record, now, knowledge)
        request = json.load(sys.stdin)
        if args.command == "execution-draft":
            inputs = draft_inputs(args.day, request["inputs"], now)
            context = build_context(data, tasks, args.day, inputs, now, knowledge)
            return {**build_draft(context, request.get("edits", [])),
                    "administration": planning_advice(data, tasks, inputs, now, context)}
        inputs = live_inputs(args.day, request, now)
        from .proactive import signals

        captured = build_context(data, tasks, args.day, inputs, now, knowledge)
        return {"inputs": inputs, **captured, "proactive": signals(captured, now),
                "administration": planning_advice(data, tasks, inputs, now, captured)}
    if args.command == "review":
        return review_data(data, tasks, args.first, args.through or args.first, args.timezone, now)
    return {"revision": digest(data), "journal": data, "tasks": tasks, "lifecycle": lifecycle}


if __name__ == "__main__":
    try:
        args = parser.parse_args()
        result = run(args)
        print(markdown(result) if args.command == "review" and args.format == "markdown"
              else json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, AttributeError, OSError, ZoneInfoNotFoundError) as exc:
        print(f"Personal Agent operation failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        # Provider errors are deliberately stripped of request URLs and response bodies.
        from .google import legacy
        if not isinstance(exc, legacy.SyncError):
            raise
        print(f"Calendar sync failed: {exc}", file=sys.stderr)
        sys.exit(1)
