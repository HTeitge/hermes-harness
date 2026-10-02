#!/bin/sh
# Smoke-test the three guard layers from INSIDE the hermes container (as the agent user):
#   docker compose exec hermes /opt/guard/verify.sh
# Exit code 0 = every check passed.
fail=0
ok()   { echo "PASS  $1"; }
bad()  { echo "FAIL  $1"; fail=1; }
hdr()  { echo; echo "== $1"; }

hdr "L2: system gitconfig + hooks are in force and read-only"
git config --system core.hooksPath | grep -q '^/opt/guard/git-hooks$' && ok "core.hooksPath -> /opt/guard/git-hooks" || bad "core.hooksPath not set"
git config --system --get-all 'url.disabled://git-push-is-blocked-by-hermes-harness/.pushinsteadof' | grep -q 'https://' && ok "pushInsteadOf rewrites push URLs to disabled://" || bad "pushInsteadOf missing"
( touch /etc/gitconfig 2>/dev/null ) && bad "/etc/gitconfig is writable by the agent user" || ok "/etc/gitconfig is read-only"
( touch /opt/guard/git-hooks/x 2>/dev/null ) && bad "/opt/guard is writable" || ok "/opt/guard is read-only"
tmp=$(mktemp -d); (
  cd "$tmp" && git init -q r && cd r && git commit -q --allow-empty -m init \
  && git remote add origin https://example.invalid/x.git \
  && if git push origin HEAD >/dev/null 2>&1; then exit 1; else exit 0; fi
) && ok "git push in a throwaway repo fails" || bad "git push in a throwaway repo SUCCEEDED"
rm -rf "$tmp"

hdr "L3: network egress only through the allowlist proxy"
[ -n "$HTTPS_PROXY" ] && ok "HTTPS_PROXY=$HTTPS_PROXY" || bad "HTTPS_PROXY unset"
code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' http://example.com/ 2>/dev/null); [ "$code" = "403" ] && ok "http://example.com refused by proxy (403)" || bad "http://example.com unexpected code '$code' (expected 403 from the proxy)"
code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' https://github.com/ 2>/dev/null); [ "$code" != "200" ] && ok "https://github.com CONNECT refused by proxy (code $code)" || bad "https://github.com reachable through the proxy"
code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' --noproxy '*' https://github.com/ 2>/dev/null); [ "$code" = "000" ] && ok "direct (no-proxy) egress has no route" || bad "direct egress reached github.com (code $code): network is NOT internal"
base="${LLM_BASE_URL:-}"
if [ -n "$base" ]; then
  code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${LLM_API_KEY:-}" "$base/models" 2>/dev/null)
  [ "$code" = "200" ] && ok "model endpoint $base/models reachable (200)" || bad "model endpoint $base/models returned '$code'"
else bad "LLM_BASE_URL is not set in the container environment"; fi

if [ -n "${COPILOT_MODEL:-}" ]; then
  hdr "Second model: GitHub Copilot (COPILOT_MODEL=$COPILOT_MODEL)"
  [ -n "${COPILOT_GITHUB_TOKEN:-}" ] && ok "COPILOT_GITHUB_TOKEN set" || bad "COPILOT_MODEL set but COPILOT_GITHUB_TOKEN empty"
  for h in github.com api.github.com api.githubcopilot.com; do
    code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' "https://$h/" 2>/dev/null)
    [ "$code" != "000" ] && [ "$code" != "403" ] && ok "$h reachable through the proxy ($code)" || bad "$h not reachable through the proxy (code $code): add it to EGRESS_ALLOW"
  done
  code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -H "Authorization: token ${COPILOT_GITHUB_TOKEN:-}" https://api.github.com/copilot_internal/v2/token 2>/dev/null)
  [ "$code" = "200" ] && ok "Copilot token exchange works (200)" || bad "Copilot token exchange returned $code (token lacks Copilot Requests permission or the plan has no Copilot)"
  grep -q 'provider: copilot' /opt/data/config.yaml && ok "config.yaml carries the Copilot block" || bad "config.yaml has no Copilot block (COPILOT_MODEL was empty at seed time; delete /opt/data/config.yaml and re-run hermes-init)"
fi

if [ -n "${FLOWGEAR_MCP_URL:-}" ]; then
  hdr "Flowgear Builder MCP"
  h=$(printf '%s' "$FLOWGEAR_MCP_URL" | sed -E 's#^https?://([^/]+).*#\1#')
  code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' "https://$h/" 2>/dev/null); [ "$code" != "000" ] && [ "$code" != "403" ] && ok "$h reachable through the proxy ($code)" || bad "$h not reachable (code $code): add it to EGRESS_ALLOW"
  grep -q '^  flowgear:' /opt/data/config.yaml && ok "mcp_servers.flowgear present in config.yaml" || bad "flowgear block missing from config.yaml (re-seed)"
  [ -f /opt/data/mcp-tokens/flowgear.json ] && ok "OAuth token present (/opt/data/mcp-tokens/flowgear.json)" || echo "TODO  no Flowgear token yet: run  docker compose exec -it hermes hermes mcp login flowgear"
