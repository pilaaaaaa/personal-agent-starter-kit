"""Evaluate one turn of optional GitHub Star guidance without performing writes."""
import argparse
import json
from pathlib import Path
import sys


def evaluate(policy, previous, milestones, feedback=None):
    """Use only milestones reached this turn and explicit user feedback."""
    known = policy["milestones"]
    terminals = policy["terminal_feedback"]
    if previous is None:
        previous = {"feedback": "unanswered", "offered": []}
    if not isinstance(previous, dict) or set(previous) != {"feedback", "offered"}:
        raise ValueError("Unrecognized Star state")
    offered = previous["offered"]
    if (previous["feedback"] not in ["unanswered", *terminals]
            or not isinstance(offered, list)
            or any(item not in known for item in offered)
            or len(set(offered)) != len(offered)):
        raise ValueError("Unrecognized Star state")
    if feedback is not None and feedback not in terminals:
        raise ValueError("Expected explicit Star feedback")
    if any(item not in known for item in milestones):
        raise ValueError("Unrecognized Star milestone")

    state = {"feedback": feedback or previous["feedback"], "offered": list(offered)}
    invitation = None
    if state["feedback"] == "unanswered":
        for milestone in policy["priority"]:
            if milestone in milestones and milestone not in offered:
                state["offered"].append(milestone)
                invitation = {"milestone": milestone, "url": policy["url"]}
                break
    return {"state": state, "invitation": invitation}


def main():
    workflow = Path(__file__).resolve().parents[1] / "onboarding" / "workflow.json"
    policy = json.loads(workflow.read_text(encoding="utf-8"))["optional_star"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--milestone", action="append", default=[], choices=policy["milestones"])
    parser.add_argument("--feedback", choices=policy["terminal_feedback"])
    args = parser.parse_args()
    try:
        result = evaluate(policy, json.load(sys.stdin), args.milestone, args.feedback)
    except ValueError as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
