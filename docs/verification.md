# Verify the kit

Use Python 3.9+ and Git for the offline checks.

```sh
python3 scripts/validate_tasks.py templates/tasks.json
python3 scripts/sync-google-calendar.py --tasks templates/tasks.json --validate-only
python3 -m pip install -r tests/requirements.txt
python3 -m unittest discover -s tests -v
bash -n scripts/install-sync.sh
```

On macOS with Swift and the EventKit SDK:

```sh
pa_build="$(mktemp -d)"
swiftc -parse-as-library -D SYNC_TEST -module-cache-path "$pa_build/modules" sync-reminders.swift tests/SyncRules.swift -o "$pa_build/rules"
"$pa_build/rules"
```

The offline harness exercises empty initialization, task lifecycle, a dependency cycle, two Git writers, calendar ownership, retries and deletion guards. The Swift harness covers snapshot, schedule, completion and deletion rules. Use the fictional conversation in [the example](../examples/scenarios.md) to exercise the planning protocol with an Agent. Check the updated plan and which knowledge files changed. The older planning examples are daily-use scenarios. Use the [onboarding example](../onboarding/examples.md) and [conversation fixtures](../tests/fixtures/onboarding.json) for the current launch flow. Run provider smoke tests in isolated targets as described in [the connector guide](../integrations/README.md).

## Before publication

- Inventory all publishable files including hidden files and Git metadata. Check identifiers, contact details, personal content, secret-shaped strings, private paths and links. Confirm templates are blank and fixtures are fictional.
- Confirm ownership, third-party permissions and copyright notices for all published material. Review dependencies and actions before enabling them.
- Record what was tested offline and which integrations still need live checks.

## Launch and update scenario review

These are protocol simulations for the Agent, with fictional state and tool outcomes. They are separate from the executable workspace tests. Use them to repeat the review on any platform.

| Scenario | Expected observable result |
| --- | --- |
| Only the public Raw launch link | Read the entry, offer a short welcome in the available language and ask for one real task. An explicit summary request instead receives a summary. |
| No connected tools | Start in chat once the entry is available. Give a useful plan without provisioning storage, a calendar or reminders. If link reading is also unavailable, request the file when needed. |
| Installed 0.2.0 and upstream 0.2.0 | Perform at most one routine check that day when the attempt can be stored, with no version notice or upgrade. |
| Installed 0.2.0 and upstream 0.3.0 | After the actual task, report both versions and verified release changes, then offer upgrade, snooze or ignore. |
| Refusal, snooze or ignore | Make no framework changes. An explicit snooze suppresses that version until its chosen date. Ignore suppresses that exact version on later routine checks. |
| Upgrade approved, official method modified locally | Compare with the pinned baseline. Keep the local edit, explain the overlap and get agreement on the final plan before applying. |
| No network or no conditional GitHub writes | Continue the task. For a requested check report any unverified result. Offer private manual application or chat-only reload with the installed version unchanged. |
| Switch Agents | Open the same private branch with the new platform's authorized connector. Read state and settings from that branch, and retain the last attempt and user choices. |
| No configuration in the old installation | Use the defaults in memory. Record a baseline only with provenance and verify the destination before persisting it. |
| Failure during upgrade | A failed candidate leaves the branch untouched. For a failed published upgrade, verify the head and apply an authorized scoped revert restoring the prior version while retaining unrelated changes. |

The interaction protocol has no universal control over whether a chat client fetches a link or activates from it. For live compatibility, run the first two scenarios in a fresh chat on each target platform and record whether it read the actual file and started onboarding. Use a disposable private repository for live write, platform-switch and rollback checks.


## Manual cross-platform matrix

Run each fixture in a fresh chat or from the exact checkpoint it specifies. Use synthetic tasks, a disposable authorized private repository for connector cases, and observer-only testing where tools are unavailable. Start link-only tests with the bare canonical Raw URL. Separately try the explicit launch sentence and the attached entry file. Observe the final response, question count, tools called and one-turn stop. For write and resume tests, read back the exact branch and checkpoint. Record platform, model, date, URL fetch evidence, tools, actual response, result and any divergence.

| Host | Bare URL / explicit / attachment | Guarded turns, exit and fallback | Private read/resume and conditional write | Provider side effects |
| --- | --- | --- | --- | --- |
| ChatGPT | Unverified | Unverified | Unverified | Unverified |
| Claude | Unverified | Unverified | Unverified | Unverified |
| Gemini | Unverified | Unverified | Unverified | Unverified |
| 豆包 | Unverified | Unverified | Unverified | Unverified |

Offline fixtures use authored user messages, interpreted guard events and expected observations. They validate the protocol harness without calling model APIs. Missing URL access should lead to an attachment request where possible. Missing supporting files after entry retrieval should still allow chat onboarding.


For the voluntary Star path, observe the optional link before the welcome question. Try a single turn reaching two milestones, explicit decline while a setup question is pending, and a fresh Agent resuming the same private branch. Verify that no Star action or lookup is attempted, no reply is required, and the pending setup question and stage survive. The installed Python evaluator test exercises deduplication, milestone priority and both terminal replies with synthetic state. Host phrasing and conditional persistence belong to the manual matrix.
