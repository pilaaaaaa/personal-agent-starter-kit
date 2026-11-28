"""Durable request receipts and optimistic Git publication."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from zoneinfo import ZoneInfo

from personal_agent.harness import apply_batch, protocol, snapshot, validate
from personal_agent.store import Conflict, UTC, atomic_write, digest, instant, require

LOG = logging.getLogger(__name__)
ACTIVE = {"ACCEPTED", "EXECUTING"}
TRANSITIONS = {
    "RECEIVED": {"VALIDATED", "NEEDS_INPUT", "FAILED"},
    "VALIDATED": {"ACCEPTED", "FAILED"},
    "ACCEPTED": {"EXECUTING"},
    "EXECUTING": {"EXECUTING", "COMMITTED", "NEEDS_INPUT", "FAILED"},
    "COMMITTED": {"SYNCING", "COMPLETED"},
    "SYNCING": {"SYNCING", "COMPLETED"},
}
PUBLISHED = ("tasks.json", "personal_data/journal.json", "planning/preferences.md",
             "planning/decision_log.md", "operations/personal-agent")


def git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          timeout=20, check=check,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def advance(receipt, state, **fields):
    require(state in TRANSITIONS[receipt["state"]], "Invalid receipt transition")
    stamp = datetime.now(UTC).isoformat()
    return {**receipt, **fields, "state": state, "updated_at": stamp,
            "timeline": {**receipt["timeline"], state: stamp}}


def read_json(path):
    return json.loads(path.read_text())


class Runtime:
    def __init__(self, directory, repository, timezone, *, branch="main", targets=()):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.jobs = self.directory / "requests"
        self.jobs.mkdir(mode=0o700, exist_ok=True)
        self.cache = self.directory / "cache.git"
        self.repository = repository
        self.branch = branch
        git(self.directory, "check-ref-format", f"refs/heads/{branch}")
        self.targets = set(targets) - {""}
        require(self.targets <= {"google_calendar", "apple_reminders"}, "Unknown sync target")
        require(not self.targets or branch == "main", "Built-in provider workflows require the main branch")
        self.zone = ZoneInfo(timezone)
        identity = self.directory / "workspace.json"
        expected = {"repository": repository, "branch": branch, "timezone": timezone}
        with self.lock(self.directory / "workspace.lock", blocking=True):
            self.bind_workspace(identity, expected)

    @staticmethod
    def bind_workspace(identity, expected):
        if identity.exists():
            require(read_json(identity) == expected, "State directory is bound to another workspace or timezone")
        else:
            atomic_write(identity, expected)

    @staticmethod
    def key(request_id):
        from personal_agent.capture import KEY

        require(isinstance(request_id, str) and KEY.fullmatch(request_id), "Invalid request_id")
        return digest(request_id)

    @contextmanager
    def lock(self, path, *, blocking=False):
        with path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
            else:
                yield True

    def submit(self, batch):
        started = monotonic()
        stamp = datetime.now(UTC).isoformat()
        receipt = {"request_id": batch.get("request_id"), "state": "RECEIVED", "timeline": {"RECEIVED": stamp}}
        try:
            validate(batch)
            require(len(json.dumps(batch, ensure_ascii=False, allow_nan=False).encode()) <= 128_000,
                    "Batch exceeds 128 KB")
            key = self.key(batch["request_id"])
        except KeyError as exc:
            return advance(receipt, "NEEDS_INPUT", missing=exc.args[0])
        except (ValueError, TypeError, AttributeError) as exc:
            return advance(receipt, "FAILED", error=str(exc))
        receipt = advance(receipt, "VALIDATED")
        path = self.jobs / (key + ".json")
        # Intake and execution use different locks. Intake only guards the first fsync.
        with self.lock(self.jobs / (key + ".submit"), blocking=True):
            if path.exists():
                job = read_json(path)
                if job["batch"] != batch:
                    return advance(receipt, "FAILED", error="request_id already belongs to a different batch")
                return job["receipt"]
            receipt = advance(receipt, "ACCEPTED", timings_ms={"intake": round((monotonic() - started) * 1000, 2)})
            atomic_write(path, {"batch": batch, "receipt": receipt})
            fd = os.open(self.jobs, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            return receipt

    def result(self, request_id):
        path = self.jobs / (self.key(request_id) + ".json")
        if path.exists():
            return read_json(path)["receipt"]
        # Recover results from Git when moving to another device.
        if self.cache.is_dir():
            output = git(self.cache, "show", f"{self.branch}:operations/personal-agent/{self.key(request_id)}.json", check=False)
            if output.returncode == 0:
                committed = json.loads(output.stdout)
                commit = git(self.cache, "log", "-1", "--format=%H", "main", "--",
                             f"operations/personal-agent/{self.key(request_id)}.json").stdout.strip()
                return {"request_id": request_id, "state": "COMMITTED", "github_commit": commit,
                        "outcomes": committed["outcomes"],
                        "sync": {target: "verification_pending" for target in committed["targets"]}}
        return {"request_id": request_id, "state": "UNKNOWN"}

    def refresh(self):
        with self.lock(self.directory / "refresh.lock") as acquired:
            if acquired:
                if not self.cache.exists():
                    self.cache.mkdir(exist_ok=True)
                if not (self.cache / "HEAD").exists():
                    git(self.cache, "init", "--bare", f"--initial-branch={self.branch}")
                git(self.cache, "fetch", "--no-tags", "--", self.repository, f"+refs/heads/{self.branch}:refs/heads/{self.branch}")

    @contextmanager
    def checkout(self):
        with TemporaryDirectory(prefix="checkout-", dir=self.directory) as directory:
            root = Path(directory)
            git(root, "clone", "--quiet", "--shared", "--single-branch", "--branch", self.branch, "--", str(self.cache), ".")
            yield root

    def read(self, view, day=None, availability=None):
        if not self.cache.is_dir() or git(self.cache, "rev-parse", "--verify", self.branch, check=False).returncode:
            return {"state": "UNAVAILABLE", "error": "Waiting for first repository snapshot"}
        with self.checkout() as root:
            commit = git(root, "rev-parse", "HEAD").stdout.strip()
            if view == "protocol":
                result = protocol(root)
            else:
                result = snapshot(root, self.zone, day, planning=view == "planning", availability=availability)
            return {"state": "AVAILABLE", "github_commit": commit, "timezone": self.zone.key, **result}

    def jobs_in(self, states):
        now = datetime.now(UTC)
        eligible = []
        for path in self.jobs.glob("*.json"):
            receipt = read_json(path)["receipt"]
            retry = instant(receipt.get("retry_at"))
            if receipt["state"] in states and (retry is None or retry <= now):
                eligible.append((receipt["updated_at"], path))
        return [path for _, path in sorted(eligible)]

    def process(self, path):
        with self.lock(path.with_suffix(".execute")) as acquired:
            if not acquired:
                return
            job = read_json(path)
            receipt = job["receipt"]
            if receipt["state"] not in ACTIVE:
                return
            batch = job["batch"]
            receipt = advance(receipt, "EXECUTING", attempts=receipt.get("attempts", 0) + 1, retry_at=None)
            atomic_write(path, {**job, "receipt": receipt})
            started = monotonic()
            try:
                commit, saved, timings = self.publish(path.stem, batch)
            except (KeyError, ValueError, TypeError, AttributeError, Conflict) as exc:
                state = "NEEDS_INPUT" if isinstance(exc, KeyError) else "FAILED"
                receipt = advance(receipt, state, error=str(exc))
                atomic_write(path, {**job, "receipt": receipt})
                return
            except (OSError, subprocess.SubprocessError):
                # Publication may have happened. Recover by reading the marker from main.
                LOG.warning("Publication unconfirmed for %s", path.stem)
                delay = min(30, 0.25 * 2 ** min(receipt["attempts"], 7))
                receipt = advance(receipt, "EXECUTING", retry_at=(datetime.now(UTC) + timedelta(seconds=delay)).isoformat(),
                                  error="Git publication unconfirmed; retry scheduled")
                atomic_write(path, {**job, "receipt": receipt})
                return
            sync = dict.fromkeys(saved["targets"], "workflow_pending")
            receipt = advance(receipt, "COMMITTED", github_commit=commit, outcomes=saved["outcomes"], sync=sync, error=None,
                              timings_ms={**receipt["timings_ms"], **timings,
                                          "execution": round((monotonic() - started) * 1000, 2)})
            atomic_write(path, {**job, "receipt": receipt})
            receipt = advance(receipt, "SYNCING" if sync else "COMPLETED")
            atomic_write(path, {**job, "receipt": receipt})

    def publish(self, key, batch):
        started = monotonic()
        with self.checkout() as root:
            git(root, "fetch", "--no-tags", "--", self.repository, f"refs/heads/{self.branch}")
            git(root, "reset", "--hard", "FETCH_HEAD")
            snapshot_ms = round((monotonic() - started) * 1000, 2)
            relative = f"operations/personal-agent/{key}.json"
            marker = root / relative
            if marker.exists():
                saved = read_json(marker)
                require(saved["fingerprint"] == digest(batch), "request_id already belongs to a different batch")
                commit = git(root, "log", "-1", "--format=%H", "--", relative).stdout.strip()
                return commit, saved, {"git_snapshot": snapshot_ms, "recovered_commit": True}
            started = monotonic()
            outcomes = apply_batch(root, batch, self.zone)
            business_ms = round((monotonic() - started) * 1000, 2)
            paths = set(git(root, "diff", "--name-only").stdout.splitlines())
            targets = []
            if "personal_data/journal.json" in paths:
                targets.append("google_calendar")
            if "tasks.json" in paths:
                targets.append("apple_reminders")
            saved = {"fingerprint": digest(batch), "outcomes": outcomes,
                     "targets": sorted(self.targets.intersection(targets))}
            marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(marker, saved)
            started = monotonic()
            git(root, "add", "--", *(name for name in PUBLISHED if (root / name).exists()))
            git(root, "-c", "user.name=Personal Agent", "-c", "user.email=personal-agent@users.noreply.github.com",
                "commit", "--quiet", "-m", "Apply structured Personal Agent request")
            commit = git(root, "rev-parse", "HEAD").stdout.strip()
            git(root, "push", "--", self.repository, f"HEAD:refs/heads/{self.branch}")
            git(root, "fetch", "--no-tags", "--", self.repository, f"refs/heads/{self.branch}")
            git(root, "merge-base", "--is-ancestor", commit, "FETCH_HEAD")
            return commit, saved, {"git_snapshot": snapshot_ms, "business": business_ms,
                                   "git_commit_push": round((monotonic() - started) * 1000, 2)}

    def verify(self, path, verifier):
        with self.lock(path.with_suffix(".execute")) as acquired:
            if not acquired:
                return
            job = read_json(path)
            receipt = job["receipt"]
            if receipt["state"] == "COMMITTED":
                receipt = advance(receipt, "SYNCING" if receipt["sync"] else "COMPLETED")
                atomic_write(path, {**job, "receipt": receipt})
            if receipt["state"] != "SYNCING":
                return
            sync = {target: verdict if verdict == "verified" else verifier(target, receipt["github_commit"])
                    for target, verdict in receipt["sync"].items()}
            state = "COMPLETED" if all(verdict == "verified" for verdict in sync.values()) else "SYNCING"
            if sync != receipt["sync"] or state != receipt["state"]:
                atomic_write(path, {**job, "receipt": advance(receipt, state, sync=sync)})
