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

## The work queue — take the top unblocked item

Pick work from this list, top down. Do not pick by interest, and do not start
something not on it without saying so first. The order is not arbitrary:
each entry says what it unblocks, and taking them out of order produces
branches that cannot be verified or merged.

The list gives the **order**. GitHub gives the **state** — always
`gh issue view <n>` before starting, because an issue's body gets amended
(#234's scope grew a whole section after it was filed) and its labels move.
If the two disagree, GitHub wins and the queue is stale; say so.

1. **#298 — flaky iOS `SessionGateTests` test.** It failed CI three times on
   2026-09-14, each time on a Python-only PR (#296, #297, #301). The failures
   are at `SessionGateTests.swift:413/414/420`, where `backend` reads `cloud`
   and `phase` reads `paired` after an explicit `.local` selection. The spec is
   in the issue comment. The lead is a single `Task.yield()` standing in for an
   awaited automatic re-evaluation; `b53ed90` fixed the sibling test the same
   way. **Answer first whether it is test-only or production.** If a late
   automatic re-evaluation can override an explicit selection in the app, #281's
   contract is broken and the fix goes in `SessionGate`. Do not add yields or
   sleeps. The done criterion is 50 consecutive green iterations of that one
   test, with the count reported.

2. **#300 — Windows docs still describe the self-hosted runner.** #299 moved
   `windows-real` and `package-unsigned` to `windows-latest`, but
   `docs/windows-packaging.md` and `README.md:563` still describe the old setup.
   Mechanical, and Python/docs only, so it is safe to run alongside #298.
   Mark anything kept about the self-hosted runner as history; don't delete
   the reasoning. The cloud macOS jobs are still self-hosted, so the README
   line may be only partly wrong.

3. **#249 — rotating full-suite test flakes. Still not a local-runs job.**
   Nothing has changed since the re-scope. 21 consecutive clean local full
   suites stand against zero reproductions. Both known instances came from
   **taksmon's machine**. The mechanism is known: the session goes missing
   mid-test, `AuthMiddleware` 303s to `/welcome`, and the assertion reads the
   wrong page. The cause is not known. PR #261's `_register()` assertions and
   PR #265's env isolation have ruled out two classes of cause. **The next step
   is taksmon's log and his exact invocation.** Until he provides them, skip
   this item rather than spending runs on it. Sweep the 37 other `_register`
   helpers only once the cause is known.

**When the list runs out, stop and say so.** Do not pick up unlabelled issues
or anything below on your own.

**Not codex work, do not start:**

- **#258, #281, #161, #162, #163, #247** are open *only* for a physical-device
  check, which no agent can perform. Their code is on `main`: #258's two
  origin test vectors landed in #301, and #281's local-first selection landed
  in #295 (`0546364`).
- **#264** waits on #194 (Android network client). #302 closed it by accident;
  it was reopened. Its Android half is taksmon's.
- **#102, #168, #169, #170, #217, #242** are `blocked` on the hosting decision
  or a live deployment.

**Done since this list was last written (2026-09-13 → 09-14).**
- #277 is closed. Items 3–9 landed in #283 (`42966db`) once both review
  blockers were fixed; the capture now happens inside the `BEGIN IMMEDIATE`
  that records success.
- #291, the snapshot-gate baseline outliving its connection, landed in #297
  (`72ac394`).
- #280, legacy completions for a non-UTC rider, landed in #292 (`2be58e5`).
  #288, calendar and manual completion by rider-local day, landed in #296
  (`cf0eeb0`).
- #156's third product answer is posted: expiry is a proactive refresh the
  rider never sees.
- #281, prefer the local desktop, landed in #295 (`0546364`).
- #290, Windows CI on hosted runners, landed in #299 (`375491d`).
- #302 clarified the scope of the iOS device-validation doc.

Older history is in `git log` and on the issues.

**Pairing is wired on `main`.** `CloudClient.pair(code:)` →
`SessionGate.pair(code:label:)` → `PairingScreen`.

**The iOS app can be run against a real server on a device.**
`scripts/walking_skeleton_server.py --lan` enrols a writer, publishes a full
snapshot, mints pairing codes and writes the Mac's LAN address to
`ios/WatTracker/Config/Local.xcconfig`. `docs/ios-device-validation.md` is the
checklist and carries the traps — a 900-second code, and remints that wipe the
in-memory store along with the revoked-device record.

**The Android epic (#192-#199, plus #259 and #260) is taksmon's** by GitHub
assignee and is not on this queue.

**One iOS issue at a time.** `project.pbxproj` conflicts on almost every
concurrent edit — see `docs/agent-workflow.md` §4. This is why #258 waited for
#256 rather than running alongside it.

### Before starting any iOS issue that already has a PR

Diff the existing PR's files against `main` **two-dot** (`git diff
origin/main <ref>`), not three-dot. A three-dot diff measures from the merge
base, so on a stale branch it presents already-merged work as new. PR #235 was
opened carrying an `api.py` change and 83 test lines that had merged three and
a half hours earlier as PR #237 (`9e1b3d2`); a two-dot diff against `main` is
empty for those files and would have caught it immediately.

PR #235 also re-implements the actor-reentrancy fix that PR #228 already
contains, under a different name — `sessionGeneration` against #228's
`lifecycleGeneration`, each with its own `refreshTaskID`. The two conflict on
`CloudSession.swift` and it is not a rebase conflict: whichever lands second
must drop its own counter and adopt the other's, then re-verify its reads.
Keep #228's, which was reviewed line by line. Two generation counters guarding
one actor is not a merge, it is a bug.

Both PRs are now closed — #253 and #254 re-applied that work onto `main`
instead. The two-dot rule above is the part to carry forward; the #235/#228
collision is kept as the worked example of why it matters.

### Say whether you ran it on a device

This section used to explain why #162 and #163 were held back: nothing in the
app target called `CloudSession.pair(code:)`, so the keychain was never
written and every screen rendered its empty state on a device. **That gap is
closed** — pairing is wired on `main` (`CloudClient.pair` → `SessionGate.pair`
→ `PairingScreen`) and #234 landed the harness to pair against.

The rule that outlived it: a screen that passes CI has not been shown to work.
Green `ios-tests` cannot see an unreachable screen, and it could not see the
sideways QR preview a device run caught on 2026-09-06. **If you build a
screen, say plainly whether you ran it on a device or only in CI.** That
disclosure is what lets the review catch what the tests cannot — and it is why
#161, #162 and #247 stay open after their code has merged.

### When an issue's premise has gone stale

Issue bodies are written at a point in time and `main` moves. #217 says the
`containerized` job is `parked at if: ${{ false }}` and that
`Dockerfile.cloud` "has never been built anywhere" — both were true when it
was filed and neither is true now: that job runs on `ubuntu-latest` on every
PR and already builds the image and verifies the `cloud` extra imports inside
it. #102 argues at length against an APIM cost that #213 removed from
`main.bicep` entirely.

So: **check the claims an issue rests on before implementing against them.**
When one has gone stale, say so in the PR description and scope to what is
actually left. Do not silently redo work that has landed, and do not invent
replacement scope to fill the gap — a smaller PR that says why it is smaller
is the right outcome.

### Finishing an item

- Report **"ready to merge"** and stop. Only the integrator merges, and only
  the integrator pushes to `main`.
- Say plainly what you could not verify. "Simulator execution unavailable in
  this environment" on #228 was the right disclosure and it is what let the
  review catch that the screen is unreachable on a device.
- If the PR does not close its issue, say so in the description and why —
  #219 did this correctly against #217.
- Do not start the next item while your PR is unreviewed *if* the next item
  touches the same files. Otherwise carry on; note the dependency in the PR.

## Scope

- Partition by feature, not by file. Cross-branch edits to shared modules
  (`db.py`, `server.py`) are fine — git merges them; simultaneous edits to one
  tree are not.
