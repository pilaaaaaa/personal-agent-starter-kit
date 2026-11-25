"""OAuth and REST transport for the optional Google calendar adapter."""
import json
import random
import time
from http.client import IncompleteRead
from urllib import error, parse, request

BASE = "https://www.googleapis.com/calendar/v3"
RATE_LIMIT = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}


class SyncError(Exception):
    pass


class ApiError(SyncError):
    def __init__(self, code, retryable=False, delay=0):
        super().__init__(f"Google API HTTP {code}")
        self.code = code
        self.retryable = retryable
        self.delay = delay


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
        required = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
        if any(not env.get(name, "").strip() for name in required):
            raise SyncError("Google OAuth credentials are missing")
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
