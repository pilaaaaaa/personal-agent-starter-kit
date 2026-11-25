# Plan, Actual and review

The journal works locally on Linux and macOS with Python 3.10+ and IANA time zone data. A Git-capable executor can publish authorized state to the user's verified private branch. Calendar connections are optional. Plan and Actual can be enabled independently, or kept entirely local.

## Journal protocol

Run these commands from the workspace root. Use `--root PATH` before the subcommand when needed.

```sh
python3 -m personal_agent snapshot
python3 -m personal_agent write < request.json
python3 -m personal_agent review --from 2030-05-10 --through 2030-05-12 --timezone UTC --format markdown
```

`snapshot` returns the journal and task ledger plus `revision` and `tasks_revision`. A write takes `id`, `base` (journal revision), `tasks_base` (task revision) and the operation below. Use a unique operation ID and retry the identical payload after transport uncertainty. Reusing an accepted ID with different input fails. On a stale revision, read both files, re-evaluate the intended change and submit a new operation. An unchanged plan can return a no-op. The log at `personal_data/journal.json` is created on its first accepted mutation. Keep it, request payloads and review output private.

| Kind | Record fields and behavior |
| --- | --- |
| `plan` | `day`, full `items` list, and `decision` containing `trigger`, `reason`, `information` and `constraints` (the last two are lists). Optional future `effective_at` for standalone blocks. Items take stable `id`, `title`, `start`, `end`, optional `status` (scheduled, deferred, cancelled, blocked, waiting), `notes`, `task_ids` and `parallel_to`. Use unique block IDs across unrelated plans and keep a block's ID across revisions and day moves. |
| `actual` | New records require stable `id`, `name`, `category`, `timezone`, `source`, `status` (ongoing, occurred, completed, cancelled) and `precision` (exact, estimated, unknown). Optional `start`, `end`, `reported_day`, `estimate_basis`, `duration_minutes`, `source_event_id`, `task_ids`, `plan_item_ids`, `parallel_ids` and `outputs`. Use an explicit `reported_day` for an undated backfill. An estimate requires a basis. |
| `delete_actual` | `id` and `reason` record a tombstone while retaining history. |
| `correction` | `version_id`, `reason`, `source` and `annotation` annotate a historical plan. |
| `review` | `from`, `through`, `findings` and optional `feedback`. Feedback carries `id`, `type`, `status`, `evidence`, `scope`, `confidence`, `recheck` and `rule`, optionally `expires_at`. See the supported types in `personal_agent/store.py`. An active confirmed preference requires `confirmed_by_user: true`. |

A fictional plan item could be `{"id":"block-gallery","title":"Label mock gallery prints","start":"2030-05-10T10:00:00Z","end":"2030-05-10T10:40:00Z","task_ids":[]}`. Use offset timestamps and an IANA time zone for Actual. For a replan, submit a new full version for the affected day. Initial is the first version in the effective sequence. Rolling is the latest effective version at the review time. Future versions superseded before taking effect remain visible in review history. Actual intervals link to effective plan versions by explicit item ID or overlapping task ID. Reviews report elapsed coverage, exact and estimated duration, unknown duration and overlaps. Point records and date-only summaries are display placeholders. See [daily review](../methods/daily-review.md).

A task-linked block takes effect immediately and mirrors its times and optional display overrides to existing task fields. A task can have one linked block in a plan version and retains its ID when rescheduled. Moving across days updates the affected versions and retains the task ID. Unrelated task fields, including deadlines and reminders, are preserved. To backfill or correct an activity, use the same activity ID. Include `source` and `reason` on a correction. Use a stable source event ID scoped to the source if importing evidence, and check for the matching activity first. Include `complete_task` only when the evidence explicitly completes that same linked task. A completion correction can retract the receipt. Keep historical tasks with a terminal status so linked references and cleanup keys remain resolvable.

