"""Durable native request descriptions and exact Codex response construction."""
import json
import os

from .codex_protocol import CodexProtocol, REQUEST_METHODS, MAX_REVIEW_BYTES, _text, request_key
from .store import StoreError


def describe(payload):
    if (not isinstance(payload, dict) or payload.get("protocol") != "codex-app-server-v2"
            or payload.get("method") not in REQUEST_METHODS
            or set(payload) - {"protocol", "rpc_id", "method", "params", "item"}):
        raise StoreError("unsupported Codex native request")
    request_key(payload.get("rpc_id"))
    params = payload.get("params")
    if not isinstance(params, dict):
        raise StoreError("invalid Codex request parameters")
    for key in ("threadId", "turnId", "itemId"):
        _text(params.get(key), key)
    method = payload["method"]
    if method == "item/tool/requestUserInput":
        if params.get("isBlocking") is not True:
            raise StoreError("nonblocking Codex native questions are not supported by the per-turn transport")
        questions = params.get("questions")
        if not isinstance(questions, list) or not 1 <= len(questions) <= 8:
            raise StoreError("invalid Codex questions")
        ids = set()
        for question in questions:
            if not isinstance(question, dict) or question.get("isSecret", False) is not False:
                raise StoreError("secret native input is not stored in Control")
            qid = _text(question.get("id"), "question ID", 128)
            _text(question.get("question"), "question", 8192)
            _text(question.get("header"), "question header", 512)
            options = question.get("options")
            if options is not None:
                if not isinstance(options, list) or len(options) > 16:
                    raise StoreError("invalid Codex question options")
                for option in options:
                    if not isinstance(option, dict):
                        raise StoreError("invalid Codex question option")
                    _text(option.get("label"), "option label", 4096)
                    if not isinstance(option.get("description"), str):
                        raise StoreError("invalid Codex option description")
                    if option["description"]:
                        _text(option["description"], "option description", 4096)
            if qid in ids:
                raise StoreError("duplicate native question ID")
            ids.add(qid)
        return "native-clarification", "Codex asks:\n" + json.dumps(questions, ensure_ascii=True)
    if type(params.get("startedAtMs")) is not int or params["startedAtMs"] < 0:
        raise StoreError("invalid Codex approval start time")
    if method == "item/commandExecution/requestApproval":
        if not params.get("command") or not params.get("cwd"):
            raise StoreError("unsupported Codex command approval: provider must disclose exact command and working directory")
        _text(params.get("command"), "approval command", 16000)
        _text(params.get("cwd"), "approval working directory", 4096)
        kind = params.get("kind", "command")
        if kind not in {"command", "writeStdin"}:
            raise StoreError("unsupported Codex command approval kind")
        title = "Allow this exact Codex terminal input?" if kind == "writeStdin" else "Allow this exact Codex command invocation?"
    elif method == "item/fileChange/requestApproval":
        item = payload.get("item")
        if (params.get("grantRoot") is not None or not isinstance(item, dict)
                or item.get("type") != "fileChange" or item.get("id") != params["itemId"]
                or not isinstance(item.get("changes"), list) or not item["changes"]):
            raise StoreError("Codex file approval requires exact changes without a session root grant")
        title = "Allow these exact Codex file changes?"
        for change in item["changes"]:
            if not isinstance(change, dict):
                raise StoreError("invalid Codex file change")
            _text(change.get("path"), "file path", 4096)
            if not isinstance(change.get("diff"), str):
                raise StoreError("Codex file change lacks a diff")
            if change["diff"]:
                _text(change["diff"], "file diff", MAX_REVIEW_BYTES)
            kind = change.get("kind")
            if not isinstance(kind, dict) or kind.get("type") not in {"add", "delete", "update"}:
                raise StoreError("invalid Codex file change kind")
            if kind.get("move_path") is not None:
                if kind["type"] != "update":
                    raise StoreError("invalid Codex file move")
                _text(kind["move_path"], "move path", 4096)
    else:
        _text(params.get("cwd"), "approval working directory", 4096)
        if not isinstance(params.get("permissions"), dict):
            raise StoreError("Codex permission profile is missing")
        title = "Allow this exact Codex permission profile for this turn only?"
    return "native-permission", title + "\n" + json.dumps(payload, ensure_ascii=True, sort_keys=True)


def scope_note(payload, cwd):
    """Display current path scope; native sandbox and explicit grants enforce it."""
    notes = []
    execution_cwd = payload["params"].get("cwd")
    if execution_cwd is not None and execution_cwd != cwd:
        notes.append("Execution directory differs from session directory: " + json.dumps(execution_cwd))
    outside = []
    root = os.path.realpath(cwd)
    for change in payload.get("item", {}).get("changes", []):
        for path in (change["path"], change["kind"].get("move_path")):
            if path is not None:
                resolved = os.path.realpath(os.path.join(cwd, path))
                if os.path.commonpath([root, resolved]) != root:
                    outside.append(path)
    if outside:
        notes.append("File access outside the session directory (current path resolution): " + json.dumps(outside))
    if payload["method"] == "item/permissions/requestApproval":
        notes.append("This grants the displayed permission profile for the whole native turn, including later tool calls.")
    return "\n".join(notes)


def frame(request):
    payload = request["payload"]["request"]
    describe(payload)
    if request["kind"] == "native-clarification":
        result = json.loads(request["answer"])
    elif request["answer"] in {"allow", "deny"}:
        if payload["method"] == "item/permissions/requestApproval":
            result = {"scope": "turn", "permissions": payload["params"]["permissions"] if request["answer"] == "allow" else {}}
        else:
            result = {"decision": "accept" if request["answer"] == "allow" else "decline"}
    else:
        raise StoreError("invalid retained Codex native decision")
    CodexProtocol._check_reply(payload, result)
    return {"id": payload["rpc_id"], "result": result}
