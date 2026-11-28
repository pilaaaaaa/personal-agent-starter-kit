"""Personal Agent's private MCP adapter. Business execution lives in personal_agent."""
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import re
import subprocess
from typing import Literal

import anyio
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from secrets import compare_digest
from typing_extensions import NotRequired, TypedDict
import uvicorn

from plugins.personal_agent.runtime import ACTIVE, Runtime, git
from personal_agent.store import require

LOG = logging.getLogger(__name__)
mcp = MCPServer(os.getenv("PERSONAL_AGENT_DISPLAY_NAME", "Personal Agent"))


class Operation(TypedDict):
    key: str
    kind: Literal["actual", "task", "plan", "intent", "preference", "correction", "delete_actual", "intake_commitments"]
    record: dict[str, object]
    expected: NotRequired[str | None]


def configured():
    return Runtime(Path(os.environ["PERSONAL_AGENT_STATE_DIR"]), os.environ["PERSONAL_AGENT_REPOSITORY"],
                   os.environ["PERSONAL_AGENT_TIMEZONE"],
                   branch=os.getenv("PERSONAL_AGENT_BRANCH", "main"),
                   targets=os.getenv("PERSONAL_AGENT_SYNC_TARGETS", "").split(","))


@mcp.custom_route("/healthz", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok", "service": "personal-agent"})


@mcp.tool(annotations={"readOnlyHint": True})
async def get_protocol() -> dict[str, object]:
    """Get AGENTS.md, current context, preferences, and enabled decision methods from cached private branch.
    The calling agent interprets natural language, resolves ambiguity and chooses actions.
    Check this protocol before executing a new conversation or when the Git revision changes."""
    return await anyio.to_thread.run_sync(configured().read, "protocol")


@mcp.tool(annotations={"readOnlyHint": True})
async def read_state(day: str | None = None, for_planning: bool = False,
                     availability: list[dict[str, str]] | None = None) -> dict[str, object]:
    """Read current tasks, Actual, effective Plan, Journal context and the complete candidate set.
    day is YYYY-MM-DD in the returned timezone; omit for today. Use stable IDs and task_revisions.
    Set for_planning=true when proposing a new Plan; this explicitly reads configured live
    calendar evidence and can take longer. Optionally supply availability as start/end windows
    with offsets for the proposed Plan. For 'what should I do now?', reason from this
    snapshot and the enabled methods. Publish a Plan only with existing or explicit authorization."""
    runtime = configured()
    try:
        return await anyio.to_thread.run_sync(runtime.read, "planning" if for_planning else "state", day,
                                              availability, abandon_on_cancel=True)
    except (ValueError, TypeError, KeyError) as exc:
        return {"state": "NEEDS_INPUT", "error": str(exc)}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
async def submit_operations(request_id: str, conversation_id: str, observed_at: str,
                            operations: list[Operation]) -> dict[str, object]:
    """Durably accept 1–32 ordered structured personal operations from one user message.
    Supply a globally unique request_id, stable conversation_id, and original RFC3339 message
    timestamp with offset; on retry reuse the exact payload and request_id. Each operation
    has a unique ASCII key and a kind-specific record defined by get_protocol.
    actual: state, verbatim evidence, name for activity; include known start/end with offsets,
    optional day, occurrence, duration_minutes, parallel_keys or Actual ID. Use completed +
    task_id + complete_task=true for explicit task completion; task_cancelled and task_reopened
    use task_id and evidence. All Actual observations are evaluated together at the first Actual;
    put any required task creation earlier. Omit unknown boundaries for conservative estimation.
    task: title to create; task_id plus expected task_revisions value to edit; distinguish
    day/time reminders from deadline_day. preference: scope=preferences or decision_log,
    summary and verbatim evidence. intent: Journal action/detail/constraint/state, stable ID,
    certainty, status, title and evidence; expected=null to create or current revision to update.
    plan: day, items or patch, decision, execution and publication authorization; expected
    is plan_expected from read_state. Account for every candidate and applicable input.
    correction / delete_actual / intake_commitments use the Journal protocol.
    ACCEPTED confirms a local durable receipt. Use get_execution to check Git publication and
    separate Google and Apple Mac readback. NEEDS_INPUT reports missing structured fields."""
    batch = dict(request_id=request_id, conversation_id=conversation_id,
                 observed_at=observed_at, operations=operations)
    return await anyio.to_thread.run_sync(configured().submit, batch)


@mcp.tool(annotations={"readOnlyHint": True})
async def get_execution(request_id: str) -> dict[str, object]:
    """Read durable execution status by submit_operations request_id. COMMITTED confirms Git
    publication. SYNCING reports external targets individually. Apple verification covers the
    runner Mac EventKit; check iCloud propagation on the device separately."""
    try:
        return await anyio.to_thread.run_sync(configured().result, request_id)
    except ValueError as exc:
        return {"state": "NEEDS_INPUT", "error": str(exc)}


def github_projection(runtime, target, commit):
    match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?", runtime.repository)
    if not match:
        return "verification_unavailable"
    # A later run is also evidence if it verified the exact same projection source.
    source = "tasks.json" if target == "apple_reminders" else "personal_data/journal.json"
    heads = [commit]
    try:
        latest = git(runtime.cache, "rev-parse", runtime.branch).stdout.strip()
        if latest != commit and git(runtime.cache, "merge-base", "--is-ancestor", commit, latest,
                                    check=False).returncode == 0:
            blobs = git(runtime.cache, "rev-parse", f"{commit}:{source}", f"{latest}:{source}").stdout.splitlines()
            if blobs[0] == blobs[1]:
                heads.append(latest)
        for head in heads:
            name = f"pa-{target}-{head}"
            output = subprocess.run(["gh", "api", f"repos/{match[1]}/actions/artifacts?name={name}&per_page=1"],
                                    capture_output=True, text=True, timeout=10, check=True)
            if any(not artifact["expired"] for artifact in json.loads(output.stdout)["artifacts"]):
                return "verified"
        return "workflow_pending"
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return "verification_unavailable"


async def refresh(runtime):
    while True:
        try:
            await anyio.to_thread.run_sync(runtime.refresh)
        except (OSError, subprocess.SubprocessError):
            LOG.warning("Personal Agent repository snapshot refresh failed")
        await anyio.sleep(5)


async def execute_pending(runtime):
    limiter = anyio.CapacityLimiter(4)

    running = set()

    async def process(path):
        try:
            await anyio.to_thread.run_sync(runtime.process, path, limiter=limiter)
        finally:
            running.discard(path)

    async with anyio.create_task_group() as workers:
        while True:
            if (runtime.cache / "refs/heads" / runtime.branch).exists() or (runtime.cache / "packed-refs").exists():
                paths = await anyio.to_thread.run_sync(runtime.jobs_in, ACTIVE)
                for path in (item for item in paths if item not in running):
                    if len(running) >= 4:
                        break
                    running.add(path)
                    workers.start_soon(process, path)
            await anyio.sleep(0.15)


async def verify_pending(runtime):
    verifier = lambda target, commit: github_projection(runtime, target, commit)
    while True:
        paths = await anyio.to_thread.run_sync(runtime.jobs_in, {"COMMITTED", "SYNCING"})
        for path in paths:
            await anyio.to_thread.run_sync(runtime.verify, path, verifier)
        await anyio.sleep(20)


def check_repository(repository):
    local = Path(repository)
    if local.is_absolute() and local.is_dir():
        return
    match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?", repository)
    require(match is not None, "Use an absolute local Git path or a credential-free GitHub HTTPS URL")
    result = subprocess.run(["gh", "repo", "view", match[1], "--json", "visibility,viewerPermission"],
                            capture_output=True, text=True, timeout=15, check=True)
    metadata = json.loads(result.stdout)
    require(metadata["visibility"] == "PRIVATE" and metadata["viewerPermission"] in {"ADMIN", "MAINTAIN", "WRITE"},
            "The service requires write access to a private GitHub repository")


def http_app():
    app = mcp.streamable_http_app(stateless_http=True, json_response=True)
    mcp_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(service):
        runtime = configured()
        await anyio.to_thread.run_sync(check_repository, runtime.repository)
        async with mcp_lifespan(service), anyio.create_task_group() as workers:
            workers.start_soon(refresh, runtime)
            workers.start_soon(execute_pending, runtime)
            workers.start_soon(verify_pending, runtime)
            yield
            workers.cancel_scope.cancel()

    app.router.lifespan_context = lifespan
    token = os.getenv("PERSONAL_AGENT_MCP_TOKEN")
    if token:
        async def authorize(request, call_next):
            header = request.headers.get("authorization", "").encode()
            if request.url.path != "/healthz" and not compare_digest(header, b"Bearer " + token.encode()):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=authorize)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(http_app(), host="127.0.0.1", port=int(os.getenv("PERSONAL_AGENT_PORT", "8787")))
