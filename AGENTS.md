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

1. **#268's review items — finish #258.** The PR is open on
   `agent2/258-local-backend` and its blocker is already fixed (`5f81f22`
   added `LocalBackend.swift` to the test target, so `xcodebuild test`
   compiles). What is left is behaviour, and the first one is the reason this
   is the top item rather than a follow-up: **a single 403 destroys the local
   pairing.** `LocalBackend.swift:253` folds 403 into `.unauthorized`,
   `markRemoved()` clears the Keychain credential *and* the snapshot cache,
   and `ensureAuthenticated()` at line 530 sits outside the retry `do/catch`
   so nothing is retried. The docs that ship in the same PR tell the rider to
   put a TLS terminator in front of the server — and a terminator doing its
   own access control returns 403. The cloud path deliberately requires two
   refusals with backoff for exactly this reason; the local path must hold the
   same line. Then the four backend-switching gaps (dead Settings picker,
   stale screen data after a switch, local removal stranding a valid cloud
   pairing, choice not persisted), empty Calendar month navigation on local,
   and two minor items. All nine are in the review comment on the PR.

2. **#156 — turn cloud sync on in the desktop app.** **Unblocked and assigned
   to codex on 2026-09-13 (owner decision): this is where cloud-sync work
   starts.** Nothing in the application imports `wattracker.cloud` today, and
   that is the single biggest reason #102 is still deferred — until the
   desktop actually pushes, the phone has nothing to read. The `blocked` label
   was for the device half and is satisfied: pairing is wired on `main`.

   **Hosting stays deferred on purpose, and does not block this.** The client
   half (`wattracker/cloud/client.py`) is pure stdlib and talks to whatever
   host it is pointed at; `scripts/walking_skeleton_server.py` is a real
   server to develop against. Only the *server* half is Azure-locked
   (`runtime.py` demands `AzureTenantStore` on Blob + Table). **Do not add an
   Azure dependency to the desktop app.**

   Three constraints that are not negotiable: credentials go only through
   `CloudCredentialStore` (OS keychain, no plaintext fallback) and the "never
   hits the DB or a config file" criterion wants a test that greps the files;
   the kill switch must stop outbound traffic immediately, tested while a push
   is queued rather than from idle; and the existing "local app is unmodified
   / not cloud-dependent" test must still pass **unedited**. See the
   assignment comment on the issue for the product questions to answer there
   rather than inventing defaults.

3. **#249 — rotating full-suite test flakes. Re-scoped: do not spend more
   local runs on it.** 1 to 4 failures per full run, a different test each
   time, each green in isolation and under heavy synthetic CPU load.
   Both reported instances were observed on **taksmon's machine**; 21
   consecutive clean local full suites (12 parallel with CI's exact
   invocation, 4 serial, plus 5 more) stand against zero local reproductions,
   which is not compatible with a 1–4-per-run rate on this hardware. The
   mechanism is known — the session goes missing mid-test, `AuthMiddleware`
   303s to `/welcome`, `TestClient` follows, and the assertion reads a 200 of
   the wrong page — but not the cause. Two classes of cause are now dead: PR
   #261 made the four implicated `_register()` helpers assert the 303 and
   probe an authenticated endpoint, and PR #265 put ten more variables the app
   reads into `conftest.isolated_env` with `tests/test_env_isolation.py`
   guarding the list, so a surviving flake is not a leaked env var. **The next
   step is taksmon's log and his exact invocation, not another local run.**
   The 37 other files with `_register` helpers that discard their response get
   swept once the cause is known, not before.

**Done since this list was last written.** **#269** merged 2026-09-13
(`e06280c`): the Zwift export manifest now prunes an uncompleted plan workout
more than `EXPORT_GRACE_DAYS` (7) days past, so a skipped session stops
lingering in Zwift's custom-workout list forever while a ride done a few days
late still finds its file. **#270** is open: two workflows still claimed
hosted runners were blocked at the account level, which stopped being true
when this repo went public — `macos-release.yml` is re-enabled and
`ios-release.yml`'s false justification for its self-hosted pin is replaced
with the real trade-off. The iOS screens all landed: **#161 as PR #253**,
**#162 as PR #254** and **#163 as PR #256** (PRs #228 and #235 closed as
superseded). **#193 landed as PR #257** — the repository now has an `android/`
tree. #234 merged as PR #240 after an on-device run; #233 landed in PR #238;
#217's credential-free half landed in PR #239, its remainder gated on #102.
#167 was closed 2026-09-05.

**Pairing is wired on `main`.** `CloudClient.pair(code:)` →
`SessionGate.pair(code:label:)` → `PairingScreen`.

**The iOS app can be run against a real server on a device.**
`scripts/walking_skeleton_server.py --lan` enrols a writer, publishes a full
snapshot, mints pairing codes and writes the Mac's LAN address to
`ios/WatTracker/Config/Local.xcconfig`. `docs/ios-device-validation.md` is the
checklist and carries the traps — a 900-second code, and remints that wipe the
in-memory store along with the revoked-device record.

**Do not start #161, #162, #163 or #247.** All four are open *only* for an
on-device check, which no agent can perform. Their screens are on `main`.
Starting one re-implements code that already exists.

**Do not start #169** without asking; it is `blocked`. #242 is `blocked` on
#102 and is a measurement of a live deployment, not code. **#217** needs a
real deployment; its body's claims about `containerized` being parked and
`Dockerfile.cloud` never being built are both stale.

4. **#264 — one device-validation procedure for iOS and Android.** Unassigned
   and cross-cutting: the harness, both clients, and the validation doc.
   Android has no on-device path at all today. Ask before taking it — it
   spans both epics and its Android half is taksmon's territory.

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
