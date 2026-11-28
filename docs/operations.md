# Structured execution protocol

Use the user's language and natural phrasing in conversation. Collect all distinct reported activities, task changes, planning changes and preferences in the turn. Read the pinned state when selecting IDs or proposing a Plan, then submit a single ordered batch. Automatic Actual capture and lifecycle changes require the user's durable grant. Confirm an ambiguity only if resolving it materially changes an authorized action. Summarize explicitly requested writes using the actual receipt.

A batch contains `request_id`, `conversation_id`, `observed_at` (original RFC 3339 time with offset), and `operations`. Each operation has a unique ASCII `key`, a `kind`, a typed `record` and, for versioned changes, an `expected` value. Create tasks before their dependent Actual operations. All Actual observations in the batch are evaluated together at the first Actual position. The runtime publishes all accepted state changes in one Git commit.

| Kind | Record |
| --- | --- |
| `actual` | `state`, verbatim `evidence` and `name` for an activity. Use known offset `start` and `end`, optional local `day`, `duration_minutes` and `estimate_basis`. Use `occurrence` to distinguish repeated activities, `parallel_keys` for simultaneous batch observations, and `actual_id` with `correction: true` to correct a stable occurrence. |
| `task` | `title` for creation. For an existing task supply `task_id` and its `expected` digest. Changes support `notes`, `day`, `time` and `deadline_day`. A planned execution date, reminder time and hard deadline have different meanings. |
| `preference` | `scope` is `preferences` or `decision_log`. Include `summary` and explicit user `evidence`. |
| `intent` | Stable `id`, `kind`, `status`, `title`, `evidence` and `certainty`. Use null `expected` to create. Use current record revision for updates. |
| `plan` | `day`, `items` or `patch`, `decision`, `execution` and explicit `publication` evidence. Use `plan_expected` from the snapshot. Every open candidate requires a disposition, including deferred and waiting work. |
| `correction`, `delete_actual`, `intake_commitments` | Supply a scoped Journal record. Corrections require `version_id`, `reason` and `source`. Deletion requires stable `id`, `reason` and current revision. Intake records explicit waiting and commitment evidence. |

Actual intervals preserve the original timezone and can span midnight. Supply reported bounds when known. An incomplete interval can remain unlocated, or the harness can estimate it conservatively from supported evidence. Keep originally reported activity separate from Plan. A partial Actual reports `progress` against the task while leaving it open. Explicit completion uses `state: completed`, `task_id` and `complete_task: true`. Explicit cancellation and reopening use `task_cancelled` or `task_reopened`, with `task_id` and evidence. Match the intended task before committing a terminal status.

Consult [the harness](../personal_agent/harness.py), [capture fields](../personal_agent/capture.py), [Journal write schemas](../personal_agent/inbox.py) and [Plan validation](../personal_agent/execution.py) for exact structured details. Plan publication is a separate user-authorized decision. Choose and combine enabled [methods](../methods/README.md) where relevant. Check the complete candidate set before publishing and re-read the state after a conflict.
