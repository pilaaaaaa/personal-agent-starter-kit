#!/bin/bash
# Run only in the disposable Actions checkout.
set -euo pipefail
umask 077

support="$HOME/Library/Application Support/Personal Agent"
binary="$support/reconcile-reminders"
temp="$(mktemp -d)"
trap 'rm -rf "$temp"' EXIT
summary="${GITHUB_STEP_SUMMARY:-/dev/stdout}"

verify_binary() {
  local expected
  expected="$(git rev-parse HEAD:integrations/apple/reconcile-reminders.swift)"
  if [[ ! -x "$binary" || "$("$binary" --version)" != "$expected" ]]; then
    echo "::error::Install the reviewed Apple bridge for source blob $expected on the connected Mac."
    exit 1
  fi
  echo "Apple bridge source blob: $expected" >> "$summary"
}

git fetch origin refs/heads/main
git reset --hard FETCH_HEAD
verify_binary
if [[ "${VALIDATE_ONLY:-true}" == true ]]; then
  "$binary" --dry-run "$(git rev-parse HEAD:tasks.json)" tasks.json
  exit 0
fi

if [[ "${PERSONAL_AGENT_REMINDERS_LIVE_ENABLED:-false}" != true ]]; then
  echo "::error::Enable live writes only after authorized preview."
  exit 1
fi
git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
for attempt in 1 2 3 4 5; do
  if [[ "$attempt" != 1 ]]; then
    git fetch origin refs/heads/main
    git reset --hard FETCH_HEAD
    verify_binary
  fi
  "$binary" --observe "$(git rev-parse HEAD:tasks.json)" tasks.json > "$temp/observation"
  python3 -m personal_agent reminders-import < "$temp/observation" > "$temp/receipt"
  "$binary" --validate tasks.json
  git add -- tasks.json
  if ! git diff --cached --quiet; then
    git commit -m 'Reconcile managed Apple Reminders'
    if ! git push origin HEAD:refs/heads/main; then
      [[ "$attempt" != 5 ]] && continue
      exit 1
    fi
  fi
  git fetch origin refs/heads/main
  if [[ "$(git rev-parse HEAD)" != "$(git rev-parse FETCH_HEAD)" ]]; then
    [[ "$attempt" != 5 ]] && continue
    exit 1
  fi
  break
done

echo "GitHub snapshot verified: $(git rev-parse HEAD); tasks blob: $(git rev-parse HEAD:tasks.json)" >> "$summary"
python3 - "$temp/receipt" "$temp/observation" "$temp/apply" <<'PY' >> "$summary"
import json
from pathlib import Path
import sys

receipt, observation, output = map(Path, sys.argv[1:])
result = json.loads(receipt.read_text())
device = json.loads(observation.read_text())
device["excluded"] = sorted({row["id"] for row in result["conflicts"]})
output.write_text(json.dumps(device))
print("Apple → GitHub:", json.dumps({"changes": result["changes"], "conflicts": result["conflicts"],
                                     "lifecycle": result["lifecycle"]}, ensure_ascii=False))
PY

if "$binary" --apply "$(git rev-parse HEAD:tasks.json)" tasks.json "$temp/apply" > "$temp/eventkit" 2>&1; then
  tee -a "$summary" < "$temp/eventkit"
else
  tee -a "$summary" < "$temp/eventkit" >&2
  echo "EventKit projection incomplete; retry starts with a fresh observation." >> "$summary"
  exit 1
fi
git fetch origin refs/heads/main
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse FETCH_HEAD)" ]]; then
  echo "Main advanced during EventKit projection; the next reconciliation must verify the latest snapshot." >> "$summary"
  exit 1
fi
echo "EventKit readback is from the runner Mac. iCloud and other devices require separate confirmation." >> "$summary"
python3 -c 'import json, sys; sys.exit(bool(json.load(open(sys.argv[1]))["conflicts"]))' "$temp/receipt"
if [[ -n "${GITHUB_OUTPUT:-}" && -n "${RUNNER_TEMP:-}" ]]; then
  git rev-parse HEAD > "$RUNNER_TEMP/pa-apple-receipt"
  echo "verified_head=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"
fi
