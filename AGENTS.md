# Multi-agent working agreement

Two agents work on this repo concurrently (a Claude Code session and another
agent in a separate terminal). These rules exist because we have already had
three incidents from violating them: entangled uncommitted trees, a stale
server process wiping a migrated live DB, and — on 2026-08-13 — a rebase onto
a stale `origin/main` that force-pushed away three merged security fixes, a
test file and a tracked document.

The third incident is why the two rules below are stated as loudly as they
are. Nothing about that work was wrong except its base: `origin/main` in that
clone was 22 hours old.

## Fetch before you rebase or branch

```sh
git fetch origin && git rebase origin/main
```

Not `git rebase origin/main` on its own. A remote-tracking ref is a cache; it
is only as current as your last fetch. Rebasing onto a stale `origin/main`
silently replays an old lineage and drops everything that landed in between,
and the result looks like a clean rebase.

Before pushing a branch someone else may have touched, confirm the remote is
where you think it is:

```sh
git fetch origin && git log --oneline origin/<branch> -1
```

## Branch ownership is exclusive

A branch has exactly one owner: whoever created it. **Nobody else pushes to
it — including the integrator.**

If the integrator needs to change another agent's branch, they cut their own
branch from it and open a PR, or they ask the owner. Rewriting someone else's
branch is how reviewed work gets destroyed: the second agent's own history is
not visible to the pusher, so a force-push cannot be checked against it.

`--force-with-lease=<branch>:<sha>` against an explicitly stated SHA, never a
bare `--force`. If the lease fails, stop and look — do not retry harder.

A `pre-push` hook backing this up ships in `scripts/hooks/`. Install it in
every clone, first thing:

```sh
scripts/hooks/install.sh
```

It refuses direct pushes to `main`, and it refuses a rewrite of a shared
branch that would drop remote work with no counterpart here — naming the
commits it would have stranded. A rebase is not such a rewrite: replaying your
own commits onto merged `main` loses nothing, and the hook lets it through.

It judges by patch content, and that is not the same as identity. It can miss
a remote commit whose changes exist here under a different author or message,
and it does not look inside merge commits, so a conflict resolution recorded
only in one is not seen. In the other direction, an amend or a squash of your
own commits will trip it; read the list it prints, and if the work is already
in what you are pushing, `--no-verify` is the intended answer.

The hook cannot check the lease. Git hands `pre-push` only ref names and
shas on stdin, never the command line, so `--force-with-lease` is invisible to
it and stays a convention you keep rather than a rule it enforces. It judges
the shas instead, which catches the same mistake from the other end.

It is a guardrail against an honest mistake, not a control — `--no-verify`
bypasses it, and that is fine, because the failure being prevented is a
reflex, not an adversary. PR merges go through the GitHub API and are
unaffected.

## Never name a branch after a closed issue

Once an issue's work is merged, its branch is dead. Anything re-derived from
that branch's original base re-adds commits that already landed, which is how
`agent2/issue59` produced add/add conflicts against `main` twice while
carrying a feature that had nothing to do with issue #59.

Name a branch for the work it carries, and cut it from current `main`.

## Worktrees — never share a checkout

- **Integrator (Claude session):** the primary checkout, branch `main`.
- **Second agent:** a separate linked worktree (e.g. `../wattracker-agent2`),
  branch `agent2/work` (or `agent2/<feature>` branches cut from `main`).
- Never edit files in the other agent's worktree. Never leave work-in-progress
  in a tree you don't own.

## Commit identity — set this in every clone and worktree, first thing

This repo is **private**, but its history was deliberately rewritten once to
strip the owner's personal identity — so treat every commit as if it will be
published, because making the repo public must not re-expose the address.
The machine's *global* git config still
carries a personal address, so any clone that doesn't override it locally
re-publishes that address in every commit. This has already happened once.

**Use a distinct name per agent, and a noreply address always.** Both agents
authenticate to GitHub as the same account, so the commit *name* is the only
thing that says who did the work. When every clone committed as
`wattrackerboss`, identifying which agent force-pushed a branch required
finding the other clone's reflog on disk — the account, the PR author and the
commit metadata were all identical.

Run inside each clone/worktree, before your first commit:

```sh
# integrator / primary checkout
git config user.name  wattrackerboss
git config user.email wattrackerboss@users.noreply.github.com

# second agent's clone
git config user.name  codex
git config user.email codex@users.noreply.github.com

git config user.email   # verify — must print a noreply address
```

The email must always be a `@users.noreply.github.com` address. The name is
free to differ and should.

Repo-local on purpose; do not touch the global config. Before pushing, check
your unmerged commits:

```sh
git log --format='%h %an <%ae> | %cn <%ce>' origin/main..HEAD
```

If any commit shows a personal address, fix it *before* it reaches `main` —
`git rebase origin/main --exec 'git commit --amend --no-edit --reset-author'`
— then force-push your `agent2/*` branch. Never rewrite commits already on
`main`.

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
