# hermes-harness

Hermes Agent (Nous Research) running in Docker Desktop on a Windows laptop as an autonomous
software-engineering agent against a dockerised replica of a live environment, driven by a
self-hosted OpenAI-compatible model, with **git-origin mutation made impossible by three
independent layers** rather than by prompt text.

Built against Hermes **v0.21.5 (v2026.9.24)**. The project merges hundreds of PRs per patch
release and its docs drift from shipped defaults; every key used here was checked against the
source at that tag. Bump `HERMES_TAG` deliberately and re-run `verify.sh`.

```
 laptop (Windows, Docker Desktop)
 ┌───────────────────────────────────────────────────────────────────────────┐
 │  127.0.0.1:9119 (dashboard)   127.0.0.1:8642 (OpenAI-compatible API)      │
 │        │                             │                                    │
 │  ┌─────┴─────────────────────────────┴──────┐      ┌────────────────────┐ │
 │  │ edge   tinyproxy egress ALLOWLIST (8888) │──────│ replica network    │ │
 │  │        socat ingress relays              │      │ api, web, db, ...  │ │
 │  └─────┬────────────────────────────────────┘      └────────────────────┘ │
 │        │ hermes-internal (internal: true — no route out)                  │
 │  ┌─────┴───────────────────┐   ┌───────────────────────────────────────┐  │
 │  │ hermes  gateway+dashboard│   │ docker-proxy  HAProxy -> docker.sock  │  │
 │  │  git-guard plugin (L1)   │───│  read + exec only (lifecycle opt-in)  │  │
 │  │  /etc/gitconfig (L2)     │   └───────────────────────────────────────┘  │
 │  │  /workspace (your repos) │                                              │
 │  └──────────────────────────┘                                              │
 └───────────────────────────────────────────────────────────────────────────┘
          │ HTTPS via edge only
          ▼
   $LLM_BASE_URL  (your llama-server / vLLM endpoint, >= 64k ctx, native tool calls)
```

## The three layers

| Layer | Mechanism | What it stops | Owner of the guarantee |
|---|---|---|---|
| **L1 tool layer** | `git-guard` plugin on Hermes's `pre_tool_call` hook, plus Hermes's own `approvals.deny` floor | `git push`, remote/config/hook mutation, every shell escape around it (`sh -c`, `eval`, wrappers, `xargs`, `find -exec`, heredocs, inline Python/Node/Perl, encoded PowerShell), edits to `.git/`, `~/.ssh`, Hermes config, docker create/run/build/cp | fails **closed**: unparseable = blocked, hook timeout = blocked, plugin error = blocked |
| **L2 repository layer** | root-owned `/etc/gitconfig` baked into the image: `pushInsteadOf` rewrites every push URL to `disabled://`, `core.hooksPath` points at a read-only `pre-push` that exits 1 | pushes spelled through aliases, wrapper scripts, or anything L1 misparsed | git itself, config the agent user cannot write |
| **L3 environment layer** | agent on an `internal: true` network; only `edge` has a route out and it allowlists hostnames; **no SSH key, PAT or credential helper exists in the container** | a push that somehow passes L1 and L2 still has no transport and no identity | Docker networking |

L1 is the only layer the model ever sees; L2 and L3 are what make the guarantee hold. Each layer
has known gaps on its own (a parser guard is a speed bump; repo-local config overrides system
config; a proxy allowlist can be mis-edited). Together they fail independently. The guard has
been through one adversarial review (about 180 bypass shapes plus 40,000 fuzz inputs); the
six bypasses it found (input redirection and here-strings reading credential stores, one-line
`function` bodies, `hash -p` rebinding, lone-CR separators, writes into the token directory)
are fixed and locked in by `tests/test_policy.py::test_redteam_round1`. Treat "no known
bypass" as the honest claim, not "no bypass".

## Prerequisites

