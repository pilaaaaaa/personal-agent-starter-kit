"""Google Calendar adapter using OAuth and REST."""
from datetime import timedelta
import hashlib
from urllib.parse import quote, urlencode

from .calendar import CalendarAdapter, reconcile
from .store import instant

from . import google_transport as transport


class GoogleAdapter(CalendarAdapter):
    def __init__(self, env, namespace):
        self.api = transport.GoogleCalendar(env)
        self.namespace = namespace

    def pages(self, path, query):
        while True:
            response = self.api.call(path + "?" + urlencode(query))
            yield from response.get("items", [])
            token = response.get("nextPageToken")
            if not token:
                return
            query = dict(query, pageToken=token)

    def calendars(self):
        return list(self.pages("/users/me/calendarList", {}))

    def authorization(self, target):
        result = self.api.call("/users/me/calendarList/" + quote(target, safe=""))
        if (result.get("id") != target or result.get("accessRole") != "owner"
                or result.get("primary") or result.get("deleted")):
            raise transport.SyncError("Each target must be an owned secondary calendar")

    def event_path(self, target):
        return "/calendars/" + quote(target, safe="") + "/events"

    def event_id(self, key, slot):
        return hashlib.sha256(f"{self.namespace}\0{key}\0{slot}".encode()).hexdigest()

    def owned(self, event, key):
        private = event.get("extendedProperties", {}).get("private", {})
        slot = private.get("paSlot")
        return (isinstance(key, str) and private.get("paOwner") == self.namespace and private.get("paKey") == key
                and isinstance(slot, str) and slot in {str(i) for i in range(100)}
                and event.get("id") == self.event_id(key, slot)
                and event.get("organizer", {}).get("self") is True and event.get("etag")
                and event.get("eventType", "default") == "default"
                and not any(event.get(field) for field in
                            ("attendees", "recurrence", "recurringEventId", "locked")))

    def events(self, target, *, managed=False, start=None, end=None):
        query = {"showDeleted": "false"}
        if start:
            query["timeMin"] = start
        if end:
            query["timeMax"] = end
        if managed:
            query["privateExtendedProperty"] = "paOwner=" + self.namespace
        rows = self.pages(self.event_path(target), query)
        if not managed:
            return list(rows)
        result = {}
        for event in rows:
            if event.get("status") == "cancelled":
                continue
            key = event.get("extendedProperties", {}).get("private", {}).get("paKey")
            if not key or key in result or not self.owned(event, key):
                raise transport.SyncError("Managed projection ownership conflict")
            result[key] = event
        return result

    def plan_duplicate_candidates(self, target, start, end):
        """Identify unmanaged rows for review while preserving ambiguous events."""
        owned = {}
        unmanaged = []
        for event in self.events(target, start=start, end=end):
            if event.get("status") == "cancelled":
                continue
            start_at = instant(event.get("start", {}).get("dateTime"))
            end_at = instant(event.get("end", {}).get("dateTime"))
            if not (start_at and end_at):
                continue
            props = event.get("extendedProperties", {}).get("private", {})
            verified = self.owned(event, props.get("paKey"))
            slot = start_at, end_at
            if verified:
                owned.setdefault(slot, []).append(event["id"])
            else:
                unmanaged.append((slot, event["id"]))
        return [{"unmanaged_id": ident, "managed_ids": owned[slot]}
                for slot, ident in unmanaged if slot in owned]

    def body(self, event):
        # Google may return timestamps at second precision. Round outward so even
        # a sub-second source interval projects to a valid non-empty event.
        start = None if event.get("all_day") else instant(event["start"])
        end = None if event.get("all_day") else instant(event["end"])
        if end and end.microsecond:
            end += timedelta(seconds=1)
        body = {"summary": event["title"],
                "description": event.get("description", ""),
                "start": {"dateTime": start.isoformat(timespec="seconds")} if start else {"date": event["start"]},
                "end": {"dateTime": end.isoformat(timespec="seconds")} if end else {"date": event["end"]},
                "transparency": "transparent" if event["free"] else "opaque",
                "reminders": {"useDefault": False, "overrides": []},
                "visibility": "private"}
        if start and "timezone" in event:
            for field in ("start", "end"):
                body[field]["timeZone"] = event["timezone"]
        if "color" in event:
            body["colorId"] = event["color"]
        return body

    def matches(self, existing, event):
        body = self.body(event)
        for field in ("start", "end"):
            remote = existing.get(field, {})
            desired = body.pop(field)
            if "date" in desired:
                if remote.get("date") != desired["date"]:
                    return False
                continue
            if instant(remote.get("dateTime")) != instant(desired["dateTime"]):
                return False
            if "timeZone" in desired and remote.get("timeZone") != desired["timeZone"]:
                return False
        return (existing.get("summary") == body["summary"]
                and (existing.get("description") or "") == body["description"]
                and existing.get("visibility") == body["visibility"]
                and existing.get("transparency", "opaque") == body["transparency"]
                and existing.get("colorId") == body.get("colorId")
                and existing.get("reminders", {}).get("useDefault") is False
                and not existing.get("reminders", {}).get("overrides"))

    def create(self, target, key, event):
        path = self.event_path(target)
        for i in range(100):
            slot = str(i)
            ident = self.event_id(key, slot)
            try:
                current = self.api.call(f"{path}/{ident}")
            except transport.ApiError as exc:
                if exc.code == 410:
                    continue
                if exc.code != 404:
                    raise
                props = {"paOwner": self.namespace, "paKey": key, "paSlot": slot}
                self.api.call(path, "POST",
                              dict(self.body(event), id=ident, extendedProperties={"private": props}))
                return
            if current.get("status") == "cancelled":
                continue
            if not self.owned(current, key):
                raise transport.SyncError("Calendar event ID collision")
            if not self.matches(current, event):
                self.update(target, key, current, event)
            return
        raise transport.SyncError("Calendar event ID slots exhausted")

    def update(self, target, key, existing, event):
        self.mutate(target, key, existing, "PATCH", dict({"colorId": None}, **self.body(event)))

    def delete(self, target, key, existing):
        self.mutate(target, key, existing, "DELETE")

    def mutate(self, target, key, existing, method, body=None):
        if not self.owned(existing, key):
            raise transport.SyncError("Refused to modify an event without verified ownership")
        self.api.call(self.event_path(target) + "/" + existing["id"],
                      method, body, existing["etag"])

    def sync(self, *args, **kwargs):
        for attempt in range(5):
            try:
                return reconcile(self, *args, **kwargs)
            except transport.ApiError as exc:
                if attempt == 4 or not (exc.retryable or exc.code in {409, 410, 412}):
                    raise
                transport.pause(attempt, exc.delay)
