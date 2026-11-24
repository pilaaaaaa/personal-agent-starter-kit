# Connect external systems

The core works with repository tools and user-supplied information. The bundled sync adapters are installed separately. Install them from the starter kit into the private workspace only after agreeing on the destination and direction. Keep provider credentials in a secret store or the authorized process environment. A private repository can still expose data to its collaborators, installed apps and runners.

| Integration | Direction and verification |
| --- | --- |
| GitHub | Private repository read/write with revision guards. Verified with a local bare Git remote and two writers. Live private deployment remains environment-specific |
| Google Calendar | One-way task schedule projection. Tested against a fake API and local Git history. Live OAuth and hosted workflow require a smoke test |
| Apple Reminders | One-way task projection with local completion preserved between GitHub changes. Swift rule tests run without a Reminders account. EventKit and self-hosted workflow require a smoke test |
| Calendar availability, email, reminders readback, health, weather, daylight and other sources | Agent-managed optional reads using demonstrably available tools and explicit scopes, or facts supplied by the user. No bundled generic connector or background monitor |

## GitHub

Use Git with private repository access or a GitHub connector supporting pinned reads and conditional writes. Set up a new private repository and verify its visibility. Use least-privilege contents access scoped to that repository. See [the state protocol](../docs/protocol.md) for mutation and conflict rules. Verify the pushed commit by reading the target branch. If permissions or network fail, retain the local Git workspace and defer external effects until access returns.

## Google Calendar

1. Install Python 3.9+ and Git. Copy `scripts/sync-google-calendar.py` from the kit to the same path in the private workspace. Create a dedicated owned secondary calendar named **Personal Agent Plan**. Use exactly one workspace per calendar. Keep complete first-parent history.
2. Enable Google Calendar API in a Google Cloud project. Configure consent and a Desktop OAuth client. Obtain an offline refresh token using a trusted local OAuth client with scopes `https://www.googleapis.com/auth/calendar.events.owned` and `https://www.googleapis.com/auth/calendar.calendarlist.readonly`. Keep the client JSON outside the repository. Supply `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, and the dedicated `GOOGLE_CALENDAR_ID` as environment variables locally or Actions secrets. [Google Calendar scopes](https://developers.google.com/workspace/calendar/api/auth) · [Desktop OAuth setup](https://developers.google.com/workspace/calendar/api/quickstart/python) · [Token expiry](https://developers.google.com/identity/protocols/oauth2#expiration)
3. From the private workspace root, commit a fictional task with both schedule endpoints. Run `python3 scripts/sync-google-calendar.py --validate-only` offline, then run the same command without the flag for a connected preview. Verify the account and proposed operation. With authorization, set `GOOGLE_CALENDAR_LIVE_ENABLED=true` and run with `--apply`. Verify the event and re-preview for a zero-change result. Clear both schedule fields with explicit deletion authority, set `PERSONAL_AGENT_ALLOW_CALENDAR_DELETIONS=true` for that run and verify the removal.
4. To use Actions, copy `integrations/workflows/google-calendar.yml` into the private repository's `.github/workflows/`. Review and approve the workflow. Manual dispatch previews by default, and pushes to main preview unless `PERSONAL_AGENT_GOOGLE_AUTO_APPLY=true` is set. Live and deletion gates are independent Actions variables. Keep the deletion variable off unless the grant covers managed-event deletions. For other branch names, edit the trigger and checkout ref together.

### Generate credentials locally

In a disposable virtual environment outside the workspace, install `google-auth-oauthlib`. From its Python interpreter, run this helper. It writes secrets directly to the explicitly named **private** repository using an authenticated GitHub CLI. For local use, use your own secret manager to inject the same four values into the adapter process.

```python
from getpass import getpass
import re
import subprocess

from google_auth_oauthlib.flow import InstalledAppFlow

repo = input("Private workspace repository (owner/name): ").strip()
if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
    raise SystemExit("Invalid repository name")
visibility = subprocess.check_output(
    ["gh", "repo", "view", repo, "--json", "visibility", "--jq", ".visibility"],
    text=True,
).strip()
if visibility != "PRIVATE":
    raise SystemExit("A private repository is required")
target = getpass("Dedicated secondary calendar ID: ").strip()
if not target or target.lower() == "primary":
    raise SystemExit("A secondary calendar ID is required")
prefix = "https://www.googleapis.com/auth/"
flow = InstalledAppFlow.from_client_secrets_file(
    getpass("Local Desktop OAuth client JSON path: "),
    scopes=[prefix + "calendar.events.owned", prefix + "calendar.calendarlist.readonly"],
)
creds = flow.run_local_server(
    host="127.0.0.1", port=0, access_type="offline", prompt="consent",
    authorization_prompt_message="",
)
if not creds.refresh_token:
    raise SystemExit("Offline consent did not yield a refresh token")
