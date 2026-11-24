# Verify the kit

Use Python 3.9+ and Git for the offline checks.

```sh
python3 scripts/validate_tasks.py templates/tasks.json
python3 scripts/sync-google-calendar.py --tasks templates/tasks.json --validate-only
python3 -m unittest discover -s tests -v
bash -n scripts/install-sync.sh
```

On macOS with Swift and the EventKit SDK:

```sh
pa_build="$(mktemp -d)"
swiftc -parse-as-library -D SYNC_TEST -module-cache-path "$pa_build/modules" sync-reminders.swift tests/SyncRules.swift -o "$pa_build/rules"
"$pa_build/rules"
```

The offline harness exercises empty initialization, task lifecycle, a dependency cycle, two Git writers, calendar ownership, retries and deletion guards. The Swift harness covers snapshot, schedule, completion and deletion rules. Use the fictional conversation in [the example](../examples/scenarios.md) to exercise the planning protocol with an Agent. Check the updated plan and which knowledge files changed. The same file contains five onboarding walkthroughs covering quick use, method explanation, customization, withheld preferences and incremental connections. Run provider smoke tests in isolated targets as described in [the connector guide](../integrations/README.md).

## Before publication

- Inventory all publishable files including hidden files and Git metadata. Check identifiers, contact details, personal content, secret-shaped strings, private paths and links. Confirm templates are blank and fixtures are fictional.
- Confirm ownership, third-party permissions and copyright notices for all published material. Review dependencies and actions before enabling them.
- Record what was tested offline and which integrations still need live checks.
