# Name and connect the user's Personal Agent

## Naming after first value

On the first relevant turn after S2 delivers its result, before asking the storage question, proactively invite the user to name their Agent. Ask only one core question and end the turn. Offer possible names when asked, and record only the user's confirmed choice. A user can explicitly defer or decline while continuing in chat. Resume a legacy unresolved question first, then insert naming before the next new setup decision. Use an already confirmed name without asking again.

Keep the name in chat until the user authorizes storage. In a private workspace write `agent_display_name` in `agent-config.json` using the ordinary conditional write and readback. Rename by changing the display configuration and, if supported, the plugin's display name. Keep the technical service and connector identifiers, repository and branch, paths, receipt store and provider namespaces stable.

## Proactive plugin setup

After the storage choice and before configuring external integrations, actively invite the user to create a dedicated private plugin. Also run this check whenever the user explicitly asks to sync Plan, Actual or tasks to an external app, including in daily use or legacy adoption. Preserve any unresolved legacy question while the prerequisite is addressed. Complete the user's unrelated work in the same conversation under the existing turn rules.

Check what the current host actually exposes for private plugin creation, connection and installation. Use the confirmed name for the display label and a stable technical identifier. Reuse the user's own private workspace, service, credentials and grants. With necessary authorization use available tools to set up, deploy and connect it. Otherwise guide the next specific step using the host's current interface and the [deployment guide](../docs/harness.md). Choose a supported local or remote route based on the user's devices. When a capability is unavailable, offer the connector or chat fallback and say which capability is missing.

Track plugin status as `unasked`, `selected`, `failed`, `unavailable`, `declined`, `deferred` or `ready`. Failure and unavailable require a retry, an explicit decline or defer, or confirmation of a supported fallback. An unanswered offer gives no approval. Decline and defer allow fallback, and do not count as ready. On an explicit later sync request or a request to retry setup, revisit the prerequisite in the context of the requested outcome instead of replaying generic introductions. A user can authorize existing connector integration separately even if they decline the plugin.

Mark `ready` only after the current host discovers the four tools and gets successful `get_protocol` and `read_state` responses from the intended private workspace. Record write verification separately. Use synthetic isolated data and scoped authorization for the write and external readbacks. Distinguish Git publication, provider verification and device propagation.

## Host Personalization

Proactively offer to set host-level instructions after the setup outcome is known, whether the user uses the plugin, direct connector or chat. Build instructions from the user's confirmed values and actual grants. Ask before saving and verify by reading the setting back if the host supports it. Otherwise give the user the current path or supported equivalent and a ready-to-copy instruction. Confirm a fresh conversation recognizes the intent and calls the configured tools without a manual mention where supported.

Suggested text to render using confirmed values:

> You are {confirmed Agent name}. For Personal Agent requests use {verified connector or tool identifier} and the authorized private workspace {repository and branch}. Read the protocol when opening a new relevant conversation. Detect every relevant independent intent in the user's message and submit a single ordered batch where the supported tool and {authorized scope} allow. Keep Plan, Actual and task state distinct. Verify explicit writes through execution receipts and report provider and device verification separately. If tools are unavailable, keep helping in chat and explain any material execution limitation.

## Backward-compatible checkpoint

Use the existing S3 and S4 states as entry points for the new prerequisites. Keep the v1 primary checkpoint and its unresolved question intact. Store only new gate metadata in `personal_data/onboarding_agent_setup.json`, using [its schema](agent-setup.schema.json) and the usual authorized revision-aware write and readback. During a naming, plugin or Personalization question record its fixed key in this sidecar before asking and ending the turn. A sidecar question, when present, is the effective interrupt. Apply the main checkpoint's pause, resume and exit controls to it. Keep staged changes in conversation until storage is authorized.

Treat a missing sidecar as unasked. Old completed users continue daily work without reopening their prior setup. Explicit sync intent still runs the plugin prerequisite. An unfinished S3 or S4 resumes the original pending question and then applies new prerequisites before asking a new setup question or executing a newly requested integration. Preserve verified integration and storage status. Upgrading or rolling back framework files does not rewrite either checkpoint. The old host can continue reading the v1 checkpoint.


A Python-capable host can run the [setup gate evaluator](../scripts/onboarding_setup.py) with JSON from the current checkpoint and sidecar on stdin as `checkpoint` and `setup`. Pass `--external-sync` for a current explicit provider request and `--integrations-resolved` after evaluating the existing S4 scope. Supply `plugin_reviewed_for_request: true` only for an explicit fallback decision on the current request. The evaluator returns the next question key; use current tool evidence to decide the exact action or wording.
