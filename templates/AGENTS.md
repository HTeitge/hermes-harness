# AGENTS.md — project context for Hermes in the hermes-harness sandbox

Copy this file to the root of every repo under /workspace and fill in the project section.
Hermes injects it into the system prompt (merged from the git root down to the cwd, and
per-directory AGENTS.md files are loaded as the agent touches those directories). State what is
absent so the agent does not spend turns discovering it.

## Environment (fixed, do not try to work around)

- You run inside a container. Repos live under `/workspace`. Commit locally as much as you like.
- **You cannot push, and you cannot change remotes, git config or hooks.** Three independent
  layers enforce this; a blocked call is final. When work is ready, write a short handover:
  branch name, commits, and what the operator should push or open a PR for.
- **There is no general internet.** Only the AI endpoint and the replica services are reachable.
  Package restores work only from caches or mirrors listed in the project section. Do not
  search the web, do not `curl` external hosts, do not wait for downloads that never come.
- `docker` works, but only `ps`, `logs`, `inspect`, `top`, `stats`, `exec` (and start/stop/restart
  if the operator enabled it). Use `docker logs --tail 300 <service>` to read deployment logs.
- The browser tools reach the replica UI and the laptop's own services by name, e.g.
  `http://web:3000` or `http://host.docker.internal:5173`. Use `browser_console` for JS errors.
- Python `execute_code` is disabled. Use the terminal tool.
- `.git/`, `~/.ssh`, `~/.gitconfig` and the Hermes config are protected paths.

## How to work here

- **Long commands**: run builds and test suites with `background=true, notify=true`. A
  foreground call longer than a few minutes is cut off; a background job is not. Truncated
  output is saved to a file named in the result; `search_files` it instead of re-running.
- **Plan before multi-file changes**: write the plan (files, order, how you will verify) with
  `/plan` or in your first message, then implement. Finish with a build or test run; a final
  answer without one is refused.
- **Searching generated code**: `search_files` respects `.gitignore`. This repo's `.ignore`
  re-includes generated directories; if a search reports hits only in ignored files, use the
  terminal: `rg --no-ignore -g '!bin' -g '!obj' <pattern>`.
- **Findings**: every non-trivial finding (bug, root cause, decision, open question, dead end)
  goes in two places the same turn: a kanban card (`kanban_create`, then `kanban_comment` with
  evidence: file paths, commands, log lines) and `docs/findings/YYYY-MM-DD.md` with a one-line
  entry added to `docs/findings/INDEX.md`. Use `session_search` and `fact_store` before
  re-investigating anything.
- **Memory**: `MEMORY.md` is tiny; store pointers there ("auth findings: kanban #12, docs/findings/2026-10-03.md"),
  store facts with `fact_store`.
- **Isolation**: for an experiment that may be thrown away, `git worktree add .worktrees/<name>`
  on a new branch and work there.

## Project (fill in)

- Stack:
- Solution map (one line per project group, where the entry points are):
- Build: `dotnet build -c Release` (background)
- Test: `dotnet test --filter Category=Unit` (background); full suite:
- Lint/format:
- Replica services and ports (service name as reachable from the browser/curl):
- Where generated code lives (and the `.ignore` negations that make it searchable):
- Package sources available offline (NuGet cache path, local mirrors):
- Conventions (branching, commit message format, what never to touch):
