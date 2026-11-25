"""Operate a private Personal Agent workspace."""
import argparse
from datetime import date, datetime
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfoNotFoundError

from .google_transport import SyncError
from .settings import settings
from .store import UTC, digest, instant, recover, review_data, workspace_snapshot, write
from .timeline import markdown


def run(args):
    root = args.root
    path = root / "personal_data/journal.json"
    tasks_path = root / "tasks.json"
    if args.command == "write":
        return write(path, tasks_path, json.load(sys.stdin))
    if args.command == "recover":
        return {"recovered": recover(path, tasks_path)}
    if args.command == "inbox":
        from .inbox import drain

        drain(root)
        return None
    if args.command == "calendar":
        targets, colors, namespace = settings(root, os.environ)
        if not targets:
            return {"status": "disconnected"}
        if args.apply and os.getenv("PERSONAL_AGENT_CALENDAR_LIVE_ENABLED") != "true":
            raise ValueError("Calendar live writes are disabled")
    data, tasks = workspace_snapshot(path, tasks_path)
    now = datetime.now(UTC)
    if args.command == "calendar":
        from .google import GoogleAdapter

        adapter = GoogleAdapter(os.environ, namespace)
        if args.audit_plan or args.events:
            start, end = args.audit_plan or args.events
            if instant(start) >= instant(end):
                raise ValueError("Query requires increasing time bounds")
            if args.audit_plan:
                if "plan" not in targets:
                    raise ValueError("Plan calendar is disabled")
                target = targets["plan"]
                adapter.authorization(target)
                return {"status": "plan_audit",
                        "candidates": adapter.plan_duplicate_candidates(target, start, end)}
            events = {}
            for track, target in targets.items():
                adapter.authorization(target)
                events[track] = adapter.events(target, start=start, end=end)
            return {"status": "read", "events": events}
        return adapter.sync(data, targets, now, apply=args.apply, tasks=tasks, colors=colors,
                            allow_delete=os.getenv("PERSONAL_AGENT_ALLOW_CALENDAR_DELETIONS") == "true")
    if args.command == "review":
        return review_data(data, tasks, args.first, args.through or args.first, args.timezone, now)
    return {"revision": digest(data), "tasks_revision": digest(tasks), "journal": data, "tasks": tasks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("snapshot")
    commands.add_parser("write", help="Read one structured operation from stdin")
    commands.add_parser("recover", help="Finish an interrupted local write")
    commands.add_parser("inbox", help="Process authorized queued connector requests")
    review = commands.add_parser("review")
    review.add_argument("--from", dest="first", type=date.fromisoformat, required=True)
    review.add_argument("--through", type=date.fromisoformat)
    review.add_argument("--timezone", required=True)
    review.add_argument("--format", choices=("json", "markdown"), default="json")
    calendar = commands.add_parser("calendar")
    mode = calendar.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--audit-plan", nargs=2, metavar=("START", "END"))
    mode.add_argument("--events", nargs=2, metavar=("START", "END"))
    args = parser.parse_args()
    try:
        result = run(args)
        if result is not None:
            print(markdown(result) if args.command == "review" and args.format == "markdown"
                  else json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError, ZoneInfoNotFoundError, SyncError) as exc:
        print(f"Personal Agent operation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
