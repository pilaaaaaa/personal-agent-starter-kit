"""Host boundary for the default conversational state update."""
import fcntl
import json

from .capture import FACTS, KEY, NON_FACTS, TASK_STATES
from .store import atomic_write, digest, instant, require


def request_for(message, observations):
    """Use a stable host conversation/message reference and the original receipt time."""
    require(isinstance(observations, list), "Interpreter must return an observation list")
    keys = set()
    for item in observations:
        key = item["key"]
        require(KEY.fullmatch(key) and key not in keys, "Observation keys must be unique and filename-safe")
        require(item["state"] in FACTS | NON_FACTS, "Unsupported observation state")
        keys.add(key)
    require(all(isinstance(message[key], str) and message[key].strip()
                for key in ("conversation_id", "message_id")), "Stable host message identity required")
    stamp = instant(message["observed_at"])
    require(stamp is not None, "Message timestamp required")
    turn_id = f"capture-turn-{stamp.strftime('%Y%m%d%H%M%S%f')}-" + digest(
        [message["conversation_id"], message["message_id"]])[:32]
    turn = {key: message[key] for key in ("conversation_id", "observed_at", "timezone")}
    turn["observations"] = observations
    if message.get("intake") is not None:
        turn["intake"] = message["intake"]
    if message.get("supersedes"):
        turn["supersedes"] = message["supersedes"]
    return {"id": turn_id, "kind": "capture_turn", "expected": None, "record": turn}


def check_receipt(request, receipt):
    """Account for every observation and distinguish durable pending from success."""
    items = request["record"]["observations"]
    if receipt is None or receipt.get("status") in {"pending", "rejected"}:
        state = "rejected" if receipt and receipt["status"] == "rejected" else "pending"
        return {"id": request["id"], "status": state,
                "error": receipt.get("error") if receipt else None,
                "dispositions": {item["key"]: {"status": state if item["state"] in FACTS else "excluded"}
                                 for item in items}}
    require(receipt.get("id") == request["id"] and receipt.get("status") == "accepted"
            and receipt.get("fingerprint") == digest(request["record"]), "Receipt does not match submitted turn")
    if "intake" in request["record"]:
        require(receipt.get("intake_operation_id") == request["id"] + "-intake",
                "Missing checkpoint intake receipt")
    dispositions = receipt["dispositions"]
    require(set(dispositions) == {item["key"] for item in items}, "Incomplete capture receipt")
    for item in items:
        result = dispositions[item["key"]]
        if item["state"] in NON_FACTS:
            require(result["status"] == "excluded" and result["reason"] == item["state"], "Invalid exclusion")
            continue
        if item["state"] in TASK_STATES:
            require(result["status"] in {"task_updated", "task_unchanged"}
                    and result["task_id"] == item["task_id"], "Missing task state receipt")
            continue
        require(result["status"] in {"written", "duplicate"} and result.get("actual_id")
                and (result["status"] != "written" or result.get("operation_id")), "Missing Actual receipt")
    return receipt


def capture_turn(message, observations, submit, *, outbox=None):
    request = request_for(message, observations)
    if ("intake" in request["record"] or request["record"].get("supersedes")
            or any(item["state"] in FACTS for item in observations)):
        try:
            if outbox is not None:
                return outbox.deliver(request, submit)
            receipt = submit(request)
        except OSError:
            return {"id": request["id"], "status": "unconfirmed",
                    "dispositions": {item["key"]: {"status": "unconfirmed" if item["state"] in FACTS else "excluded"}
                                     for item in observations}}
        return check_receipt(request, receipt)
    return {"id": request["id"], "status": "no_actual",
            "dispositions": {item["key"]: {"status": "excluded", "reason": item["state"]}
                             for item in observations}}


def handle_turn(message, interpret, submit, reply, *, outbox=None):
    """Call semantic interpretation and disposition checks before returning a normal reply.

    interpret receives the original message and host-provided context. submit
    must durably stage the request before returning its verified receipt or None.
    """
    capture = capture_turn(message, interpret(message), submit, outbox=outbox)
    response = reply(message)
    if capture["status"] in {"rejected", "unconfirmed"}:
        response += "\n\n刚才的更新暂时没保存，之后可以补上。"
    return {"reply": response, "capture": capture}


class TurnOutbox:
    """Optional durable transport retry for runtimes with a writable local store."""

    def __init__(self, path):
        self.path = path

    def deliver(self, request, submit):
        ident = request["id"]
        require(KEY.fullmatch(ident) and ident.startswith("capture-turn-"), "Invalid capture turn ID")
        self.path.mkdir(parents=True, exist_ok=True)
        with (self.path / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            payload = self.path / (ident + ".json")
            status = self.path / (ident + ".receipt")
            if payload.exists():
                require(json.loads(payload.read_text()) == request, "Outbox request identity conflict")
            else:
                atomic_write(payload, request)
            if status.exists():
                prior = json.loads(status.read_text())
                if prior["status"] in {"accepted", "rejected"}:
                    return prior
            try:
                result = check_receipt(request, submit(request))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result = check_receipt(request, None)
                result.update(status="queued", error_type=type(exc).__name__)
            atomic_write(status, result)
            return result

    def replay(self, submit):
        return [self.deliver(json.loads(path.read_text()), submit)
                for path in sorted(self.path.glob("capture-turn-*.json"))]



class LocalInbox:
    """Local filesystem transport using the same immutable Inbox as Connector."""

    def __init__(self, root):
        self.root = root

    def submit(self, request, *, process=True):
        from .inbox import drain

        inbox = self.root / "operations/inbox"
        path = inbox / (request["id"] + ".json")
        inbox.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / "personal_data/.capture.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists():
                require(json.loads(path.read_text()) == request, "Turn already staged with different evidence")
            else:
                atomic_write(path, request)
            if process:
                try:
                    drain(self.root, only=request["id"])
                except OSError:
                    # The immutable request is already staged; drain can recover it.
                    return None
            receipt = self.root / "operations/results" / path.name
            return json.loads(receipt.read_text()) if receipt.exists() else None

    def status(self, ident):
        require(KEY.fullmatch(ident) and ident.startswith("capture-turn-"), "Invalid capture turn ID")
        path = self.root / "operations/inbox" / (ident + ".json")
        request = json.loads(path.read_text())
        receipt = self.root / "operations/results" / path.name
        return check_receipt(request, json.loads(receipt.read_text()) if receipt.exists() else None)

    def pending(self):
        inbox = self.root / "operations/inbox"
        pending, repaired = [], set()
        for path in sorted(inbox.glob("capture-turn-*.json")):
            result = self.status(path.stem)
            if result["status"] == "accepted":
                repaired.add(json.loads(path.read_text())["record"].get("supersedes"))
            else:
                pending.append(result)
        return [result for result in pending if result["id"] not in repaired]


class ConnectorInbox:
    """Adapt an authorized connector without assuming a worker or poller in the host."""

    def __init__(self, commit_request, read_request, read_receipt):
        self.commit_request = commit_request
        self.read_request = read_request
        self.read_receipt = read_receipt

    def submit(self, request):
        ident = request["id"]
        persisted = self.read_request(ident)
        if persisted is None:
            try:
                self.commit_request(request)
            except OSError:
                # A timeout may be reported after the commit reached the server.
                if self.read_request(ident) is None:
                    raise
            persisted = self.read_request(ident)
        if persisted is None:
            raise OSError("Connector commit could not be verified")
        require(persisted == request, "Connector contains different evidence for this turn")
        try:
            return self.read_receipt(ident)
        except OSError:
            return None
