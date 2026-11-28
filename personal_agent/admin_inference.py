"""Conservative legacy metadata estimates. Explicit checkpoints always take precedence."""
from collections import defaultdict
from datetime import timedelta
import re
from statistics import median

from .store import instant

PROJECTS = {
    "maintenance": {"荷兰离境", "上海安家", "日常事务", "日常生活", "个人整理", "账户安全",
                    "tu/e × kth 双学位毕业", "kth 毕业相关", "毕业收尾"},
    "growth": {"半导体行业研究", "职业表达", "商业金融", "ai创业", "商业阅读", "教育行业研究",
               "趋势观察", "兴趣学习", "技术学习", "量化学习", "副业验证", "个人表达训练",
               "知识产品", "商业研究与ai能力库"},
}
ACTIONS = (
    ("email", 15, r"邮件|邮箱|\bemail\b|follow.up|\bcorrespondence\b"),
    ("forms", 30, r"表格|填[写报]|checklist|材料提交|提交.{0,6}材料|\b(application|paperwork|forms?)\b"),
    ("trade", 20, r"买家|出售|挂牌|vinted|收款|二手"),
    ("admin", 25, r"手续|退款|核实|补贴|机票|预约|缴费|备份|\b(renew|refund|invoice|appointment|reimburse)\b"),
    ("setup", 30, r"^(?:配置|布置|整理).{0,18}(?:学习区|工作区|办公区|家居)|\b(set up|organize) (?:the |my )?(?:workspace|home office)\b"),
    ("inspection", 30, r"^(?:检查|查看).{0,20}(?:房屋|家具|住房|公寓)|\b(property inspection|apartment viewing)\b"),
    ("errand", 20, r"收拾|洗碗|购物|采购|超市|煮饭|煮米饭|备餐"),
)
GROWTH = re.compile(
    r"学习|研究|行业报告|表达练习|商业验证|客户.{0,4}验证|影子(?:模式|实验)|"
    r"评估.{0,28}(?:升级|上线|实验)|\b(research|coursework|study|experiment|prototype)\b"
)
ACTUAL_LANES = {
    "maintenance": {"行政", "行政事务", "日常事务", "入职", "毕业", "荷兰离境", "admin", "administration"},
    "growth": {"学习", "个人成长", "research", "study", "professional development"},
}


def active_sample(row):
    return not (row.get("deleted") or row.get("effort") == "passive"
                or row.get("status") not in {"completed", "occurred"})


def classify(task):
    title = task.get("title", "").casefold()
    project = task.get("project", "").strip().casefold()
    action = next(((batch, effort) for batch, effort, pattern in ACTIONS if re.search(pattern, title)), None)
    project_lane = next((lane for lane, projects in PROJECTS.items() if project in projects), None)
    growth = bool(GROWTH.search(title)) and not (action and action[0] == "setup")
    if (project_lane == "growth" and action) or (project_lane == "maintenance" and growth) or (action and growth):
        return None, None, "ambiguous_signals"
    lane = project_lane or ("maintenance" if action else "growth" if growth else None)
    return lane, action, "project" if project_lane else "title" if lane else "insufficient_evidence"


def infer(data, tasks, actual, now):
    profiles = {"task:" + task["id"]: classify(task) for task in tasks["tasks"]}
    first, planned, categories = {}, defaultdict(dict), defaultdict(set)
    for entry in data["operations"]:
        if entry["kind"] != "plan":
            continue
        observed = instant(entry["recorded_at"])
        if observed > now:
            continue
        for item in entry["record"]["items"]:
            for ident in item.get("task_ids", []):
                key = "task:" + ident
                first.setdefault(key, observed)
                start, end = instant(item.get("start")), instant(item.get("end"))
                if (start and end and item.get("status", "scheduled") == "scheduled"
                        and not item.get("parallel_to") and timedelta(minutes=5) <= end - start <= timedelta(hours=2)):
                    planned[key][start.date()] = (end - start).total_seconds() / 60
    for row in actual.values():
        observed = instant(row.get("first_recorded_at") or row.get("recorded_at") or row.get("start"))
        if not active_sample(row) or not observed or observed > now:
            continue
        category = str(row.get("category", "")).casefold().strip()
        lane = next((value for value, labels in ACTUAL_LANES.items() if category in labels), None)
        finished = instant(row.get("end"))
        for ident in row.get("task_ids", []):
            key = "task:" + ident
            first[key] = min(first.get(key, observed), observed)
            if lane and finished and finished <= now:
                categories[key].add((lane, finished.date()))
    for key, (lane, action, origin) in list(profiles.items()):
        votes = categories[key]
        labels = {label for label, _ in votes}
        if lane is None and origin == "insufficient_evidence" and len(votes) >= 2 and len(labels) == 1:
            profiles[key] = (next(iter(labels)), action, "linked_actual_categories")
    actual_samples, cohorts = {}, defaultdict(list)
    for row in actual.values():
        if not active_sample(row) or row.get("status") != "completed":
            continue
        end, start = instant(row.get("end")), instant(row.get("start"))
        key = "task:" + str(row.get("complete_task"))
        lane, action, _ = profiles.get(key, (None, None, None))
        if row.get("precision") == "exact" and lane and start and end and start < end <= now:
            actual_samples[key] = (action[0] if action else lane, (end - start).total_seconds() / 60)
    for group, elapsed in actual_samples.values():
        cohorts[group].append(elapsed)
    result = {}
    for key, (lane, action, origin) in profiles.items():
        values, evidence = {}, {}
        if lane:
            values["lane"] = lane
            evidence["lane"] = origin
        if key in first:
            values["first_seen_at"] = first[key].isoformat()
            evidence["first_seen_at"] = "journal_observation"
        if lane:
            group, fallback = action or (lane, 45 if lane == "growth" else None)
            samples = cohorts[group]
            budget_samples = planned[key]
            if len(samples) >= 3:
                values["effort_minutes"] = max(5, min(180, round(median(samples))))
                evidence["effort_minutes"] = f"exact_completion_cohort:{len(samples)}"
            elif len(budget_samples) >= 2:
                values["effort_minutes"] = round(median(budget_samples.values()))
                evidence["effort_minutes"] = f"historical_plan_budget:{len(budget_samples)}"
            elif fallback:
                values["effort_minutes"] = fallback
                evidence["effort_minutes"] = "provisional_action_estimate"
            if action:
                values["batch"] = group
                evidence["batch"] = "title"
        result[key] = (values, evidence, origin)
    return result
