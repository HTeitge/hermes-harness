# AGENTS.md — project context for Hermes in the hermes-harness sandbox

Copy this file to the root of every repo under /workspace and fill in the project section.
Hermes injects it into the system prompt. State what is absent so the agent does not spend
turns discovering it.

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

## Project (fill in)

- Stack:
- Build: `dotnet build -c Release`
- Test: `dotnet test --filter Category=Unit`
- Replica services and ports:
- Where generated code lives (and the `.ignore` negations that make it searchable):
- Conventions (branching, commit message format):