- Windows 10/11 with Docker Desktop (WSL2 backend), `docker compose` v2.
- A separate clone of each repo the agent should work in, under one folder (`WORKSPACE`).
  Do not point it at your own working copies: L2 config is container-side only, but the agent
  commits freely and resets freely.
- The llama-server API key.
- Optional: the replica environment already running under compose (note its network name
  with `docker network ls`).

## Setup (PowerShell)

```powershell
git clone <this kit> C:\hermes-harness
cd C:\hermes-harness
Copy-Item .env.example .env
notepad .env          # WORKSPACE, LLM_BASE_URL/MODEL/API_KEY, dashboard login, EGRESS_ALLOW, REPLICA_NETWORK

docker compose build                      # derived hermes image (L2 baked in) + edge
docker compose up -d                      # without the replica network
# or, once the replica stack exists:
docker compose -f docker-compose.yml -f docker-compose.replica.yml up -d

docker compose logs -f hermes             # wait for "gateway" and "dashboard" to come up
docker compose exec hermes /opt/guard/verify.sh
```

Open <http://127.0.0.1:9119>, log in with the basic-auth values from `.env`, use the **Chat**
tab (it is the full Hermes TUI in the browser). Approval prompts for commands Hermes itself
flags as dangerous appear there; `approvals.mode: manual` means you answer them.

Model: `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` in `.env` are interpolated into
`config.yaml` at start. `context_length` is 131072 in `hermes-home/config.yaml`; change it to
match your server. To change anything else in the seeded config, edit it in the volume
(`docker compose exec hermes vi /opt/data/config.yaml`) or delete it there and re-run
`docker compose run --rm hermes-init` to re-seed from `hermes-home/config.yaml`.

## Daily use

- **Chat**: dashboard Chat tab. Or `docker compose exec -it hermes hermes` for the TUI in a terminal.
- **API**: set `API_SERVER_ENABLED=true` and `API_SERVER_KEY` in `.env`; Open WebUI or any
  OpenAI client talks to `http://127.0.0.1:8642/v1`. Unattended sessions deny dangerous commands.
- **Repos**: live under `/workspace/<name>` in the container. Put the `templates/AGENTS.md`
  (filled in) at each repo root; Hermes injects it. State what is absent (no internet, no push)
  so the agent does not spend a context window discovering it.
- **Logs and deployments**: the agent runs `docker ps`, `docker logs`, `docker inspect`,
  `docker exec` against the laptop's Docker through the filtered proxy. `ALLOW_CONTAINER_LIFECYCLE=1`
  in `.env` additionally permits start/stop/restart/kill (both in git-guard and in the proxy).
- **Browser**: built-in accessibility-tree tools (`browser_navigate`, `browser_snapshot`,
  `browser_click`, `browser_console` for JS errors and `evaluate`). Replica UI by service name
  (`http://web:3000`), laptop-hosted dev servers via `http://host.docker.internal:<port>`; both
  must be in `EGRESS_ALLOW`.
- **Memory**: `MEMORY.md`/`USER.md` (bounded, injected per session) plus FTS5 search over all
  past sessions. Optional local vector-free provider: `docker compose exec hermes hermes memory setup`
  and pick `holographic`.
- **Learning**: skills are created on nudge and on request but land in a pending queue
  (`skills.write_approval: true`): `/skills pending`, `/skills approve <id>`. The post-turn
  background review is off (2 to 3 minutes per turn on a local 27B); run `/refine` when a
  task is worth distilling.
- **Audit**: every guarded tool call, allowed or blocked, is appended to
  `/opt/data/git-guard/decisions.jsonl` with the reason. This is also the labelled data for a
  future classifier that trims approval prompts.

## Working on a large codebase

Settings in `hermes-home/config.yaml` that matter for real engineering work, all verified
against the v0.21.5 source:

- **Long builds.** Hermes's tool executor has a separate 420-second deadline on top of
  `terminal.timeout`; the config raises `timeouts.tools.*` to 900 and the AGENTS.md template
  tells the agent to run builds and test suites as background jobs with `notify=true`.