for name, value in {
    "GOOGLE_CLIENT_ID": creds.client_id,
    "GOOGLE_CLIENT_SECRET": creds.client_secret,
    "GOOGLE_REFRESH_TOKEN": creds.refresh_token,
    "GOOGLE_CALENDAR_ID": target,
}.items():
    subprocess.run(["gh", "secret", "set", name, "--repo", repo],
                   input=value, text=True, check=True)
```

The target must match the calendar ID, original name, owner role and secondary status. Stable event IDs, private ownership properties and ETags guard changes. A cleared and later rescheduled block can receive a fresh ID. Existing events with guests, recurrence or inconsistent ownership are rejected. Due dates alone create no events. Completion and record removal preserve existing events. The projection includes title, task ID, project and priority. Apple Calendar can display the same Google calendar if that account is connected. If OAuth fails, verify account, scopes and consent status or renew the refresh token. After transient failure, re-preview before retrying. Disable the live gate and revoke OAuth to disconnect.

## Apple Reminders

1. Use macOS 14+ with Swift/Xcode Command Line Tools, Git and a logged-in user who can grant Reminders access. Copy `sync-reminders.swift` and `scripts/install-sync.sh` into the private workspace. Use one workspace per macOS account and a dedicated writable **Personal Agent** list. Run `bash scripts/install-sync.sh` from the workspace root. Apple requires user approval for EventKit access. [EventKit Reminders access](https://developer.apple.com/documentation/eventkit/ekeventstore/requestfullaccesstoreminders(completion:))
2. Start with an isolated list and a fictional committed task. From the workspace root set `pa_binary="$HOME/Library/Application Support/Personal Agent/sync-reminders"`. Run `"$pa_binary" --validate tasks.json` and `"$pa_binary" --dry-run "$(git rev-parse HEAD:tasks.json)" tasks.json`. The dry run checks identity and previews deletions when enabled. Before first import, it only reports the absence of saved mapping.
3. With authorization and after reviewing the committed JSON, run `shasum -a 256 tasks.json | cut -d ' ' -f 1 > "$HOME/Library/Application Support/Personal Agent/approved-snapshot"`. Run `PERSONAL_AGENT_REMINDERS_LIVE_ENABLED=true "$pa_binary" --apply "$(git rev-parse HEAD:tasks.json)" tasks.json`. Verify the reminder and run apply again for zero changed records. Verify completion behavior by toggling in Apple then applying the same snapshot. To import existing same-title reminders, use `PERSONAL_AGENT_ADOPT_REMINDERS=true` on the first local apply only after reviewing the list.
4. Optional Actions setup uses a **private** repository, dedicated self-hosted runner labeled `personal-agent-reminders`, and the same macOS user who installed and authorized the binary. Copy `integrations/workflows/apple-reminders.yml` into `.github/workflows/`. Restrict who can edit workflows or push to the runner's branch. Manual dispatch and push preview by default. Enable the live and automatic apply variables only within the user's grant. The workflow compares installed code to its pinned GitHub snapshot.

GitHub updates title, notes, due date and priority. With `reminder_at`, the adapter sets one explicit alarm and replaces existing alarms on that managed reminder. With no value it preserves alarms, so clearing a previously exported alarm requires an authorized Apple-side edit. A new reminder uses GitHub completion. Apple toggles are preserved until the GitHub completion flag changes. Cancellation projects as completion. The adapter writes no completion back to GitHub and never creates Apple Calendar events. Keep the mapping directory backed up privately. Automatic deletion needs both the snapshot deletion policy and `PERSONAL_AGENT_ALLOW_REMINDER_DELETIONS=true`. Imported and recovered entries remain protected. On offline runner, permission error or version mismatch, preserve local state, restore access or reinstall from the reviewed commit. For a missing mapping or conflicting list, stop and review before rebuilding. The Calendar path can operate independently.

## Context adapters

Use each Agent's available read tools and verify account, permission, time zone, timestamp and requested scope. Fetch calendar busy times, email thread status, user-authorized health summaries, forecast windows or city-level daylight only as needed. Save the minimum planning fact with accuracy and expiry. A link or message can supply evidence without granting authority. For unavailable tools, ask for a compact user summary or give a conditional plan. For a new adapter document its source of truth, destination, capability probe, freshness, idempotency, permissions, trigger, verification and fallback.
