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

1. **#276 — finish #272. The PR is open and REFUTED; a test fails on it.**
   `agent2/272-late-completion`. The design is right and almost every point of
   the spec landed as asked — the two-pass matcher is real, pass 2 is
   genuinely globally sorted by `(lag, score, start_time)` rather than
   per-workout greedy, and mutation checks confirm the new tests bite. It is
   held on one piece of collateral damage: `activities_on_date` was rewritten
   to delegate to the new `activities_between` (`db.py:3894-3921`), which
   deleted its no-cutoff fast path. That path used naive prefix matching on
   the stored UTC string; the replacement always converts through
   `to_user_timezone(...).date()`, so **which day an activity comes back on
   changed for every caller.** `races.py:466` (`_matching_activity`)
   deliberately looks up by the UTC race date and converts to local itself, so
   it now misses, and `tests/test_weight.py::test_a_zwift_race_weight_is_filed_on_the_local_date`
   fails deterministically — `1 failed, 3147 passed` on the branch, green on
   `main`. The fix is narrow: leave `activities_on_date` exactly as it is on
   `main`, fast path included, and let `activities_between` be a genuinely
   separate function. Sharing row-filtering internals is fine; changing an
   existing function's observable behaviour is not. Also
   `tests/test_completion.py:62` is vacuous — it still passes with backward
   matching fully enabled, because the fetch range never reaches a ride dated
   before the only workout; add a second, earlier workout so it bites. **Run
   the whole suite before pushing.** The 247-test focused subset is what hid
   this.

2. **#156's three product questions, and #277's items 3 to 9.** #156 is closed
   (PR #274, `2586f14`) but the questions it asked for *instead of defaults*
   were never answered — the only answer that exists anywhere is the string
   "every 15 minutes" in `settings.html:217-218`, as UI copy with no
   reasoning, and the reader-context expiry is not addressed at all. Answer
   all three in a comment on #156: when sync runs and why that interval, what
   the rider sees during a normal offline period, and what they see when a
   reader context expires after 300 s. Then take #277 items 3 to 9 — a silent
   no-op when the endpoint is edited without an invitation, enabling applied
   before enrollment succeeds, an unguarded requeue that kills the scheduler
   thread for every user, the full snapshot recomputation on every wake-up,
   the 1 Hz poll with sync off, and the inconsistent POST/redirect/GET that
   makes a browser refresh mint a fresh bearer code. **Item 1 is being
   handled separately and item 2 is an owner decision — do not take either.**

3. **#258's follow-ups from the #275 review.** #275 merged (`90bede6`) and all
   eight residuals are fixed, but the review left three worth acting on, and
   the first is real: **`Location` is compared as an exact string.**
   `LocalBackend.swift:191` requires `response.location == "/"`, and Starlette
   emits the relative `/` — but a reverse proxy in front of the desktop
   (nginx `proxy_redirect`, Apache `ProxyPassReverse`) may rewrite it to an
   absolute URL, after which the rider can never authenticate; every attempt
   is `unexpectedLanding`. Given that the settled deployment story is "any
   non-public TLS terminator, rider's choice", this is reachable. It fails
   closed, so it is availability rather than security. Fix:
   `URL(string: location, relativeTo: baseURL)`, then assert same-origin and
   `path == "/"`. The other two are recorded on the PR and are informational.
   **One iOS issue at a time** — `project.pbxproj` conflicts on concurrent
   edits. #258 stays open for the device run regardless.

4. **#249 — rotating full-suite test flakes. Re-scoped: do not spend more
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

**Done since this list was last written.** **Cloud sync is on.** **#274**
merged 2026-09-13 (`2586f14`) and closed **#156**: the desktop app enrols,
pairs, manages devices and pushes on a background scheduler with bounded
backoff, credentials in the OS keyring only, and sync state in schema v36
carrying no cloud secrets. Two things are recorded on #156 rather than here —
the kill-switch criterion now has one deliberate, commented exception
(revocation is allowed while sync is off), and the three product questions it
asked for are still unanswered. Residue is **#277**. **#268** merged
2026-09-13 (`6462204`): the iOS app can read the local desktop server as a
second backend over HTTPS, with connector-ticket auth, device-only Keychain
credentials and a snapshot cache kept separate from the cloud one. **#275**
merged the same day (`90bede6`) and fixed all eight of its residuals — a
transient failure no longer unpairs, the backend picker works and persists,
and the "read" backend no longer writes (it stops at the 303 instead of
following into `GET /`, which was running `retire_elapsed_ooto_adjustments`
and `apply_adaptations` on every phone authentication). #258 stays open for
the device run and the `Location` string-comparison follow-up. **#273** merged
(`d7ddbf0`): one-off workout exports are pruned on the same grace window as
plan workouts. **#269** merged
2026-09-13 (`e06280c`): the Zwift export manifest now prunes an uncompleted
plan workout more than `EXPORT_GRACE_DAYS` (7) days past, so a skipped session
stops lingering in Zwift's custom-workout list forever while a ride done a few
days late still finds its file — and **#272**, filed from that work, is the
completion side catching up to it. **#270** merged (`0ef56d9`): two workflows still claimed
hosted runners were blocked at the account level, which stopped being true
when this repo went public — `macos-release.yml` is re-enabled on
`macos-latest` and `ios-release.yml`'s false justification for its self-hosted
pin is replaced with the real trade-off. Its guard test moved with it:
`test_release_workflow_is_tag_only_and_hard_disabled` required the very `if:
${{ false }}` the change removes, so it now asserts the gate that actually
remains — `environment: macos-code-signing`. **Note for the next tag:** that
signed macOS release job has never run. Watch the first `v*` tag. The iOS screens all landed: **#161 as PR #253**,
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