- **Verification before stopping.** `agent.verify_on_stop: true` refuses a final answer when
  code was edited and no build, test or lint has passed since.
- **Loop hard stops.** `tool_loop_guardrails.hard_stop_enabled: true` ends identical-failure
  loops and A/B/A/B cycles instead of only warning.
- **Generated code.** `search_files` respects `.gitignore` with no override. Copy
  `templates/.ignore` to each repo root and edit the negations; ripgrep ranks `.ignore` above
  `.gitignore`, so generated directories become searchable.
- **Compression.** With a 131k window Hermes compacts near 98k tokens using the same local
  model with thinking off; `auxiliary.compression.timeout` is 600 for that reason. Proactive
  pruning is off so llama-server's prefix cache keeps hitting.
- **Sampling.** There is no temperature key; `providers.local.extra_body` carries the Qwen
  sampling values and `enable_thinking`. Change them there.
- **No C# language server ships.** `lsp.enabled: false`. To add diagnostics, bake `csharp-ls`
  into `hermes.Dockerfile`, then set `lsp.enabled: true`, `install_strategy: manual`,
  `warmup_timeout: 300`.
- **Checkpoints stay off.** The shadow-git snapshotter skips directories over 50,000 files and
  does not exclude `bin/` or `obj/`. Use branches and `git worktree`.

Built-ins worth using in chat:

| Command | Use |
|---|---|
| `/plan <task>` | Planning-only turn; writes `.hermes/plans/<timestamp>-<slug>.md`, no edits |
| `/goal <text>` + `/goal gate add "dotnet test ..."` | Keep iterating until a deterministic command passes; `/goal draft` writes a completion contract |
| `@diff`, `@staged`, `@git:5`, `@file:path:10-25`, `@folder:dir` | Inject context into a message |
| `/worktree new <name>` | Isolated checkout under `.worktrees/` for an experiment |
| `/review` | Reviewer subagent over the current changes |
| `/refine` | Manual run of the skill/memory distillation pass (the automatic one is off) |
| `/loop 10m <prompt>` | Re-run a prompt on a cadence inside the session |

Bundled skills to enable in the dashboard Skills tab: `systematic-debugging`,
`test-driven-development`, `requesting-code-review`, `spike`, `simplify-code`.

## Keeping track of findings

Hermes has no journal feature, so findings live in three places that the config and the
AGENTS.md template wire together:

1. **Kanban board** (`kanban.db` in the data volume, created lazily; dashboard Kanban tab).
   Cards hold a markdown body, a comment thread, attachments and an event history. Every
   worker-spawning feature is off in the config and the toolset is pre-enabled via
   `platform_toolsets.cli`, so there is no init or enable step.
2. **`docs/findings/` in each repo**, with `INDEX.md` as the table of contents
   (`templates/docs/findings/INDEX.md`). Git history keeps it, and it travels with the code.
3. **Holographic memory** (`memory.provider: holographic`): a local SQLite fact store with
   full-text search, tags and trust scores; the top matching facts are prefetched into every
   turn. No embedding model, no network. The built-in `MEMORY.md` stays for pointers.

Recall: `session_search` searches every past session (`sessions.retention_days` raised to 365;
pin important sessions with `hermes sessions pin <id>`), `fact_store` searches the memory store,
and `hermes sessions export --format md` archives transcripts. Per-repo knowledge skills must be
pinned with `hermes curator pin <skill>` or the curator archives them after 30 days unused.

## Second model: GitHub Copilot as planner and reviewer

Optional. Hermes runs one main model per session (Qwen, local, runs all tools) and lets each
auxiliary job use a different provider. With `COPILOT_MODEL` and `COPILOT_GITHUB_TOKEN` set in
`.env`, hermes-init merges `hermes-home/config.copilot.yaml` into the seeded config:

| Where Copilot is used | How | Falls back to local when |
|---|---|---|
| Planning turn | `/model planner --once` then `/plan <task>`; next turn runs on Copilot, then Qwen is restored | 402 credits exhausted, 429, 5xx, 401/403: `fallback_providers` takes that turn |
| Code review | `/model planner --once` then `/review`; the reviewer inherits that turn's model | the agent-level `fallback_providers` chain (Hermes has no fallback for a pinned `auxiliary.review`, so it is deliberately not pinned) |
| `/goal` judge and `/goal draft` contracts | `auxiliary.goal_judge` | `fallback_chain` |
| `/btw <question>` | `auxiliary.side_question`, answered outside the transcript | `fallback_chain` |
| Advisor on every user turn | Mixture-of-Agents preset `plan`: Copilot advises, Qwen aggregates and acts. `/moa <prompt>` for one turn, `/model plan --provider moa` for the session | a failed advisor becomes a `[failed: ...]` note, Qwen continues |

Compression, the main loop and ordinary subagents stay on the local model, so whole
transcripts never leave the machine. Review and planning do send code to Copilot.

Setup notes:

- The token must carry only the "Copilot Requests" permission. A token with repository write
  access would be a push credential inside the container and would undermine layer L3.
  git-guard strips the kit's secret variables from the agent's shell (see "Secrets" below) and
  denies `GITHUB_*`, `GH_*` and `COPILOT_*` overrides, but the scope of the token is the real control.
- Add `github.com,api.github.com,api.githubcopilot.com` (and the business or enterprise host if
  your plan uses one) to `EGRESS_ALLOW`. `verify.sh` checks reachability and the token
  exchange when `COPILOT_MODEL` is set.
- Get the model id from the live catalog on first start: `/model --refresh` under the
  `copilot` provider. Hermes's static list in v0.21.5 stops at `gpt-5.4`; the id your
  subscription exposes may be newer. Then set `COPILOT_MODEL`, delete the seeded
  `/opt/data/config.yaml` and re-run `docker compose run --rm hermes-init`.
- There is no automatic difficulty-based routing in Hermes. Escalation is explicit: the
  `planner` alias, `/review`, `/btw`, or the MoA preset.

## Hosted MCP servers: Flowgear Builder and Atlassian

Both are vendor-hosted; nothing runs here. Each is an overlay that hermes-init merges into the
config only when its `.env` value is set, and both go out through the egress proxy, so their
hosts must be in `EGRESS_ALLOW`. MCP tools reach git-guard as `mcp__<server>__<tool>`.

**Flowgear Builder MCP** (`FLOWGEAR_MCP_URL=https://<environment-hostname>/mcp/builder`).
Remote HTTP, OAuth browser sign-in only, one Environment per URL, needs Site Administrator plus
the Builder permission bundle on the Flowgear side. First login is interactive and once:

```
docker compose exec -it hermes hermes mcp login flowgear
```

Hermes prints the authorisation URL; open it on the laptop, sign in, then paste the full
redirect URL back into the prompt (the callback listener lives inside the container, so the
browser redirect cannot reach it directly). The token lands in `/opt/data/mcp-tokens/` and
refreshes itself. Tools such as `ListWorkflows`, `ListNodes`, `GetBuilderGuidance` and the
save/test tools are exposed as-is. git-guard routes any tool whose name contains deploy,
publish, promote, release, delete or remove through the human approval gate; everything else
runs freely so flows can be built without friction. Use a Development environment. Flowgear
also keeps its own scopes and audit trail per the Builder MCP documentation.

**Atlassian hosted MCP, read-only** (`ATLASSIAN_MCP_AUTH="Basic <base64(email:token)>"`).
Atlassian Cloud only, endpoint `https://mcp.atlassian.com/v2/mcp`, headless API-token auth so no
browser step. Read-only is enforced three times: create the API token with read scopes only
(`read:jira-work`, `read:jira-user`, `read:page:confluence`, `read:content-details:confluence`,
`search:confluence`), the config registers only tools matching `get*`, `search*`, `list*`,
`lookup*`, `fetch*`, and git-guard hard-blocks any `mcp__atlassian__*` name containing create,
update, edit, delete, add, transition, move, assign or comment. OAuth is possible instead
(`auth: oauth`, then `hermes mcp login atlassian`), but corporate Atlassian often rejects
dynamic client registration; the scoped token is the reliable route.

