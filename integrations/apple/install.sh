#!/bin/bash
set -euo pipefail
umask 077

root="$(cd "$(dirname "$0")/../.." && pwd)"
support="$HOME/Library/Application Support/Personal Agent"
build="$(mktemp -d)"
staged=""
cleanup() {
  rm -rf "$build"
  if [[ -n "$staged" ]]; then rm -f "$staged"; fi
}
trap cleanup EXIT

cp "$root/integrations/apple/reconcile-reminders.swift" "$build/sync-reminders.swift"
blob="$(git hash-object --no-filters "$build/sync-reminders.swift")"
printf 'let sourceBlob = "%s"\n' "$blob" > "$build/BuildInfo.swift"
swiftc -parse-as-library -D VERSIONED_BUILD -module-cache-path "$build/modules" \
  "$build/sync-reminders.swift" "$build/BuildInfo.swift" -o "$build/reconcile-reminders"
[[ "$("$build/reconcile-reminders" --version)" == "$blob" ]]
"$build/reconcile-reminders" --validate "$root/tasks.json"

mkdir -p "$support"
staged="$(mktemp "$support/.reconcile-reminders.XXXXXXXX")"
install -m 700 "$build/reconcile-reminders" "$staged"
mv -f "$staged" "$support/reconcile-reminders"
staged=""
echo "Installed reconcile-reminders from Swift blob $blob"
