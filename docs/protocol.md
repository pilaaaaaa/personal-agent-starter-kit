# Shared state protocol

Use a private repo with `tasks.json`, `planning/`, `methods/` and `AGENTS.md`. The chosen GitHub branch is the source of truth when connected. A disconnected local Git workspace can run the same workflow. Before writing personal knowledge to GitHub, verify the exact target belongs to the user and is Private. Never push private state to the kit repository. See [the update protocol](../UPDATE.md) for framework upgrades and `agent-config.json`.

## Snapshot and compare-and-swap

Read the latest branch head and pin all reads to that commit. Select the target by ID, checking for existing equivalent work before creating a new task. Generate a UUID once per new intent and retain it across retries. Record the old values of edited fields so you can detect overlapping writes. Preserve unknown fields, unrelated edits and histories. Validate the whole document with `python3 scripts/validate_tasks.py`.

With Git, start the transaction from the pinned revision in a clean checkout, commit the scoped diff and use an ordinary fast-forward push. Never force-push. On rejection or an uncertain result fetch the latest head, check whether the same intent already landed, and reapply only to unchanged target fields. For a GitHub connector, use a commit with an expected head or a non-forced ref update of a commit whose sole parent is the pinned head. File-only clients can use the file SHA for a single-file update when the change depends only on that file. Use a branch-head check for plans that depend on several files. A connector without conditional writes should produce a patch for an authorized writer. Review the resulting latest revision. If another writer changed the same field differently, resolve that conflict before writing.

## Task records

| Field | Meaning |
| --- | --- |
| `id`, `title` | Stable unique ID of 1–128 ASCII letters, digits, hyphens or underscores, and a human label |
| `priority`, `status` | P0–P3 as a display hint. Status is todo, done, completed (legacy alias), or cancelled |
| `project`, `notes` | Optional text. Consider visibility before copying task details to external providers |
| `due` | Null or object with local `date` (YYYY-MM-DD), optional `time` (HH:MM) and IANA `timezone`. This is a real deadline |
| `reminder_at` | Optional offset timestamp for a requested reminder trigger |
| `start_time`, `end_time` | Both null or both offset timestamps in chronological order. This is the reserved execution window |
| `depends_on` | Optional predecessor IDs. A cancelled predecessor requires reassessment |
| `waiting` | Optional on an open task. Contains `on` (condition or external dependency), `review_at` (offset timestamp), and optionally a private reference |
| `reminder_list` | Optional Apple destination list name in the approved account. Omit for the dedicated default |

New work starts at todo. Modify in place by ID. Set done or cancelled and clear `waiting` when closing. Reopen explicitly when requested. Cancellation retains history. Preserve a historical execution block unless the user also requests unscheduling. Clear both schedule fields to unschedule, since deleting the task record leaves an existing Google event alone. Clear a waiting item when resolved, or set its next review on new evidence. Reuse the same record for the same reply, approval or application.

Store recurring routines separately. Derive temporary microsteps in the plan. A task commit can trigger external sync when automation is enabled. Review downstream actions before writing, especially calendar deletion and destination changes.

[GitHub Contents API reference](https://docs.github.com/en/rest/repos/contents)