fi
if [ -n "${ATLASSIAN_MCP_AUTH:-}" ]; then
  hdr "Atlassian hosted MCP (read-only)"
  code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' https://mcp.atlassian.com/ 2>/dev/null); [ "$code" != "000" ] && [ "$code" != "403" ] && ok "mcp.atlassian.com reachable through the proxy ($code)" || bad "mcp.atlassian.com not reachable (code $code): add it to EGRESS_ALLOW"
  grep -q '^  atlassian:' /opt/data/config.yaml && ok "mcp_servers.atlassian present in config.yaml" || bad "atlassian block missing from config.yaml (re-seed)"
  if command -v hermes >/dev/null 2>&1; then hermes mcp test atlassian 2>&1 | tail -3; fi
fi
if [ -n "${ASSISTANT_PROFILE:-}" ]; then
  hdr "Assistant profile"
  [ -f /opt/data/profiles/assistant/config.yaml ] && ok "profile config present" || bad "profiles/assistant/config.yaml missing (re-run hermes-init)"
  [ -f /opt/data/profiles/assistant/SOUL.md ] && ok "assistant SOUL.md present" || bad "assistant SOUL.md missing"
  [ -f /opt/data/assistant/tasks.md ] && ok "assistant workspace present" || bad "/opt/data/assistant workspace missing"
  ( touch /opt/data/profiles/assistant/plugins/git-guard/x 2>/dev/null ) && bad "assistant git-guard copy is writable" || ok "assistant git-guard copy is read-only"
  if command -v hermes >/dev/null 2>&1; then hermes profile list 2>/dev/null | grep -q assistant && ok "hermes profile list shows assistant" || bad "hermes profile list does not show assistant"; fi
  if [ -n "${NTFY_TOPIC:-}" ]; then
    code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' -H "Title: hermes-harness verify" ${NTFY_TOKEN:+-H "Authorization: Bearer $NTFY_TOKEN"} -d "verify.sh ran on $(date -u +%FT%TZ)" "${NTFY_SERVER_URL:-https://ntfy.sh}/${NTFY_TOPIC}" 2>/dev/null)
    [ "$code" = "200" ] && ok "ntfy push delivered (check your phone)" || bad "ntfy push failed (code $code): server host in EGRESS_ALLOW? topic/token right?"
  fi
fi

hdr "Docker socket proxy: read/exec only"
docker ps >/dev/null 2>&1 && ok "docker ps works" || bad "docker ps failed (DOCKER_HOST=$DOCKER_HOST)"
if docker run --rm alpine:3.22 true >/dev/null 2>&1; then bad "docker run SUCCEEDED through the proxy"; else ok "docker run refused"; fi
if docker volume create hermes-verify-tmp >/dev/null 2>&1; then bad "docker volume create SUCCEEDED"; docker volume rm hermes-verify-tmp >/dev/null 2>&1; else ok "docker volume create refused"; fi

hdr "L1: git-guard plugin"
[ -f /opt/data/plugins/git-guard/plugin.yaml ] && ok "plugin files mounted" || bad "plugin not mounted at /opt/data/plugins/git-guard"
( touch /opt/data/plugins/git-guard/x 2>/dev/null ) && bad "plugin dir is writable" || ok "plugin dir is read-only"
python3 - <<'PY' && ok "policy engine blocks push / allows status" || bad "policy engine self-test failed"
import sys; sys.path.insert(0, '/opt/data/plugins/git-guard'); import policy
assert policy.analyze_command('git push'); assert policy.analyze_command('bash -c "git push"')
assert policy.analyze_command('git status') is None; assert policy.analyze_command('dotnet build') is None
PY
grep -q 'git-guard' /opt/data/config.yaml && ok "plugins.enabled lists git-guard in config.yaml" || bad "git-guard not enabled in config.yaml"
if command -v hermes >/dev/null 2>&1; then
  hermes plugins list 2>/dev/null | grep -qi 'git-guard' && ok "hermes plugins list shows git-guard" || bad "hermes plugins list does not show git-guard (check gateway logs)"
fi

echo
echo "Live check (manual): in the dashboard chat ask the agent to run 'git push' and 'bash -c \"git push\"'."
echo "Both must come back as 'BLOCKED by git-guard'. Then check /opt/data/git-guard/decisions.jsonl."
[ $fail -eq 0 ] && echo "ALL CHECKS PASSED" || echo "SOME CHECKS FAILED"
exit $fail
