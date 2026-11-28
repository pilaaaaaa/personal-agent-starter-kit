"""Rebuild a rolling advisory from one snapshot and explicitly supplied day windows."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from .admin_shadow import analyze
from .execution import bounds
from .store import require


def forecast(data, tasks, days, now, knowledge=None, options=None):
    require(isinstance(days, dict) and 1 <= len(days) <= 14, "Provide one to fourteen consecutive planning days")
    ordered = sorted(date.fromisoformat(day) for day in days)
    zone = ZoneInfo(days[ordered[0].isoformat()]["timezone"])
    today = now.astimezone(zone).date()
    require(ordered == [today + timedelta(days=i) for i in range(len(ordered))],
            "Forecast days must be consecutive and start on the observation day")
    require(all(settings["timezone"] == zone.key for settings in days.values()), "Forecast needs one timezone")
    selected, known, reports = set(), {}, []
    additional_demand = 0
    arrival_rate = None
    service_rate = None
    known_samples = 0
    for day in ordered:
        key = day.isoformat()
        settings = days[key]
        start, _ = bounds(key, zone)
        assessment = analyze(data, tasks, key, settings, max(now, start), knowledge, options,
                             previously_selected=selected)
        m = assessment["metrics"]
        if day == today:
            known_samples = m["arrivals_with_effort_14d"]
            arrival_rate = m["arrival_effort_minutes_14d"] / 14 if known_samples else None
            service_rate = m["completed_effort_minutes_14d"] / 14 if m["completions_14d"] else None
        known.update({key: row["effort_minutes"] or 0 for key, row in assessment["work"].items()
                      if row["lane"] == "maintenance" and not row["scheduled"]
                      and (row["state"] in {"ready", "active"} or row["eligible_later_today"])})
        selected.update(slot["key"] for slot in assessment["suggested_slots"])
        # New arrivals are a sensitivity scenario; they never create imaginary tasks or slots.
        spare = max(0, m["maintenance_capacity_minutes"] - m["allocated_maintenance_minutes"])
        if known_samples:
            additional_demand = max(0, additional_demand + (arrival_rate or 0) - spare)
        remaining = sum(effort for key, effort in known.items() if key not in selected)
        reports.append({"day": day.isoformat(), "advisory": assessment,
                        "known_ready_carryover_minutes": remaining,
                        "carryover_with_observed_arrivals_minutes":
                            round(remaining + additional_demand, 2) if known_samples else None,
                        "potential_follow_ups": day != today})
    return {"status": "rolling_administration_advice", "as_of": now.isoformat(), "days": reports,
            "observed_daily_arrival_effort_minutes": arrival_rate if known_samples else None,
            "observed_daily_completion_effort_minutes": service_rate,
            "forecast_policy": "conditional_on_suggested_slots_and_supplied_availability"}
