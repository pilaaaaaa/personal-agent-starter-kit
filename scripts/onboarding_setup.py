"""Evaluate setup prerequisites from host-supplied checkpoint evidence."""
import argparse
import json
import sys


def next_gate(checkpoint, setup, *, external_sync=False, integrations_resolved=False,
              plugin_reviewed_for_request=False):
    # Exited onboarding stays exited. An explicit sync request still needs a
    # decision about the plugin before any new provider setup.
    state = checkpoint["current_state"]
    status = checkpoint["overall_status"]
    pending = setup.get("pending_question")
    if status in {"paused", "exited"} and not external_sync:
        return None
    if pending:
        return pending
    if checkpoint.get("pending_question") and not external_sync:
        return None

    if state in {"S3", "S4"} or external_sync:
        if setup.get("naming", "unasked") == "unasked":
            return "agent_name"
    if state == "S4" or external_sync:
        plugin = setup.get("plugin", "unasked")
        if plugin == "selected":
            return "plugin_grant"
        if plugin in {"failed", "unavailable"}:
            return "plugin_retry"
        if plugin == "unasked":
            return "plugin_offer"
        if external_sync and plugin in {"declined", "deferred"} and not plugin_reviewed_for_request:
            return "plugin_offer"
    if state == "S4" and integrations_resolved:
        if setup.get("personalization", "unasked") == "unasked":
            return "personalization_offer"
        if setup["personalization"] == "selected":
            return "personalization_grant"
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--external-sync", action="store_true")
    parser.add_argument("--integrations-resolved", action="store_true")
    args = parser.parse_args()
    evidence = json.load(sys.stdin)
    print(json.dumps({"next_question": next_gate(
        evidence["checkpoint"], evidence["setup"], external_sync=args.external_sync,
        integrations_resolved=args.integrations_resolved,
        plugin_reviewed_for_request=evidence.get("plugin_reviewed_for_request", False),
    )}))
