# Workspace and integration setup

Use these steps when the relevant state in the [onboarding workflow](../onboarding/workflow.json) is active. Follow the [interaction protocol](../onboarding/interaction.md) before advancing or offering setup.

## Prepare a private workspace

- Check whether the Agent can read files or GitHub, make revision-aware writes and verify repository visibility. Inspect an existing planner only when relevant and within the user's authorization. Confirm import scope before moving records.
- Reuse the user's chosen workspace if it already contains `tasks.json`, `planning/` and `methods/`, and the exact owner, repository and branch can be resolved. For a new workspace, run `python3 scripts/init_workspace.py PATH` from the starter kit, or copy the paths in [FRAMEWORK.txt](../FRAMEWORK.txt) with file tools, then copy `templates/tasks.json` to `tasks.json`, `templates/planning/` to `planning/`, and `templates/agent-config.json` to `agent-config.json`. Set its `installed_version` from the same source revision's `VERSION` after verifying the copied framework. Copy `templates/onboarding_state.json` to `personal_data/onboarding_state.json` only for a fresh workspace. When provisioning mid-onboarding, write the current validated checkpoint in place of the blank template and set the current UTC timestamp. For an empty workspace create `personal_data/journal.json` with `{"schema_version":1,"operations":[]}`. Check the empty ledger with `python3 scripts/validate_tasks.py` when available. Assure the user that preferences can be added in conversation as needed.
- With authorization, create or connect the user's private repository. Before writing personal knowledge, verify **Private**, ownership by the user and the exact target branch from current repository metadata. Names, URLs and prior assumptions are insufficient. Verify the initial snapshot if pushed. Follow the [state protocol](protocol.md) for edits and any pre-existing data.
- If the user postpones GitHub, honor the deferred decision. When access fails, record failure and offer chat while the user decides whether to retry or explicitly choose an alternative. An explicitly authorized private local workspace can also hold state under the base protocol. Confirm GitHub privacy before sending any local records there. Keep the option to finish setup later.

## Name and connect the user's Agent

Follow the [setup gates](../onboarding/agent-setup.md) to proactively invite naming after first value, guide private plugin setup and offer host Personalization. An authorized connector fallback can reuse existing workspace permissions.

## Add only the chosen connection

| Choice | Next action |
| --- | --- |
| Conversation only | The user can continue working in conversation. Resolve optional integration scope separately. |
| Reminders | Identify the service the user uses. Check for its actual capabilities and offer the applicable route. For the bundled Apple adapter use the [connector guide](../integrations/README.md#apple-reminders). |
| Calendar | Ask which service and whether the user needs availability or plan projection only if it affects setup. Use authorized availability reads where supported. For projection to Google Calendar use the [connector guide](../integrations/README.md#google-calendar). |
| Both | If no priority is clear, start with the first capability needed by the current task. Finish or pause that path before presenting steps for the other. |

Automate authorized setup steps through available tools. For manual steps, name the service, exact action and what indicates success. Check credentials and permissions by capability. Never ask users to paste secrets into chat. Before enabling a live connector, preview its actual effect, obtain scoped authorization and verify with an isolated fictional record and readback. Respect the connector guide's separate deletion controls. Let users continue while a permission, install or smoke test is pending. Report each selected service as ready or pending with the current blocker. Test harnesses and sample conversations are for maintainers, and users can start with their own requests.