**Secrets.** Hermes itself strips only a fixed list of vendor key names from the agent's
subprocesses; custom names such as `LLM_API_KEY` or `ATLASSIAN_MCP_AUTH` would be inherited.
git-guard therefore registers them for stripping through Hermes's terminal-environment
registry (add more with `GIT_GUARD_STRIP_ENV=NAME1,NAME2`), and at the command level blocks
`env`/`printenv`/bare `set`/`export -p`, any `$NAME` expansion of a secret-looking variable,
reads under `/proc/<pid>/`, inline interpreter code that touches the environment, and any
command or `read_file` naming `mcp-tokens/`, `.env`, `auth.json`, `.copilot_jwt.json` or
`.ssh/`. The gateway process and the agent's shell share one uid, so `/proc` is the residual
path that only the command-level rule covers.

## Personal assistant profile

`ASSISTANT_PROFILE=1` makes hermes-init create a second Hermes profile named `assistant`:
its own `SOUL.md` (persona and file formats), config, memory, sessions, skills and cron jobs,
served by the same gateway and dashboard (pick it in the dashboard's profile switcher, or
`docker compose exec -it hermes hermes -p assistant`). It cannot touch repositories: no
browser, no delegation, `git`/`dotnet`/`docker`/`curl` denied, terminal confined to
`/opt/data/assistant`, and the same git-guard plugin installed read-only.

The assistant's memory of your commitments is plain Markdown in `/opt/data/assistant`
(`tasks.md`, `agenda.md`, `followups.md`, `notes/`), formats fixed in its `SOUL.md` so cron
jobs can parse them. Hermes's kanban has no due-date field and dispatches cards to worker
agents, so it is deliberately not used for personal tasks. Microsoft 365 has no agent-facing
calendar or mail tool in Hermes v0.21.5 (the Graph helpers are app-only and used by the Teams
pipeline), so you paste meetings into `agenda.md` or ask the assistant to record them; a
Graph-based calendar skill is a later addition that needs a tenant app registration.

Reminders and briefings are cron jobs run by the gateway. Push goes to **ntfy** (phone app or
web, topic from `NTFY_TOPIC`; public `ntfy.sh` through the proxy or your own server). The
dashboard itself has no push notifications. Create the standing jobs once from the assistant
chat, in plain language or with `/cron add`:

```
/cron add "weekdays at 08:00" "Morning brief: read tasks.md, agenda.md and followups.md; list today's meetings, tasks due or overdue, follow-ups older than 3 working days. Under 15 lines. Deliver to ntfy."
/cron add "weekdays at 16:30" "End of day: ask what got done, move finished tasks to Done with today's date, list tomorrow's first three items. Deliver to ntfy."
/cron add "in 45m" "Remind me: stand-up prep"        # one-shot
```

`/blueprint` lists ready-made templates (`morning-brief`, `workday-start`, `custom-reminder`,
`evening-winddown`, `weekly-review`); the morning one assumes Google Workspace, so prefer the
prompts above. Microsoft Teams as a channel needs a public HTTPS endpoint for the bot and an
SDK that is not in the image, so it is out of scope for a laptop.

## First run

Everything is driven by `config.yaml` plus `.env`; no `hermes setup`, `hermes model`,
`hermes memory setup`, `hermes kanban init` or `hermes tools enable` is needed. On first boot
the image's init stamps `_config_version` (already set to the image's current value), copies
its own `.env.example` into the volume, seeds `SOUL.md`, and git-guard loads because it is in
`plugins.enabled`. What remains:

