# Starter Kit 0.5 migration

The public 0.4 baseline provided chat-led onboarding, a private JSON task ledger, the method library, a task calendar adapter and a one-way Apple workflow. The private source baseline added a structured MCP adapter with a durable Git runtime, Journal-backed execution and provider reconciliation. Public 0.5 keeps the existing onboarding and integrations available alongside the new route.

| Source area | Public disposition |
| --- | --- |
| MCP server and durable runtime | Port the four-tool adapter and isolated checkout, with configurable name, repo, branch, timezone and enabled targets. Include local bearer authentication and exact Git readback. |
| Harness, capture, interaction, Journal store and inbox | Port structured batches, occurrence identity, conservative estimates, deduplication, correction, cross-day intervals and transactional updates. The host identifies all independent intents. |
| Task updates, lifecycle, intake and intents | Port open-task editing, stable IDs, rollover, waiting states, commitments and partial progress. Preserve existing task JSON and use a separate Journal file. |
| Execution, timeline and supervision | Port candidate conservation, independent plans and Actual, read-only proposals, and evidence-based signals. The host suggests actions and plans. |
| Sources, methods and planning knowledge | Use the existing public methods or a compatible private method catalog. Port opt-in live Calendar evidence and explicit preference capture. |
| Administration and rolling forecasts | Include deterministic adviser code with explicit opt-in. Default conversational execution exposes regular supervision signals. |
| Adaptive learning | Keep explicit preference and decision-log recording. Exclude the shadow evaluation and automatic policy experiments. |
| Calendar adapters and workflows | Keep the legacy task adapter. Add a private, separately enabled two-track Journal workflow and provider receipts. |
| Apple EventKit | Keep the existing bridge. Bundle separate opt-in two-way reconciliation with conflict and readback checks. |
| Private service deployment and Personalization | Exclude production credentials, endpoint and host instructions. Provide a configurable deployment and instructions generated from each user's own grants. |
| Private knowledge and history | Exclude tasks, Journal, operation requests and receipts, personal planning files, workflows tied to the original account, and Git history. |

The private Actual path had prior live voice, Git and Google acceptance. Public tests exercise the selected code with fictional data. Provider credentials, installed host behavior and cross-device Apple propagation require new acceptance in the user's own deployment.
