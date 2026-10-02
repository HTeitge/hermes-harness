"""Static checks on the kit itself: valid YAML with NO duplicate keys (docker compose rejects
them; PyYAML silently keeps the last), overlays merge, every compose env var is documented."""
import os
import re

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        assert key not in seen, f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)

YAML_FILES = ["docker-compose.yml", "docker-compose.replica.yml", "hermes-home/config.yaml",
              "hermes-home/config.copilot.yaml", "hermes-home/config.flowgear.yaml",
              "hermes-home/config.atlassian.yaml", "hermes-home/assistant/config.yaml",
              "hermes-home/plugins/git-guard/plugin.yaml"]


def test_yaml_files_have_unique_keys():
    for f in YAML_FILES:
        with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
            yaml.load(fh, Loader=_UniqueKeyLoader)


def test_compose_env_vars_documented():
    compose = open(os.path.join(ROOT, "docker-compose.yml"), encoding="utf-8").read()
    example = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", example, re.M))
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)(?::[-?]|\})", compose))
    missing = used - documented
    assert not missing, f"compose references undocumented .env variables: {sorted(missing)}"


def test_overlay_merge_shapes():
    base = yaml.safe_load(open(os.path.join(ROOT, "hermes-home/config.yaml"), encoding="utf-8"))
    assert base["plugins"]["enabled"] == ["git-guard"]
    assert base["platform_toolsets"]["cli"] == ["hermes-cli", "kanban"]
    assert base["display"]["memory_notifications"] == "on"  # must stay a string, not a YAML bool
    cop = yaml.safe_load(open(os.path.join(ROOT, "hermes-home/config.copilot.yaml"), encoding="utf-8"))
    assert "review" not in cop["auxiliary"], "auxiliary.review has no fallback path in Hermes; do not pin it"
    for k in ("goal_judge", "side_question"):
        assert cop["auxiliary"][k]["fallback_chain"][0]["provider"] == "custom"
    atl = yaml.safe_load(open(os.path.join(ROOT, "hermes-home/config.atlassian.yaml"), encoding="utf-8"))
    inc = atl["mcp_servers"]["atlassian"]["tools"]["include"]
    assert all(not p.lower().startswith(("create", "update", "delete", "add", "edit")) for p in inc)


def test_scripts_are_lf():
    for f in ("guard/verify.sh", "guard/seed-config.py", "guard/git-hooks/pre-push", "edge/entrypoint.sh"):
        data = open(os.path.join(ROOT, f), "rb").read()
        assert b"\r\n" not in data, f"{f} has CRLF line endings"
        assert data.startswith(b"#!"), f"{f} missing shebang"
