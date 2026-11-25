# First use and gradual setup

This guide is for the Agent. Consult only the relevant path and surface the next action the user needs.

## Welcome and routing

Follow the launch routing and language choice in [SKILL.md](../SKILL.md). Start with a real task or decision. Offer persistence, reminders or calendar setup only when relevant or requested, and accept chat-only use at any point. Existing users can identify their private repository and branch to resume on another platform. Check the new platform's read and write capabilities and honor the existing workspace rules. Connector access must be available and authorized on the new platform.

## Prepare a private workspace

- Check whether the Agent can read files or GitHub, make revision-aware writes and verify repository visibility. Inspect an existing planner only when relevant and within the user's authorization. Confirm import scope before moving records.
- Reuse the user's chosen workspace if it already contains `tasks.json`, `planning/` and `methods/`, and the exact owner, repository and branch can be resolved. For a new workspace, run `python3 scripts/init_workspace.py PATH` from the starter kit, or copy the paths in [FRAMEWORK.txt](../FRAMEWORK.txt) with file tools, then copy `templates/tasks.json` to `tasks.json`, `templates/planning/` to `planning/`, and `templates/agent-config.json` to `agent-config.json`. Set its `installed_version` from the same source revision's `VERSION` after verifying the copied framework. Check the empty ledger with `python3 scripts/validate_tasks.py` when available. Assure the user that preferences can be added in conversation as needed.
- With authorization, create or connect the user's private repository. Before writing personal knowledge, verify **Private**, ownership by the user and the exact target branch from current repository metadata. Names, URLs and prior assumptions are insufficient. Verify the initial snapshot if pushed. Follow the [state protocol](protocol.md) for edits and any pre-existing data.
- If the user postpones GitHub, or you cannot establish private access, offer session-only planning. Report that updates are unsaved. An explicitly authorized private local workspace can also hold state under the base protocol. Confirm GitHub privacy before sending any local records there. Keep the option to finish setup later.

## Add only the chosen connection

| Choice | Next action |
| --- | --- |
| Conversation only | Proceed to the user's first real request. GitHub is the only connection needed for durable shared state. |
| Reminders | Identify the service the user uses. Check for its actual capabilities and offer the applicable route. For the bundled Apple adapter use the [connector guide](../integrations/README.md#apple-reminders). |
| Calendar | Ask which service and whether they want Plan, Actual, both or local-only. Use authorized availability reads where supported. The [calendar guide](calendars.md) covers both tracks and Google setup. |
| Both | If no priority is clear, start with the first capability needed by the current task. Finish or pause that path before presenting steps for the other. |

Automate authorized setup steps through available tools. For manual steps, name the service, exact action and what indicates success. Check credentials and permissions by capability. Never ask users to paste secrets into chat. Before enabling a live connector, preview its actual effect, obtain scoped authorization and verify with an isolated fictional record and readback. Respect the connector guide's separate deletion controls. Let users continue while a permission, install or smoke test is pending. Report each selected service as ready or pending with the current blocker. Test harnesses and sample conversations are for maintainers, and users can start with their own requests.

## Learn from actual work

- Use the method registry internally. After delivering a helpful result, a single optional question can reference what happened, such as “I moved the file export into your waiting time and kept a quiet block for proofreading. Want to see how I chose that order?” If asked, explain the methods' use, limits and how to edit or replace them in the current case. If the user declines, acknowledge and, where welcome, mention that the kit ships with 12 composable methods they can ask about or change later. Honor a standing preference against explanations. A later explicit question can reopen the topic.
- Ask about bedtime when it would change an evening plan, about competing attention demands when it changes whether tasks can overlap, or about permanence when a correction may change future plans. If the user declines, proceed with a useful conditional plan or a stated assumption. Mention once when relevant that they can tell you routines, work habits or principles in ordinary conversation. Store confirmed durable preferences, temporary constraints and one-time instructions under the [operating protocol](../AGENTS.md#learn-in-conversation).
- Surface features in context. A pending reply can become a tracked waiting item. A changed deadline can trigger a replan. Overlapping work can prompt a brief compatibility check. Requests for fewer confirmations can lead to scoped delegation, requests to switch providers to the connector guide, and dissatisfaction with a recommendation to a method change. Explain only the capability that helps, with a concrete next action.
