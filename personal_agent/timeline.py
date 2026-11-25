"""Render a standalone, accessible review timeline as Markdown."""
from zoneinfo import ZoneInfo

from .store import instant


def cell(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def markdown(review):
    zone = ZoneInfo(review["timezone"])

    def local(value):
        when = instant(value)
        return when.astimezone(zone).strftime("%Y-%m-%d %H:%M %z") if when else "—"

    precision_labels = {"exact": "Exact", "estimated": "Estimated", "unknown": "Unknown"}
    parallel = {}
    for entry in review["parallel"]:
        left, right = entry["activity_ids"]
        parallel.setdefault(left, set()).add(right)
        parallel.setdefault(right, set()).add(left)
    names = {item["id"]: item["name"] for item in review["timeline"]}
    lines = [f"# Activity timeline — {review['from']} to {review['through']}", "",
             f"Time zone `{zone.key}`", "",
             "| Start | End | Activity | Category | Precision | Parallel | Outputs |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for item in review["timeline"]:
        peers = sorted(parallel.get(item["id"], set()) | set(item.get("parallel_ids", [])))
        output = item.get("outputs") or "—"
        if isinstance(output, list):
            output = ", ".join(map(str, output))
        basis = precision_labels[item["precision"]]
        if item.get("estimate_basis") and item["precision"] == "estimated":
            basis += " — " + item["estimate_basis"]
        if item.get("duration_minutes") and item["id"] in review["unknown_duration_ids"]:
            basis += f" — Reported duration: {item['duration_minutes']:g} min"
        fields = (local(item.get("start")), local(item.get("end")), item["name"], item["category"],
                  basis, ", ".join(names.get(peer, peer) for peer in peers) or "—", output)
        lines.append("| " + " | ".join(map(cell, fields)) + " |")
    if not review["timeline"]:
        lines.append("| — | — | No activities recorded | — | — | — | — |")
    lines.extend(["", f"Known coverage: {review['known_seconds'] / 3600:.2f} h, "
                 f"including {review['exact_seconds'] / 3600:.2f} h exact and "
                 f"{review['estimated_only_seconds'] / 3600:.2f} h estimated only. "
                 f"{len(review['unknown_duration_ids'])} activities have unknown duration."])
    if review["elapsed_coverage"] is not None:
        lines.append(f"Elapsed coverage: {review['elapsed_coverage']:.1%} with "
                     f"{review['uncovered_elapsed_seconds'] / 3600:.2f} h uncovered.")
    lines.extend(["", "## Plan history", "",
                  "| Date | Effective from | Effective until | Changes | Reason | Linked activity |",
                  "| --- | --- | --- | --- | --- | --- |"])
    for stage in review["stages"]:
        version = stage["version"]
        rolling = review["rolling"][version["day"]]
        labels = []
        if review["initial"][version["day"]]["id"] == version["id"]:
            labels.append("Initial")
        if rolling and rolling["id"] == version["id"]:
            labels.append("Rolling")
        if stage["superseded_before_effective"]:
            labels.append("Superseded before taking effect")
        fields = (version["day"], local(version["effective_at"]), local(stage["effective_until"]),
                  f"{'/'.join(labels) + ' — ' if labels else ''}{len(version['changes'])} items",
                  version["decision"]["reason"], ", ".join(names[key] for key in stage["actual_ids"]) or "—")
        lines.append("| " + " | ".join(map(cell, fields)) + " |")
    if not review["stages"]:
        lines.append("| — | — | — | No plan versions | — | — |")
    return "\n".join(lines)
