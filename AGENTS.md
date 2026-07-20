# Multi-agent working agreement

Two agents work on this repo concurrently (a Claude Code session and another
agent in a separate terminal). These rules exist because we have already had
two incidents from violating them (entangled uncommitted trees, and a stale
server process wiping a migrated live DB).

## Worktrees — never share a checkout

- **Integrator (Claude session):** `/home/user/repos/wattracker`, branch `main`.
- **Second agent:** `/home/user/repos/wattracker-agent2`, branch `agent2/work`
  (or `agent2/<feature>` branches cut from `main`).
- Never edit files in the other agent's worktree. Never leave work-in-progress
  in a tree you don't own.

## Integration

- Only the integrator pushes to `main`. The second agent commits to its
  `agent2/*` branch, pushes it, and reports "ready to merge".
- Before any merge to `main`: full suite green — `.venv/bin/python -m pytest`
  (run from the repo root; the venv lives in the main worktree).
- Rebase `agent2/*` on `main` before handing off if `main` has moved.

## Schema changes (serialize these)

- `SCHEMA_VERSION` in `wattracker/db.py` and the `_MIGRATIONS` chain are a
  shared sequence. Two branches must never both introduce the same version
  number. Announce a bump before starting it; if `main` gains a version while
  your branch is in flight, renumber yours on rebase.
- Migrations are in-place (`ALTER`/`CREATE`), never drop/recreate.

## Live server and live DB (single owner: the integrator)

- Live data: `~/.wattracker/wattracker.db`. Only the integrator migrates it,
  restarts the server (`./scripts/restart.sh`), or writes to it.
- The second agent tests against a scratch DB in a temp dir — never against
  `~/.wattracker`.
- Restart protocol (integrator only): back up the live DB, kill **every**
  running server process (old code holding a stale schema in memory has wiped
  the DB before), then start from pushed `main` only — never from a tree with
  uncommitted schema changes.

## Scope

- Partition by feature, not by file. Cross-branch edits to shared modules
  (`db.py`, `server.py`) are fine — git merges them; simultaneous edits to one
  tree are not.
