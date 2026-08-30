# Working agreement for two agents

Two agents work this repo: a Claude session and a codex agent. This file is the
contract between them. It exists so that any piece of work can be handed to
either one without rewriting it, and so that two agents working at once do not
corrupt each other's checkouts or each other's branches.

## 1. Assignment is a label, never something written into an issue

Every issue carries exactly one owner label:

| Label | Meaning |
|---|---|
| `agent:claude` | The Claude session is on it now |
| `agent:codex` | The codex agent is on it now |
| `agent:either` | Nobody is on it; no strong preference |

Reassignment is flipping the label. That is the whole mechanism. **No issue body
may name an agent**, describe an agent's strengths, or assume who will read it.
An issue that says "codex should be careful here" is broken and should be edited.

A second axis says whether it can be started:

| Label | Meaning |
|---|---|
| `ready` | Dependencies met, safe to start now |
| `blocked` | Waiting on another issue, named in the body. Do not start |

When an issue merges, whoever merged it removes `blocked` from everything that
was waiting on it. That is the only queue management this needs.

## 2. Every issue must be portable

An issue is only reassignable if a cold reader can execute it. Before an issue is
labelled `ready`, it must have:

- **Why** — the reason the work exists, not just the change requested. An agent
  that understands the reason makes better decisions at the edges than one
  following a spec.
- **Scope** — what to build, with the real file paths and `file:line` anchors.
- **Files in scope** — and, where it matters, files explicitly not to touch.
- **Done** — criteria a machine can check. A test that passes, a command that
  succeeds, a number that appears. Not "works correctly".
- **Dependencies** — named by issue number.

If you pick up an issue and it fails this bar, fix the issue first and say so in
a comment. Do not start guessing.

## 3. One issue, one branch, one worktree

Branch naming: `<agent>/<issue-number>-<slug>` — `claude/171-walking-skeleton`,
`codex/154-publish-objects`.

**One branch per issue. Never reuse a branch across issues.** A reused branch
accumulates unrelated commits, which makes a PR unreviewable and makes reverting
one change impossible without reverting others.

**Each agent works in its own git worktree.** Two writers in one checkout will
silently corrupt each other — one agent's `git checkout` moves the other's
working tree out from under it mid-edit.

## 4. Do not take two issues that touch the same files

Collisions are avoided by picking, not by merging. Current overlap groups — never
run two of these at once:

| Group | Issues | Shared surface |
|---|---|---|
| Snapshot/publish | #154, #157, #173 | `wattracker/cloud/snapshot.py` |
| Cloud auth | #151, #152, #153 | `wattracker/cloud/security.py`, `api.py` |
| Read plane | #155, #151 | `wattracker/cloud/api.py` |
| Bicep | #164, #165, #168, #170 | `infra/azure/main.bicep`, `tests/test_cloud_deployment.py` |
| Desktop settings | #156, #172 | `wattracker/server.py` settings routes, `db.py` schema |
| iOS shell | #158, #159, #160 | the Xcode project file, which merges badly |

Anything not in a group is independent and safe to run in parallel.

`.xcodeproj/project.pbxproj` deserves special mention: it is a generated file
that conflicts on almost every concurrent edit. Only one iOS issue at a time
until the project structure is stable.

## 5. Rebase before review, enforced

`main` requires branches to be up to date before merging. This is deliberate: a
PR built on a base from several days ago can pass its own tests and still break
`main`, and reviewing a stale diff wastes the reviewer's time on code that has
already changed underneath it.

Before asking for review: `git fetch origin && git rebase origin/main`, then
re-run the tests. A PR that has been sitting gets rebased again before merge, not
merged on the strength of a green run from Tuesday.

## 6. Handing work over mid-flight

Work moves between agents. When you stop before an issue is done, comment on the
issue with:

- the branch name and its base commit
- what is finished and verified
- what is left, concretely
- **every decision you made that is not in the issue body** — this is the part
  that is actually load-bearing. The next agent can read code; it cannot read
  why you chose the thing that looks wrong.
- anything you tried that did not work, so it is not tried again

Then flip the owner label. Do not delete the branch.

## 7. Repo-specific traps

These have all cost real time here before.

- **pytest in a worktree tests the wrong code.** Without the right `PYTHONPATH`,
  a worktree's test run silently imports the main checkout's package and passes
  against code you did not change. Verify what you are importing before trusting
  a green run in a worktree.
- **Ad-hoc scripts hit the real database.** `conftest.py` isolates pytest and
  nothing else. Any probe script, one-off migration, or debugging snippet runs
  against `~/.wattracker/wattracker.db` unless told otherwise. Back it up
  (`sqlite3.Connection.backup`, not `cp` — the app may be running) before any
  write, and use `mode=ro` for reads.
- **The installed pre-push hook goes stale** against `scripts/hooks/`. If the
  hook behaves unexpectedly, compare the installed copy before debugging the
  code it is complaining about.
- **The venv interpreter must be the uv arm64 build.** Do not rebuild `.venv`
  with a bare `python3.12`.
- **This repository is public.** No credentials, no signing material, no real
  email addresses in commits, issues, or code.

## 8. Which work suits which agent

Guidance for choosing, not a rule. Any issue can go to either.

The codex agent runs long, so it suits work that is large, well-specified and
mechanically checkable: bulk serialization, plumbing, screens built from a clear
spec, test sweeps, deployment configuration.

The Claude session holds the architecture and takes work where the specification
runs out and judgment starts: anything touching credentials, signing, key
handling or isolation; decisions with a cost or security consequence; the first
vertical slice through a new integration, where most of the value is discovering
what the spec should have said.

When in doubt, the question is not "who is better" but "does this issue still
make sense if the person reading it knows nothing about the last conversation".
If yes, either agent can take it. If no, fix the issue.
