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

    precision_labels = {"exact": "精确", "estimated": "估算", "unknown": "未知"}
    parallel = {}
    for entry in review["parallel"]:
        left, right = entry["activity_ids"]
        parallel.setdefault(left, set()).add(right)
        parallel.setdefault(right, set()).add(left)
    names = {item["id"]: item["name"] for item in review["timeline"]}
    lines = [f"# 实际活动时间轴 · {review['from']} 至 {review['through']}", "",
             f"时区 `{zone.key}`", "",
             "| 开始 | 结束 | 活动 | 类别 | 时间依据 | 并行 | 产出 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for item in review["timeline"]:
        peers = sorted(parallel.get(item["id"], set()) | set(item.get("parallel_ids", [])))
        output = item.get("outputs") or "—"
        if isinstance(output, list):
            output = "、".join(map(str, output))
        basis = precision_labels[item["precision"]]
        if item.get("estimate_basis") and item["precision"] == "estimated":
            basis += " · " + item["estimate_basis"]
        if item.get("duration_minutes") and item["id"] in review["unknown_duration_ids"]:
            basis += f" · 报告时长 {item['duration_minutes']:g} 分钟"
        fields = (local(item.get("start")), local(item.get("end")), item["name"], item["category"],
                  basis, "、".join(names.get(peer, peer) for peer in peers) or "—", output)
        lines.append("| " + " | ".join(map(cell, fields)) + " |")
    if not review["timeline"]:
        lines.append("| — | — | 暂无活动记录 | — | — | — | — |")
    lines.extend(["", f"已知覆盖 {review['known_seconds'] / 3600:.2f} 小时，"
                 f"其中精确 {review['exact_seconds'] / 3600:.2f} 小时，"
                 f"仅估算 {review['estimated_only_seconds'] / 3600:.2f} 小时。"
                 f"另有 {len(review['unknown_duration_ids'])} 条时长未知的记录。"])
    if review["elapsed_coverage"] is not None:
        lines.append(f"已过去窗口覆盖率 {review['elapsed_coverage']:.1%}，"
                     f"未覆盖 {review['uncovered_elapsed_seconds'] / 3600:.2f} 小时。")
    lines.extend(["", "## 计划演化", "",
                  "| 日期 | 生效开始 | 生效结束 | 变更 | 原因 | 关联实际 |",
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
            labels.append("生效前被替代")
        fields = (version["day"], local(version["effective_at"]), local(stage["effective_until"]),
                  f"{'/'.join(labels) + ' · ' if labels else ''}{len(version['changes'])} 项",
                  version["decision"]["reason"], "、".join(names[key] for key in stage["actual_ids"]) or "—")
        lines.append("| " + " | ".join(map(cell, fields)) + " |")
    if not review["stages"]:
        lines.append("| — | — | — | 暂无计划版本 | — | — |")
    return "\n".join(lines)
