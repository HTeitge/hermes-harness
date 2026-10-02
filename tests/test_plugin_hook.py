"""Exercise the plugin entry point the way Hermes calls it (keyword args, extra kwargs)."""
import importlib
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(HERE, "..", "hermes-home", "plugins")


def _load(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sys.path.insert(0, PLUGIN_DIR)
    pkg_name = "git_guard_under_test"
    spec = importlib.util.spec_from_file_location(
        pkg_name, os.path.join(PLUGIN_DIR, "git-guard", "__init__.py"),
        submodule_search_locations=[os.path.join(PLUGIN_DIR, "git-guard")])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_register_and_block(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    hooks = {}
    registered = []
    ctx = types.SimpleNamespace(register_hook=lambda name, fn: hooks.setdefault(name, fn),
                                register_terminal_environment_provider=lambda p: registered.append(p))
    mod.register(ctx)
    # without the Hermes package importable the strip provider is skipped gracefully
    assert registered == [] or registered[0].strip_env_keys
    cb = hooks["pre_tool_call"]
    out = cb(tool_name="terminal", args={"command": "git push"}, task_id="t1", session_id="s1",
             tool_call_id="c", turn_id="u", api_request_id="a", middleware_trace=[])
    assert out["action"] == "block" and "git push" in out["message"]
    assert cb(tool_name="terminal", args={"command": "git status"}, task_id="t1") is None
    assert cb(tool_name="browser_navigate", args={"url": "http://web:3000"}) is None
    assert cb(tool_name="execute_code", args={"code": "print(1)"})["action"] == "block"
    # fail closed on garbage
    assert cb(tool_name="terminal", args=None)["action"] == "block"
    out = cb(tool_name="mcp__flowgear__DeployWorkflow", args={"id": "w1"})
    assert out["action"] == "approve" and out["rule_key"].endswith("DeployWorkflow")
    assert cb(tool_name="mcp__atlassian__createJiraIssue", args={})["action"] == "block"
    assert cb(tool_name="mcp__atlassian__getJiraIssue", args={"issueKey": "A-1"}) is None
    lines = open(tmp_path / "git-guard" / "decisions.jsonl").read().splitlines()
    recs = [json.loads(l) for l in lines]
    assert [r["decision"] for r in recs] == ["block", "allow", "block", "block", "approve", "block", "allow"]
