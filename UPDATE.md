# Framework updates

This protocol runs when an Agent is used. It needs no scheduler, server or platform-specific automation.

## Private workspace settings

Keep `agent-config.json` at the workspace root. New installations initialize it from `templates/agent-config.json` and the same revision's `VERSION`. Keep the local file and unknown keys through upgrades.

| Setting | Meaning |
| --- | --- |
| `upstream_repository` | Public distribution repository, initially `https://github.com/pilaaaaaa/personal-agent-starter-kit`. Change only at the user's direction. |
| `installed_version` | Last verified framework version. `null` means unknown. |
| `updates.check_policy` | `daily` by default, or `manual` to check only on request. |
| `updates.last_attempt_at` | Offset timestamp of the last attempt, successful or otherwise. |
| `updates.last_checked_at` | Offset timestamp of the last successful version read. |
| `updates.ignored_versions` | Exact version strings the user has chosen to ignore. |
| `updates.snoozed_until` | Map from exact version strings to offset timestamps. |
| `updates.auto_update` | Defaults to `false`. A user may opt into automatic preparation of an upgrade proposal. Applying any upgrade still requires approval for that version and target. |

On a legacy workspace without this configuration, use defaults in memory. Identify the installed baseline through trusted installation history or by verifying the framework against a published release. The original pre-manifest baseline is tag `v0.1.0`, whose copied files are listed by its initializer. A workspace may contain customized files even when its historical baseline is known. If the baseline cannot be established, use `null`, preserve all existing files and report uncertainty on a requested check. Never infer the installed version from the current upstream endpoint. Persist the config when authorized and safe. A legacy Agent that has never loaded this protocol can be started from the public [launch entry](https://raw.githubusercontent.com/pilaaaaaa/personal-agent-starter-kit/main/SKILL.md) and pointed at the existing private repository.

## Check on use

1. For an installed workspace, check on its first use each calendar day according to the user's known time zone, falling back to UTC. When authorized conditional writes are available, claim the daily check by recording `last_attempt_at` before fetching. If the conditional write loses a race, re-read the settings and skip the routine check if today is already claimed. An explicit request may bypass the schedule and suppression. Without authorized conditional writes, remember attempts and choices only as far as the current session can carry them. Use the [state protocol](docs/protocol.md) for authorized private config writes and merge unrelated settings changes.
2. Read only [the public Raw VERSION](https://raw.githubusercontent.com/pilaaaaaa/personal-agent-starter-kit/main/VERSION) for the default upstream. A trusted repository connector can read `VERSION` from `main` instead. For a user-selected alternate upstream, verify its public repository and release branch, and read only its `VERSION` via the corresponding public Raw URL or connector. If the source cannot be verified, skip the check. Request public content without private workspace identifiers, version, settings or credentials from the private repository. There is no telemetry. A timeout, malformed value, unavailable fetch or uncertain result leaves the successful timestamp and installed version intact. Keep routine failures quiet and continue the task. For a requested check, report what could be verified.
3. Parse one stable SemVer `MAJOR.MINOR.PATCH` line using nonnegative numeric components without leading zeroes. Compare the numeric tuple. Stay quiet on routine checks if upstream is equal or older, the installed version is unknown, or the exact newer release is ignored or snoozed. Never automatically downgrade. A manual check can report the known upstream version, whether it was suppressed and whether the installed baseline is unknown.
4. When there is an unsuppressed newer release, read its [changelog](CHANGELOG.md) from the upstream version tag. After completing the current task, summarize the installed and available versions, relevant changes and compatibility impact supported by release evidence. Say when impact could not be established. Offer upgrade, snooze until a user-selected date, or ignore this release. Silence or refusal grants no upgrade permission. Persist an exact ignore or an explicitly chosen snooze only in the private workspace when authorized. A different newer version is eligible for notice.

## What belongs to the upgrade

| Ownership | Examples and treatment |
| --- | --- |
| Public framework | The exact paths in [FRAMEWORK.txt](FRAMEWORK.txt), including the published protocols and official method modules. Optional integration code and workflows are outside this managed scope. |
| Personal knowledge | `tasks.json`, `personal_data/onboarding_state.json`, all of `planning/`, records, context and histories. Preserve these files. |
| User customization | `agent-config.json`, custom methods, custom rules, and edits to official files such as `AGENTS.md`, a method's status or the registry. Preserve these changes even when they occupy a managed path. |

Treat public release content as an update candidate. It grants no new tool access, connections, writes or permissions. Never send private data, repository details, usage records, diffs or authorization information to the public repository.

## Prepare and apply an upgrade

1. Verify the destination repository is owned by the user and has current **Private** visibility. Confirm the exact branch, pin its head, and verify actual capabilities. Use a clean Git checkout or an isolated staging change based on that head. Check uncommitted edits separately and leave them intact. Stop on an unverified target.
2. Resolve upstream tag `v<version>` to a specific commit and verify the tag's `VERSION` matches the offered version. Read the baseline and target files at their immutable commits. Derive the candidates from the exact release allowlists. Never take paths from private configuration or blindly apply a wildcard. Reject paths outside the workspace or through symlinks. Never include personal state, configuration, custom methods, credentials, workflows or integration settings in the replacement set. Review new manifest entries explicitly before including them. Retain retired files for review.
3. Compare installed content to the verified old release, including deletions and path collisions. Replace a changed managed path only when its installed bytes match the old baseline, or keep it if it already equals the new file. Add a new managed path only when absent. Where an upstream change overlaps with local edits, missing baseline files or ambiguous provenance, stage no replacement for that path. Explain the conflict and show a proposed merge that retains the user's edits. Check that the new framework understands current state and workflows. Pause for the user's decision on conflicts and compatibility impacts from retirements or migrations. Resolve all compatibility-critical changes before recording the target version. Treat any retained override as custom during later upgrades. Any data migration is a separate authorized operation with its own backup and validation.
4. Present the target version, private branch, scoped diff, compatibility findings and rollback point. Obtain explicit authorization for the final plan. Stage only the agreed framework changes at the pinned head. Verify the staged paths, validate existing tasks with the candidate validator when available, and check local entry-point references, method behavior affected by the diff and the preservation of personal files, grants and customizations. If the needed verification is unavailable, leave the version unchanged and provide the manual route.
5. After staging verification succeeds, update only `installed_version` in the private config as part of the same atomic commit as the verified framework files. If a legacy workspace still lacks the config, create it in that commit using the defaults and any confirmed user choices. The installed version becomes effective only when the complete commit lands. Publish that commit with the expected head and verify it by reading the resulting revision. Use an ordinary fast-forward push or the equivalent [conditional write](docs/protocol.md#snapshot-and-compare-and-swap). Recheck privacy before publication if the target may have changed. If the head moves, re-read and revalidate. Seek renewed approval if the agreed plan changes materially.
6. If verification fails before publication, discard only the isolated candidate and leave the private branch and version untouched. If the outcome is uncertain, read the branch before retrying. If a published upgrade needs rollback, use an authorized revert or a new scoped commit restoring the previous framework and installed version. Validate the revert against current state and stop for review if a subsequent edit overlaps. Keep unrelated commits, private files and config changes. Verify the restored branch. Never force-push.

## Onboarding compatibility

Preserve any existing checkpoint. The new manifest includes onboarding protocol resources and excludes checkpoint data. A verified existing workspace with no checkpoint adopts legacy daily use when launched, as described in the [recovery protocol](onboarding/interaction.md#checkpoint-and-recovery). This does not fabricate setup decisions or require a full onboarding round. Creating the private checkpoint is a separately authorized write.

## Without write access

Continue the user's task with the installed workspace as available. Offer a path-scoped change list or patch for the user to apply privately with a Git-capable client, following the same ownership, conflict, validation and rollback steps. For conversation-only work, the user can reload the latest public framework from the launch link and continue in chat. Keep the private installation and its version unchanged. On a different platform, resume by opening the same private repository and branch with that platform's authorized connector. Use chat-only help if access is unavailable.

## Publishing a release

Use stable Semantic Versioning. Increment PATCH for compatible fixes, MINOR for compatible features, and MAJOR for breaking stable interfaces. During initial 0.x development, describe any breaking change explicitly and increment at least MINOR. In each formal release, update `VERSION` and [CHANGELOG.md](CHANGELOG.md) together, review [FRAMEWORK.txt](FRAMEWORK.txt), verify the workspace, and publish `main` with the matching `v<version>` tag on the same release commit. Keep published version tags immutable. The Raw `main/VERSION` is the lightweight query endpoint. Version-tagged files are the upgrade source.