1. `docker compose logs hermes | grep -E "stage2|config-migrate|plugin|gateway"` once, to see
   the migration and git-guard load lines. Then `docker compose exec hermes hermes doctor`.
2. **Browser tools need one warm-up.** The `agent-browser` driver is fetched with `npx` on
   first use. Add `registry.npmjs.org` to `EGRESS_ALLOW` temporarily, run
   `docker compose exec hermes hermes doctor --fix`, then remove it. The npm cache persists in
   the volume.
3. **Flowgear only:** `docker compose exec -it hermes hermes mcp login flowgear` (see above).
4. **Copilot only:** nothing if the token is in `.env`; otherwise
   `docker compose exec -it hermes hermes auth add copilot` runs a device flow.
5. **Assistant only:** create the standing cron jobs from the assistant chat (see above) and
   subscribe your phone to the ntfy topic.
6. **Per repo:** copy `templates/AGENTS.md`, `templates/.ignore` and `templates/docs/findings/`
   into the repo. If a repo carries its own `.hermes/skills`, list its exact path under
   `skills.trusted_project_dirs`.

## Verifying

`docker compose exec hermes /opt/guard/verify.sh` checks, from inside the container as the
agent user: L2 config in force and read-only, a throwaway `git push` fails, direct egress has
no route, the proxy refuses non-allowlisted hosts and reaches the model endpoint, the socket
proxy refuses `docker run` and `docker volume create`, the plugin is mounted read-only and its
engine blocks `git push`. Then do the live test it prints: ask the agent in chat to run
`git push` and `bash -c "git push"`. Both must come back `BLOCKED by git-guard`. Hermes has had
two issues where `pre_tool_call` did not fire on a specific entry point, so test the entry point
you actually use (dashboard chat, API, TUI) after every image bump.

Unit tests for the policy engine run on any machine with Python 3.11+ and pytest:

```
python -m pytest -q tests/
```

300 cases: 94 allowed shapes, 200 blocked shapes (every bypass class listed above), the
lifecycle knob, script-file scanning, the hook surface, and a 6000-iteration fuzz asserting
the guard never throws.

## What git-guard allows and blocks (summary)

Allowed: all local git (`status log diff add commit checkout switch branch merge rebase reset
stash tag worktree fetch pull clone ls-remote`), read-only `git remote -v/show/get-url` and
`git config --get/--list/<name>`, builds and tests, `curl` (the proxy decides), `docker`
read/exec, `python3 -c`/scripts and shell scripts **after** a strict text scan, command
substitutions **after** recursive analysis, heredocs to `cat`/files.

Blocked: `git push`, `send-email`, `svn`, `filter-branch`, `remote add/set-url/rm/rename`,
config writes, `-c`/`--exec-path`/`--git-dir`, `rebase -x`, `submodule foreach`, `bisect run`,
unknown git subcommands (alias risk); `gh glab hub jj hg ssh scp eval source trap alias crontab
at tmux screen expect`; shells reading scripts from stdin or unreadable files; `GIT_*`,
`DOCKER_*`, `PATH`, `HOME`, proxy env overrides; writes to `.git/`, `~/.ssh`, `~/.gitconfig`,
`/etc/gitconfig`, Hermes `config.yaml`/`.env`/`plugins`, `/opt/guard`; `docker run create build
cp push login -H --context`; `execute_code` entirely.

## Operations

- **Upgrade**: change `HERMES_TAG` in `.env`, `docker compose build && docker compose up -d`,
  re-run `verify.sh` and the live test. Read the tag's `cli-config.yaml.example` for renamed keys.
- **Re-seed config**: the init service never overwrites an existing `config.yaml` in the volume.
- **Data**: the named volume `hermes-data` holds `/opt/data` (SQLite state, memories, skills,
  audit log). Named volume, not a bind mount: SQLite WAL on Docker Desktop bind mounts corrupts.
