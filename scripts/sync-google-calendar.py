"""Reconcile the committed task schedule with one owned secondary Google calendar."""

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from http.client import IncompleteRead
from pathlib import Path
from urllib import error, parse, request

OWNER = "personal-agent-starter-kit/calendar-v1"
NAME = "Personal Agent Plan"
BASE = "https://www.googleapis.com/calendar/v3"
SECRETS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "GOOGLE_CALENDAR_ID")
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d+)?)?(Z|[+-]\d\d:?\d\d)")
RATE_LIMIT = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}


class SyncError(Exception):
    pass


class ApiError(SyncError):
    def __init__(self, code, retryable=False, delay=0):
        super().__init__(f"Google API HTTP {code}")
        self.code = code
        self.retryable = retryable
        self.delay = delay


def timestamp(value):
    match = TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("expected an ISO timestamp with a UTC offset")
    zone = match.group(1)
    if zone != "Z":
        hh, mm = int(zone[1:3]), int(zone[-2:])
        if mm > 59 or hh * 60 + mm > 18 * 60:
            raise ValueError("invalid UTC offset")
    fraction = re.search(r"\.(\d+)", value)
    if fraction and fraction[1][6:].strip("0"):
        raise ValueError("timestamp exceeds microsecond precision")
    # Normalize compact offsets and minute-only times before sending RFC3339.
    if zone == "Z":
        zone = "+00:00"
    elif ":" not in zone:
        zone = zone[:3] + ":" + zone[3:]
    return datetime.fromisoformat(value[:match.start(1)] + zone)


