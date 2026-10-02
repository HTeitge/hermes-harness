"""git-guard — Hermes pre_tool_call plugin.

Layer L1 of the hermes-harness three-layer model (see README):
  L1 this plugin: parse-and-classify every terminal/execute_code/file-write call, hard deny.
  L2 read-only /etc/gitconfig (pushInsteadOf -> disabled://, hooksPath -> /opt/guard/git-hooks).
  L3 network: Hermes sits on an internal network; only the egress proxy allowlist is reachable;
     no origin credentials exist in the container.

The hook never raises: policy.check_tool_call() converts any internal error into a block.
Every decision for a guarded tool is appended to $HERMES_HOME/git-guard/decisions.jsonl so
the ask/deny history can later train a narrower classifier (the "labelling machine" idea).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

from . import policy

logger = logging.getLogger("hermes.plugins.git-guard")

_BLOCK_SUFFIX = (
    " This sandbox cannot push to, or alter, the git origin and cannot escape the terminal guard. "
    "Commit locally and tell the operator what needs pushing. Do not retry with different phrasing, "
    "wrappers, scripts or encodings; those are denied too and are logged."
)


def _audit_path() -> str:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return os.path.join(home, "git-guard", "decisions.jsonl")


def _audit(record: Dict[str, Any]) -> None:
    try:
        path = _audit_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - auditing must never affect the verdict
        logger.debug("git-guard audit write failed: %s", exc)


def _summarise_args(tool_name: str, args: Any) -> str:
    if not isinstance(args, dict):
        return repr(args)[:200]
    key = {"terminal": "command", "execute_code": "code", "write_file": "path", "patch": "path",
           "skill_manage": "name", "process_manage": "action"}.get(tool_name)
    val = args.get(key) if key else None
    return (val if isinstance(val, str) else json.dumps(args, default=str))[:500]


def _on_pre_tool_call(tool_name: str = "", args: Any = None, task_id: str = "", session_id: str = "",
                      **_: Any) -> Optional[Dict[str, str]]:
    reason = policy.check_tool_call(tool_name, args)  # never raises
    if tool_name in policy.PROTECTED_TOOLS:
        _audit({
            "ts": time.time(), "tool": tool_name, "decision": "block" if reason else "allow",
            "reason": reason, "input": _summarise_args(tool_name, args),
            "task_id": task_id, "session_id": session_id,
        })
    if reason:
        logger.warning("git-guard blocked %s: %s", tool_name, reason)
        return {"action": "block", "message": f"BLOCKED by git-guard: {reason}.{_BLOCK_SUFFIX}"}
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    logger.info("git-guard registered (HERMES_HOME=%s, guard root=%s)",
                os.environ.get("HERMES_HOME", "~/.hermes"), os.environ.get("GIT_GUARD_ROOT", "/opt/guard"))
