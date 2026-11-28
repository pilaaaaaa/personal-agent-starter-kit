"""Measure the real MCP streamable HTTP path with private, temporary Git data."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import itertools
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from personal_agent.calendar import projection
from personal_agent.store import UTC, index, read
from datetime import datetime
from plugins.personal_agent.runtime import Runtime, git
from test_harness import actual, batch, fixture


def wait(action, description, timeout=30):
    until = monotonic() + timeout
    while monotonic() < until:
        result = action()
        if result:
            return result
        sleep(0.05)
    raise AssertionError(f"Timed out: {description}")


@contextmanager
def server(root, remote, port, display_name="Fieldnote"):
    env = {**os.environ, "PERSONAL_AGENT_STATE_DIR": str(root / "state"),
           "PERSONAL_AGENT_REPOSITORY": str(remote), "PERSONAL_AGENT_TIMEZONE": "UTC",
           "PERSONAL_AGENT_DISPLAY_NAME": display_name, "PERSONAL_AGENT_PORT": str(port),
           "PERSONAL_AGENT_MCP_TOKEN": "fictional-smoke-only",
           "PERSONAL_AGENT_SYNC_TARGETS": "google_calendar,apple_reminders",
           "PERSONAL_CALENDAR_PROVIDER": "none"}
    with (root / "server.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, "-m", "plugins.personal_agent.server"], env=env, stdout=log, stderr=log)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=25)


def main():
    with TemporaryDirectory() as directory, socket.socket() as probe:
        root = Path(directory)
        fixture_root = root / "fixture"
        fixture_root.mkdir()
        remote = fixture(fixture_root)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        url = f"http://127.0.0.1:{port}/mcp"
        ids = itertools.count(1)
        samples = defaultdict(list)

        def request(method, params, *, authenticated=True):
            payload = json.dumps({"jsonrpc": "2.0", "id": next(ids), "method": method, "params": params}).encode()
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                       "MCP-Protocol-Version": "2025-11-25"}
            if authenticated:
                headers["Authorization"] = "Bearer fictional-smoke-only"
            with urlopen(Request(url, payload, headers), timeout=10) as response:
                output = json.load(response)
            assert "error" not in output, output
            return output["result"]

        def call(name, **arguments):
            started = monotonic()
            result = request("tools/call", {"name": name, "arguments": arguments})
            samples[name].append((monotonic() - started) * 1000)
            assert not result.get("isError"), result
            return result["structuredContent"]

        def ready():
            try:
                return call("read_state")["state"] == "AVAILABLE"
            except URLError:
                return False

        def receipt(ident):
            result = call("get_execution", request_id=ident)
            assert result["state"] not in {"FAILED", "NEEDS_INPUT"}, result
            return result if "github_commit" in result else None

        runtime = Runtime(root / "state", str(remote), "UTC")
        recovered = batch("recover", actual("meal", "Synthetic restart meal"))
        receipts = []
        with server(root, remote, port) as process:
            wait(ready, "MCP read")
            initialize = request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                               "clientInfo": {"name": "Synthetic harness acceptance", "version": "1"}})
            assert initialize["serverInfo"]["name"] == "Fieldnote", initialize
            try:
                request("tools/list", {}, authenticated=False)
            except HTTPError as failure:
                assert failure.code == 401
            else:
                raise AssertionError("Unauthenticated MCP request succeeded")
            assert {tool["name"] for tool in request("tools/list", {})["tools"]} == {
                "get_protocol", "read_state", "submit_operations", "get_execution"}
            for _ in range(10):
                assert call("get_protocol")["state"] == "AVAILABLE"
            for _ in range(30):
                assert call("read_state")["state"] == "AVAILABLE"

            mixed = batch("mixed-http", actual("forms", "Synthetic forms", task_id="forms", complete_task=True),
                          {"key": "follow-up", "kind": "task", "record": {"title": "Synthetic follow-up"}},
                          {"key": "context", "kind": "intent", "expected": None, "record": {
                              "id": "synthetic-status", "kind": "state", "status": "open",
                              "title": "Synthetic context", "certainty": "stated", "evidence": "Synthetic"}})
            assert call("submit_operations", **mixed)["state"] == "ACCEPTED"
            first = wait(lambda: receipt(mixed["request_id"]), "mixed publication")
            assert call("submit_operations", **mixed)["github_commit"] == first["github_commit"]
            receipts.append(first)
            with runtime.lock(runtime.jobs / (runtime.key("recover") + ".execute"), blocking=True):
                assert call("submit_operations", **recovered)["state"] == "ACCEPTED"
                process.kill()
                process.wait(timeout=5)

        with server(root, remote, port, "Fieldnote Studio"):
            wait(ready, "restarted MCP read")
            renamed = request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                             "clientInfo": {"name": "Synthetic acceptance", "version": "1"}})
            assert renamed["serverInfo"]["name"] == "Fieldnote Studio"
            assert receipt(mixed["request_id"])["github_commit"] == first["github_commit"]
            receipts.append(wait(lambda: receipt("recover"), "recovered publication"))
            with ThreadPoolExecutor(max_workers=4) as workers:
                def post(number):
                    payload = batch(f"concurrent-{number}", actual("actual", f"Synthetic activity {number}"))
                    assert call("submit_operations", **payload)["state"] == "ACCEPTED"
                    return payload["request_id"]

                pending = list(workers.map(post, range(24)))
            receipts.extend(wait(lambda: receipt(ident), ident) for ident in pending)

        runtime.refresh()
        with runtime.checkout() as checkout:
            journal = read(checkout / "personal_data/journal.json")
            assert len(index(journal)[1]) == 26
            assert len(projection(journal, datetime.now(UTC))[0]["actual"]) == 26
            assert json.loads((checkout / "tasks.json").read_text())["tasks"][0]["status"] == "completed"
        assert len({item["github_commit"] for item in receipts}) == 26
        for item in receipts:
            git(remote, "merge-base", "--is-ancestor", item["github_commit"], "main")
            timeline = item["timeline"]
            accepted = datetime.fromisoformat(timeline["ACCEPTED"])
            executed = datetime.fromisoformat(timeline["EXECUTING"])
            committed = datetime.fromisoformat(timeline["COMMITTED"])
            samples["accepted_to_committed"].append((committed - accepted).total_seconds() * 1000)
            samples["accepted_to_final_attempt"].append((executed - accepted).total_seconds() * 1000)
            for stage, duration in item["timings_ms"].items():
                samples[stage].append(duration)
        report = {}
        for name, values in samples.items():
            ordered = sorted(values)
            report[name] = {"n": len(ordered), **{
                f"p{p}": round(ordered[math.ceil(len(ordered) * p / 100) - 1], 2) for p in (50, 95)}}
        print(json.dumps({"unit": "ms", "optimistic_retries": sum(item["attempts"] - 1 for item in receipts),
                          "timings": report}, indent=2))


if __name__ == "__main__":
    main()