Local CLI writers share a workspace lock. If interrupted while updating two files, use `python3 -m personal_agent recover` before publishing. Recovery checks both file revisions before replacing either file. A conflict requires reconciling the private files before continuing. Direct task editors should use the same lock or serialize their writes with the CLI. Across checkouts use the [snapshot and compare-and-swap protocol](protocol.md#snapshot-and-compare-and-swap) and publish the journal and task changes together. Run sync from the latest verified revision, with a single live sync runner per namespace.

For connector-based intake, an authorized executor may run `python3 -m personal_agent inbox` on a verified private checkout. Put immutable `operations/inbox/<id>.json` requests there with `id`, `kind`, `record` and `expected` (latest entry ID for that day or activity, or null if new). This route supports plan, actual, delete_actual and correction. Inspect `operations/results/` and commit the resulting changes together against the verified head. Keep inbox files and results private. A queued request alone is pending.

## Connect Google Calendar

Use owned secondary calendars and the [Google OAuth setup](../integrations/README.md#google-calendar). Each enabled track needs its own target. Google is the bundled provider. A different service requires an adapter implementing `personal_agent.calendar.CalendarAdapter` and a corresponding CLI provider configuration. Supply these via the runtime environment or private repository secrets.

| Name | Purpose |
| --- | --- |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | Google OAuth credentials for the authorized calendar account. |
| `PERSONAL_AGENT_PLAN_CALENDAR_ID`, `PERSONAL_AGENT_ACTUAL_CALENDAR_ID` | Target IDs, required only for enabled tracks. |
| `PERSONAL_AGENT_CALENDAR_NAMESPACE` | Stable random identifier (16–128 letters, digits, underscores or hyphens). Keep it unchanged for a connected workspace. |
| `PERSONAL_AGENT_CALENDAR_LIVE_ENABLED` | Set to `true` after reviewing a preview and authorizing live writes. |
| `PERSONAL_AGENT_ALLOW_CALENDAR_DELETIONS` | Set to `true` only with authorization to remove stale managed projections. |

Set `calendars.provider` to `google` in private `agent-config.json`. Set `calendars.plan.enabled` or `calendars.actual.enabled` to `true` for the chosen tracks. An enabled track can have an optional Google event `color` as a string from `"1"` to `"11"`. Missing settings default to disconnected. Disabling a track suspends sync and retains existing events.

Run `python3 -m personal_agent calendar` for a connected preview, then add `--apply` after review and authorization. Verify with `--events START END`, using explicit offset timestamps. The optional private-repo workflow in `integrations/workflows/dual-calendar.yml` can be installed manually and dispatched on main. To opt into live sync on relevant commits and hourly refresh, set the private Actions variable `PERSONAL_AGENT_DUAL_CALENDAR_AUTO_APPLY=true` after an authorized preview and smoke test. The same live and deletion gates apply. Keep workflow logs and local readback private.

Plan projects upcoming task and standalone time blocks as busy. Actual projects private, transparent activities without reminders. Corrections update the same stable projection whenever possible. The sync verifies ownership and uses conditional ETag updates and deletes. Managed event fields follow the journal or task ledger. Enter corrections there to keep the source and projection aligned. Use `--audit-plan START END` to review unmanaged events occupying the same window as a managed event. Manual or old integration events require user review before any separate cleanup. Unknown remote keys are preserved when a local record cannot establish their provenance.

## Upgrade an existing workspace

Use the [framework update protocol](../UPDATE.md) to compare the installed release with the tagged release and apply only the agreed framework changes. Keep the existing task ledger, planning files and configuration. Missing `calendars` settings require no migration. Add the selected settings only when ready to connect. Existing tasks with future schedules can project in Plan after preview. The journal begins with new operations and scoped, authorized backfills.

An existing Google task-only workflow can continue alongside Actual-only sync. To switch Plan to the new projector, pause the old Plan writer first and choose a fresh secondary Plan calendar, or review a manual cutover for existing events. The new projector uses a different ownership namespace and cannot adopt the previous events. Keep the old calendar intact until the preview and new Plan readback are verified.