def desired_tasks(doc):
    if (not isinstance(doc, dict) or type(doc.get("schema_version")) is not int
            or doc["schema_version"] not in (1, 2) or not isinstance(doc.get("tasks"), list)):
        raise SyncError("Expected tasks.json schema 1 or 2 and a tasks array")
    desired = {}
    for task in doc["tasks"]:
        if not isinstance(task, dict):
            raise SyncError("Invalid task record")
        key = task.get("id")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key) or key in desired:
            raise SyncError("Missing, invalid or duplicate task ID")
        title = task.get("title")
        if not isinstance(title, str) or not title.strip():
            raise SyncError(f"{key}: missing title")
        first, last = task.get("start_time"), task.get("end_time")
        if first is None and last is None:
            desired[key] = None
            continue
        try:
            start, end = timestamp(first), timestamp(last)
            if end <= start:
                raise ValueError("end must follow start")
        except ValueError as exc:
            raise SyncError(f"{key}: invalid schedule ({exc})") from None
        for field in ("project", "priority", "notes"):
            if task.get(field) is not None and not isinstance(task[field], str):
                raise SyncError(f"{key}: invalid {field}")
        description = "\n".join([f"Task ID: {key}"] + [
            f"{field.capitalize()}: {task.get(field) or ''}" for field in ("project", "priority")])
        desired[key] = {"summary": title, "description": description,
                        "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
    return desired


def schedule_episodes(path, scheduled):
    """The first scheduled commit since an explicit clear supplies the insertion key.
    Clearing and re-adding even identical dates gets a fresh key after deletion.
    """
    def git(*args):
        return subprocess.check_output(["git", "-C", str(path.parent), *args], stderr=subprocess.DEVNULL).decode().strip()

    result = {}
    pending = set(scheduled)
    if git("hash-object", "--no-filters", "--", path.name) != git("rev-parse", "HEAD:" + path.name):
        raise SyncError("The input must match the committed task snapshot")
    if not pending:
        return result
    if git("rev-parse", "--is-shallow-repository") != "false":
        raise SyncError("A full checkout is required to recover event IDs")
    for commit in git("log", "--first-parent", "--format=%H", "--", path.name).splitlines():
        if not git("ls-tree", "--name-only", commit, "--", path.name):
            break
        historical = json.loads(git("show", f"{commit}:{path.name}"))
        for task in historical["tasks"]:
            key = task["id"]
            if key not in pending:
                continue
            first, last = task.get("start_time"), task.get("end_time")
            if first is None and last is None:
                pending.remove(key)
            elif first and last:
                result[key] = commit
        if not pending:
            break
    if result.keys() != set(scheduled):
        raise SyncError("Use the committed tasks.json from the target main snapshot")
    return result


def configuration(env):
    missing = [name for name in SECRETS if not env.get(name, "").strip()]
    if missing:
        raise SyncError("Missing GitHub Actions secrets: " + ", ".join(missing))
    target = env["GOOGLE_CALENDAR_ID"].strip()
    if target.lower() == "primary":
        raise SyncError("Specify the secondary calendar ID, not the primary alias")
    return target


def exchange(url, *, method="GET", data=None, headers=None):
    req = request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with request.urlopen(req, timeout=25) as response:
            raw = response.read()
        try:
            return json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            raise ApiError("incomplete response", retryable=True) from None
    except error.HTTPError as exc:
        with exc:
            try:
                body = json.load(exc).get("error", {})
            except (ValueError, UnicodeError, AttributeError):
                body = {}
        reasons = {item.get("reason") for item in body.get("errors", [])} if isinstance(body, dict) else set()
        delay = exc.headers.get("Retry-After", "")
        transient = exc.code in (408, 429, 500, 502, 503, 504) or (exc.code == 403 and bool(reasons & RATE_LIMIT))
        raise ApiError(exc.code, transient, int(delay) if delay.isdigit() else 0) from None
    except (error.URLError, TimeoutError, OSError, EOFError, IncompleteRead):
        raise ApiError("transport", retryable=True) from None


def pause(attempt, delay=0):
    if delay > 60:
        raise SyncError("Google requested a longer cooldown; retry this workflow later")
    time.sleep(max(delay, 2 ** attempt + random.random()))


def read_with_retry(operation):
    for attempt in range(5):
        try:
            return operation()
        except ApiError as exc:
            if not exc.retryable or attempt == 4:
                raise
            pause(attempt, exc.delay)


class GoogleCalendar:
    def __init__(self, env):
        self.calendar = configuration(env)
        self.calendar_path = parse.quote(self.calendar, safe="")
        self.events = f"/calendars/{self.calendar_path}/events"
        self.form = parse.urlencode({"grant_type": "refresh_token", "client_id": env["GOOGLE_CLIENT_ID"],
                                     "client_secret": env["GOOGLE_CLIENT_SECRET"],
                                     "refresh_token": env["GOOGLE_REFRESH_TOKEN"]}).encode()
        self.refresh()

    def refresh(self):
        try:
            token = read_with_retry(lambda: exchange("https://oauth2.googleapis.com/token", method="POST",
                                    data=self.form, headers={"Content-Type": "application/x-www-form-urlencoded"}))
            self.token = token["access_token"]
        except (KeyError, ApiError):
            raise SyncError("OAuth refresh failed. Check client credentials, consent status and refresh token.") from None

    def call(self, path, method="GET", body=None, etag=None):
        def send():
            headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
            if etag:
                headers["If-Match"] = etag
            return exchange(BASE + path, method=method, headers=headers,
                            data=json.dumps(body).encode() if body is not None else None)
        def authorized():
            try:
                return send()
            except ApiError as exc:
                if exc.code != 401:
                    raise
                self.refresh()
                return send()
        return read_with_retry(authorized) if method == "GET" else authorized()


def event_id(key, episode, slot):
    return hashlib.sha256(f"{OWNER}\n{key}\n{episode}\n{slot}".encode()).hexdigest()


def check_owned(event, key):
    private = event.get("extendedProperties", {}).get("private", {})
    episode, slot = private.get("episode", ""), private.get("slot", "")
    if (private.get("personalAgentOwner") != OWNER or private.get("taskId") != key
            or not re.fullmatch(r"[0-9a-f]{40,64}", episode) or not re.fullmatch(r"0|[1-9]\d{0,6}", slot)
            or event.get("id") != event_id(key, episode, slot) or not event.get("etag")
            or event.get("organizer", {}).get("self") is not True or event.get("recurrence")
            or event.get("recurringEventId") or event.get("attendees") or event.get("locked")
            or event.get("eventType", "default") != "default"):
        raise SyncError(f"{key}: ownership conflict or event has guests/recurrence; review in Google Calendar")
    return private


def owned_index(api, keys):
    result = {}
    page = None
    while True:
        query = {"privateExtendedProperty": "personalAgentOwner=" + OWNER, "showDeleted": "false", "maxResults": 2500}
        if page:
            query["pageToken"] = page
        response = api.call(api.events + "?" + parse.urlencode(query))
        for event in response.get("items", []):
            key = event.get("extendedProperties", {}).get("private", {}).get("taskId")
            if key not in keys or event.get("status") == "cancelled":
                continue
            check_owned(event, key)
            if key in result:
                raise SyncError(f"{key}: multiple managed events; review before syncing")
            result[key] = event
        page = response.get("nextPageToken")
        if not page:
            return result


def locate(api, key, episode):
    # A manually deleted event may leave a reserved, cancelled ID at Google.
    # Advance only on positive evidence of deletion, never on a timeout/409.
    for slot in range(100):
        ident = event_id(key, episode, slot)
        try:
            event = api.call(api.events + "/" + ident)
        except ApiError as exc:
            if exc.code == 404:
                return ident, str(slot), None
            if exc.code == 410:
                continue
            raise
        if event.get("status") != "cancelled":
            check_owned(event, key)
            return ident, str(slot), event
    raise SyncError(f"{key}: too many reserved event IDs")


def make_plan(api, desired, episodes):
    target = api.call("/users/me/calendarList/" + api.calendar_path)
    if (target.get("id") != api.calendar or target.get("primary") or target.get("summary") != NAME
            or target.get("accessRole") != "owner" or target.get("deleted")):
        raise SyncError(f"Target must be the owned secondary calendar named {NAME}")
    known = owned_index(api, desired)
    plan = []
    for key, fields in desired.items():
        event = known.get(key)
        if fields is None:
            if event:
                plan.append(("delete", key, event["id"], event["etag"], None))
            continue
        if event is None:
            episode = episodes[key]
            ident, slot, event = locate(api, key, episode)
        offsets = {"sourceStart": fields["start"]["dateTime"], "sourceEnd": fields["end"]["dateTime"]}
        if event is None:
            props = dict(offsets, personalAgentOwner=OWNER, taskId=key, episode=episode, slot=slot)
            body = dict(fields, id=ident, extendedProperties={"private": props})
            plan.append(("create", key, ident, None, body))
            continue
        props = check_owned(event, key)
        patch = {name: fields[name] for name in ("summary", "description") if event.get(name) != fields[name]}
        for name in ("start", "end"):
            try:
                same_instant = timestamp(event[name]["dateTime"]) == timestamp(fields[name]["dateTime"])
            except (KeyError, ValueError):
                same_instant = False
            if not same_instant or props.get("source" + name.title()) != fields[name]["dateTime"]:
                patch[name] = fields[name]
        if any(props.get(k) != v for k, v in offsets.items()):
            patch["extendedProperties"] = {"private": dict(props, **offsets)}
        if patch:
            plan.append(("update", key, event["id"], event["etag"], patch))
    return plan


def sync(api, desired, episodes, apply=False, allow_delete=False):
    for attempt in range(5):
        plan = make_plan(api, desired, episodes)
        counts = Counter(action for action, *_ in plan)
        print(f"{'Apply' if apply else 'Dry run'}: create={counts['create']} update={counts['update']} delete={counts['delete']}")
        for action, key, _, _, body in plan:
            fields = desired[key]
            if fields is None:
                print(f"  delete {key} (schedule cleared)")
            else:
                changes = ", ".join(k for k in body if k in fields)
                print(f"  {action} {key} {fields['start']['dateTime']} → {fields['end']['dateTime']} ({changes})")
        if not apply:
            return plan
        if counts["delete"] and not allow_delete:
            raise SyncError("Plan includes deletions. Review it and authorize deletion before applying.")
        try:
            for action, _, ident, etag, body in plan:
                path = api.events if action == "create" else api.events + "/" + ident
                method = {"create": "POST", "update": "PATCH", "delete": "DELETE"}[action]
                api.call(path, method, body, etag)
            return plan
        except ApiError as exc:
            if attempt == 4 or not (exc.retryable or exc.code in (404, 409, 410, 412)):
                raise
            # A write may have succeeded before the response was lost.
            # Re-read ownership, ETags and desired fields before retrying.
            pause(attempt, exc.delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=Path("tasks.json"))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--validate-only", action="store_true", help="validate the snapshot offline")
    modes.add_argument("--apply", action="store_true", help="apply a reviewed calendar configuration")
    args = parser.parse_args()
    try:
        desired = desired_tasks(json.loads(args.tasks.read_text()))
        scheduled = [key for key, fields in desired.items() if fields is not None]
        print(f"Validated {len(desired)} tasks, {len(scheduled)} scheduled blocks")
        if args.validate_only:
            return 0
        if args.apply and os.getenv("GOOGLE_CALENDAR_LIVE_ENABLED") != "true":
            raise SyncError("Live writes are disabled; review a connected dry run before enabling them")
        configuration(os.environ)
        episodes = schedule_episodes(args.tasks.resolve(), scheduled)
        sync(GoogleCalendar(os.environ), desired, episodes, apply=args.apply,
             allow_delete=os.getenv("PERSONAL_AGENT_ALLOW_CALENDAR_DELETIONS") == "true")
        return 0
    except (SyncError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"Calendar sync failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