- **Backup**: `docker run --rm -v hermes-harness_hermes-data:/d -v ${PWD}:/b alpine tar czf /b/hermes-data.tgz -C /d .`
- **Two gateways on one data dir is unsupported**; scale by profile, not by container.

## Known limits and residual risks

- **Bind-mounted repos from Windows are slow** for large solutions (10 to 100x on file-heavy
  ops). If it hurts, keep the agent's clones in the WSL2 filesystem and point `WORKSPACE` there
  (`\\wsl$\...` paths work in compose on Docker Desktop).
- **Chromium through the proxy**: the browser engine inherits `HTTPS_PROXY` from the Hermes
  process; this is standard Chromium-on-Linux behaviour but was not exercised here (no Docker
  Desktop on the build machine). The verify script covers curl; do one `browser_navigate` to the
  replica UI as part of the live test.
- **Opaque code**: a project script or Python file the agent runs is scanned by text heuristics,
  not parsed. Something like `subprocess.run(["gi"+"t","pu"+"sh"])` passes L1 and is stopped
  by L2/L3. That is the point of having three layers.
- **`docker compose` is not in the Hermes image**; the agent uses plain `docker`. Compose
  commands in the lifecycle allowlist exist for a future image that has the plugin.
- **Approval fatigue**: `approvals.mode: manual` prompts for every Hermes-flagged pattern
  (rm -rf, chmod 777, DROP TABLE, ...). Use `[a]lways` in the prompt to grow `command_allowlist`
  for the ones you are happy with; the deny floor and git-guard still apply.
- **Docker socket scope.** The proxy allows per-container operations only for names starting
  with `REPLICA_PROJECT`; everything else, including the harness's own containers (whose
  `inspect` output would reveal this stack's secrets) and raw hex ids, is refused. `docker exec`
  into a replica container still runs a command inside that container, which has whatever
  network that container has: keep replica services off the internet, or treat exec as a
  deliberate hole.
- **Egress allowlist is exact and case-sensitive** (tinyproxy fnmatch on the hostname; a
  subdomain of an allowed host is refused). With the replica overlay, replica containers can
  also use the edge proxy to reach allowlisted hosts; that is by design but worth knowing.
- **Not verified on Docker Desktop itself**: the egress proxy and the socket filter were
  validated on Linux with Podman (proxy end to end against the real endpoint; HAProxy ACLs
  against a live API socket: every mutation path 403, every read/exec path forwarded), and the
  compose file against the real `docker compose` parser.

## Layout

```
docker-compose.yml            stack (hermes, edge, docker-proxy, hermes-init)
docker-compose.replica.yml    overlay: attach edge to the replica network
hermes.Dockerfile             derived image: /etc/gitconfig + /opt/guard baked in, root-owned
.env.example                  all knobs
hermes-home/config.yaml       Hermes config seed (model, approvals, deny floor, toolsets, plugin)
hermes-home/config.copilot.yaml  optional overlay: Copilot planner/reviewer/judge with local fallback
hermes-home/config.flowgear.yaml optional overlay: Flowgear Builder MCP (OAuth)
hermes-home/config.atlassian.yaml optional overlay: Atlassian hosted MCP, read-only (API token)
hermes-home/assistant/        assistant profile: SOUL.md, config.yaml, workspace templates
guard/seed-config.py          hermes-init helper that merges the overlay when COPILOT_MODEL is set
hermes-home/plugins/git-guard plugin.yaml, __init__.py (hook + audit), policy.py (engine)
guard/gitconfig               L2 system gitconfig
guard/git-hooks/pre-push      L2 hook (exit 1)
guard/verify.sh               in-container smoke test
edge/                         tinyproxy allowlist + socat ingress
docker-proxy/haproxy.cfg      filtered Docker API
templates/AGENTS.md           per-repo context file for the agent
templates/.ignore             re-include generated code for ripgrep/search_files
templates/docs/findings/      findings index starter for each repo
tests/                        pytest suite for the policy engine and hook
```
