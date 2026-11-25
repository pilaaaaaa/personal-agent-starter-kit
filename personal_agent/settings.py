"""Resolve optional private calendar settings without persisting credentials."""
import json
import re

from .store import require


def settings(root, env):
    path = root / "agent-config.json"
    config = json.loads(path.read_text()) if path.exists() else {}
    calendar = config.get("calendars", {})
    require(isinstance(calendar, dict), "Invalid calendars settings")
    provider = calendar.get("provider", "none")
    require(provider in {"none", "google"}, "Unknown calendar provider")
    targets, colors = {}, {}
    if provider == "none":
        return targets, colors, None
    for track in ("plan", "actual"):
        options = calendar.get(track, {})
        require(isinstance(options, dict), "Invalid calendar track settings")
        enabled = options.get("enabled", False)
        require(type(enabled) is bool, "Calendar track enabled must be a boolean")
        if not enabled:
            continue
        target = env.get(f"PERSONAL_AGENT_{track.upper()}_CALENDAR_ID", "").strip()
        require(target and target.lower() != "primary", f"Configure an owned secondary {track} calendar")
        targets[track] = target
        color = options.get("color")
        if color is not None:
            require(isinstance(color, str) and re.fullmatch(r"[1-9]|10|11", color),
                    "Google event color must be a string from 1 to 11")
            colors[track] = color
    require(len(set(targets.values())) == len(targets), "Plan and Actual require distinct targets")
    namespace = env.get("PERSONAL_AGENT_CALENDAR_NAMESPACE", "")
    if targets:
        require(re.fullmatch(r"[A-Za-z0-9_-]{16,128}", namespace), "Configure a stable calendar namespace")
    return targets, colors, namespace
