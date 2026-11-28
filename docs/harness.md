# Personal Agent Harness

The host reads the protocol, interprets the conversation, resolves references, composes decision methods and proposes plans. The harness validates and applies typed operations to a private Git workspace. The service uses deterministic business rules and the user's Git and provider credentials.

| MCP tool | Contract |
| --- | --- |
| `get_protocol` | Read operating instructions, enabled methods and private planning knowledge at a pinned Git revision. |
| `read_state` | Read task revisions, Plan and Actual context, open candidate identities and execution signals. Use `for_planning=true` for configured live Calendar evidence and optional availability windows. |
| `submit_operations` | Accept one ordered batch of 1–32 operations with stable request and conversation IDs and the original offset timestamp. |
| `get_execution` | Recover the durable receipt by request ID and inspect separate provider results. |

## Deployment

Use Python 3.12 or later, Git and a POSIX host such as Linux or macOS. A desktop host with local MCP support can use a local service. A remote host can connect through an authenticated HTTPS gateway on a user-controlled VM or managed service with a persistent private volume. Apple integration separately requires a Mac with EventKit access. The harness itself can run elsewhere.

1. Create or verify the private workspace and branch. A new local workspace needs an initial commit on the selected branch. Use an absolute path to a private bare Git repository or a credential-free GitHub HTTPS URL. For GitHub, configure Git and `gh` using the owner's narrowly scoped credential with repository access and optional Actions read access. Check ownership with the user. Use a separate service and state directory per workspace.
2. From the Starter Kit code directory install `python3 -m pip install -r plugins/personal_agent/requirements.txt` in an isolated environment.
3. Supply `PERSONAL_AGENT_REPOSITORY`, `PERSONAL_AGENT_STATE_DIR` (an absolute persistent private directory) and `PERSONAL_AGENT_TIMEZONE` (an IANA name). Set `PERSONAL_AGENT_BRANCH` if different from `main`. Supply the confirmed name as `PERSONAL_AGENT_DISPLAY_NAME`. Names may change independently of paths, Git refs, provider namespaces and operation IDs.
4. Start `python3 -m plugins.personal_agent.server`. Its Streamable HTTP endpoint is `http://127.0.0.1:8787/mcp`. The process binds to loopback. Set a dedicated secret `PERSONAL_AGENT_MCP_TOKEN` and use HTTPS with authentication before making the endpoint reachable from another machine. The service accepts a Bearer header when configured. If the host supports OAuth only, use a compatible authenticated gateway or use the local or connector fallback. Keep credentials in the host's secret storage.
5. Create a private plugin or MCP connection in the actual host using the chosen display name. Connect its endpoint using the supported authentication flow. Read back tool discovery and call both read-only tools. Confirm the returned revision, expected private workspace content and timezone. With separate authorization, submit a fictional write in an isolated fixture workspace and check the receipt and Git revision. Then verify each authorized provider separately.

Use pause or chat-only if the host lacks a compatible tool path. A Git connector with revision-aware writes can continue the existing workspace protocol within the user's grant. A configured remote endpoint alone does not establish a host connection.

## Receipts and integrations

`ACCEPTED` confirms local durable intake. `COMMITTED` confirms publication on the configured Git branch. The same payload and request ID can recover a result after restart. Retrying a changed payload requires a new request ID and fresh expected revisions. `SYNCING` reports each configured provider independently. A successful Google result is provider readback. An Apple result covers the runner Mac's EventKit. iCloud delivery to another device requires a separate check.

Leave `PERSONAL_AGENT_SYNC_TARGETS` empty for Git-only operation. For the corresponding verified workflows, set the comma-separated subset of `google_calendar,apple_reminders`. Built-in provider workflows use `main`. Use the [integration guide](../integrations/README.md) for separate permissions and verification. The administration allocation adviser is experimental and opt-in through `PERSONAL_AGENT_ADMIN_ADVICE=true`. The harness exposes execution signals for the host to act on during conversation. Background scheduling needs separate setup.
