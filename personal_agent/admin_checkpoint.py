"""Optional operational evidence on the existing versioned intake unit."""
from datetime import datetime

from .store import instant, require

TEXT = {"next_action", "completion_condition", "blocker", "batch"}
LISTS = {"completed_steps", "open_questions", "references"}
NUMBERS = {"effort_minutes", "lead_days", "aging_days", "future_effort_minutes"}
TIMES = {"first_seen_at", "available_from", "window_end", "cost_change_at"}


def validate(checkpoint, target, observed):
    require(isinstance(checkpoint, dict) and target and target.startswith(("task:", "item:")),
            "An operational checkpoint requires a stable work identity")
    require(set(checkpoint) <= TEXT | LISTS | NUMBERS | TIMES | {"lane", "phase", "unlocks_wait"},
            "Unexpected operational checkpoint field")
    for key, value in checkpoint.items():
        if value is None:
            continue  # Explicitly clear this field on this identity.
        if key in TEXT:
            require(isinstance(value, str) and 0 < len(value.strip()) <= 1000, f"Invalid {key}")
        elif key in LISTS:
            require(isinstance(value, list) and len(value) <= 30 and all(
                isinstance(item, str) and 0 < len(item.strip()) <= 1000 for item in value), f"Invalid {key}")
        elif key in NUMBERS:
            require(type(value) is int and 0 < value <= 525600, f"Invalid {key}")
        elif key in TIMES:
            require(isinstance(value, str) and isinstance(instant(value), datetime), f"Invalid {key}")
            if key == "first_seen_at":
                require(instant(value) <= observed, "First observation cannot be in the future")
        elif key == "lane":
            require(value in {"maintenance", "growth"}, "Invalid resource lane")
        elif key == "phase":
            require(value in {"ready", "active", "blocked", "deferred"}, "Invalid checkpoint phase")
        elif key == "unlocks_wait":
            require(type(value) is bool, "Invalid external wait flag")
