#!/bin/bash
set -euo pipefail
umask 077

root="$(cd "$(dirname "$0")/.." && pwd)"
support="$HOME/Library/Application Support/Personal Agent"
build="$(mktemp -d)"
staged=""
cleanup() {
  rm -rf "$build"
  if [[ -n "$staged" ]]; then rm -f "$staged"; fi
}
trap cleanup EXIT

cp "$root/sync-reminders.swift" "$build/sync-reminders.swift"
blob="$(git hash-object --no-filters "$build/sync-reminders.swift")"
printf 'let sourceBlob = "%s"\n' "$blob" > "$build/BuildInfo.swift"
# Keep the executable identifier used by the original local installation.
swiftc -parse-as-library -D VERSIONED_BUILD -module-cache-path "$build/modules" \
  "$build/sync-reminders.swift" "$build/BuildInfo.swift" -o "$build/sync-reminders-organize"
[[ "$("$build/sync-reminders-organize" --version)" == "$blob" ]]
"$build/sync-reminders-organize" --validate "$root/tasks.json"

mkdir -p "$support"
staged="$(mktemp "$support/.sync-reminders.XXXXXXXX")"
install -m 700 "$build/sync-reminders-organize" "$staged"
mv -f "$staged" "$support/sync-reminders"
staged=""
echo "Installed sync-reminders from Swift blob $blob"
