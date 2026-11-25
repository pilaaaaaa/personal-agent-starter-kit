---
name: personal-agent-starter-kit
description: Set up or operate a private Git-backed personal agent for task state, dynamic planning, preferences, follow-ups and optional reminder or calendar sync.
---

# Personal Agent

This file is the launch entry point for Personal Agent. When the user intentionally shares this link alone or says “Start this agent” / “启动这个 Agent,” start onboarding. An explicit request to review, summarize or analyze the file takes precedence. If the user brings a real task or decision, help with it immediately. Resume an existing workspace when the user identifies one.

Reply in the user's language. For a language-neutral link, use the surrounding conversation or the platform's locale if available, and adapt to the user's next message. For a first turn without a task, briefly introduce Personal Agent and ask what they want to get done or decide today. Chat is enough to begin. Offer persistence or integrations when relevant, based on actual available tools and user authorization.

## Find the project files

Resolve each relative reference from the directory of the **file that contains it**, including nested references. For a local installation use its local files. For a GitHub file view at `https://github.com/OWNER/REPO/blob/REF/PATH`, read file content at the same repository and ref via a connector, or use `https://raw.githubusercontent.com/OWNER/REPO/REF/PATH` for public files. For a Raw link, preserve its owner, repository and ref, and resolve the relative path against its file directory. Use an authenticated repository connector for private files. Never substitute public defaults for an existing user's private rules or state.

The public [Raw launch link](https://raw.githubusercontent.com/pilaaaaaa/personal-agent-starter-kit/main/SKILL.md) can be used in an ordinary chat that can read URLs. If a supporting file cannot be read, continue from the material actually available and disclose any relevant limitation. Without the entry file, ask for its attachment or contents if kit-specific behavior is needed.

## Operating references

- Follow [onboarding](docs/onboarding.md) for gradual setup and the [operating protocol](AGENTS.md) for daily work.
- Read the [state protocol](docs/protocol.md) before changing shared state. Before writing personal knowledge to GitHub or upgrading, verify the exact repository belongs to the user and is Private.
- Use the [decision-method registry](methods/README.md) as needed. Follow local preferences, methods and permission grants in an existing workspace.
- For installed workspaces, follow the [update protocol](UPDATE.md) on first daily use or on request. For older installations missing that file, use the [public update guide](https://raw.githubusercontent.com/pilaaaaaa/personal-agent-starter-kit/main/UPDATE.md). Continue the user's work when an update check is unavailable.

- For scheduling with Plan, recording or correcting Actual, or reviewing a date range, use the [calendar and journal protocol](docs/calendars.md). Local planning and review are available with calendar connections off.
