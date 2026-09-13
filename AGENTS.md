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

1. **#283's two defects — finish #277.** The PR is open on
   `agent2/277-cloud-sync-followups` and blocked. Items 3 to 9 are all
   implemented and mutation-checked and the suite is green at 3,207 — but two
   of the new behaviours fail *silently*, which is the shape of bug that batch
   of work existed to remove.

   **The snapshot gate can permanently swallow a push.**
   `desktop_sync.py:427-430` calls `_snapshot_gate_mark_current` *after* the
   push and after `_record_success` has already written, so anything another
   connection committed between the snapshot read and that `PRAGMA
   data_version` read is absorbed into the baseline and never sent. The window
   is not small: `_all_snapshot_objects` (`cloud/snapshot.py:1269-1287`) opens
   its own `readonly_connection` per page, so the snapshot read is not one
   consistent transaction. On `main` the same non-atomicity is harmless
   because the next cycle rebuilds unconditionally; the gate turns it sticky,
   and since the 900 s cycle no longer writes while the gate holds, it does
   not self-heal. Reproduced: an activity committed after the final "nothing
   to send" read stays unsynced across three further cycles. Fix by taking the
   change token inside the same write transaction that records success
   (`BEGIN IMMEDIATE` serializes against other writers), or by keying the gate
   on a content token scoped to the user's source tables rather than a
   whole-file `data_version` that the sync plane's own writes also bump.

   **The endpoint guard can silently discard the kill switch.**
   `server.py:4930-4938` returns before `sync.set_enabled(...)`, so unchecking
   "Enable cloud sync" *while also* editing the endpoint throws the disable
   away and leaves sync on, with no message saying so. That is a regression
   against `main` and it lands on #156's safety control, which the owner has
   just finished adjudicating (#277 item 2). Turning sync **off** is never
   gated: apply `set_enabled(uid, False)` ahead of the guard when the box is
   unchecked. `tests/test_desktop_cloud_routes.py:86-107` currently pins the
   wrong behaviour and must be corrected, not kept.

   Item 5 — the scheduler requeue — is genuinely fixed and needs no rework;
   the evidence is in the review comment. Item 7 has no lost-notification
   race. Do not re-derive either.

2. **#156's third product answer, re-posted.** One comment, and it is owed.
   The first two answers are good and recorded. The third answers the question
   as it was asked — "what does the rider see when a reader context expires
   **with no refresh endpoint**" — but that parenthetical was wrong, and came
   from #102's stale body. `POST /api/v1/context/refresh` exists
   (`cloud/api.py:1056`, ungated) and `CloudClient.swift:172` already calls
   it. So expiry is a routine refresh the rider never sees; the behaviour
   described belongs to the *refresh-failure* path. Re-label it, and confirm
   the client refreshes **proactively** rather than after a failure —
   reactive-only would stall the rider every five minutes on a good network.

3. **#280 — legacy completions read as unverified for a non-UTC rider.**
   `plan_workout_completion_verified` (`importer.py:1229-1263`) compares the
   activity's rider-local date against a stored `completed_date` that legacy
   rows derived from UTC-prefix matching, so pre-existing rows flip to
   unverified and drop out of RPE evidence feeding FTP re-evaluation. **This
   rider is UTC-4**, where the stored UTC date runs one day *ahead* of local
   for rides between 20:00 and 23:59 local — prime indoor-trainer time, so the
   affected share of history is large rather than an edge case. Decide backfill
   versus a tolerance keyed to pre-upgrade rows; the issue lays out both. A
   backfill rewrites stored user data, so it wants the mandatory pre-migration
   backup and an idempotence test.

4. **#281 — iOS: prefer the rider's own desktop when reachable.** Owner
   request. The identity question is already answered — the connector token
   *is* the proof, since `device_for_token` resolves the owning user from the
   token hash — and the phone already stores the local `baseURL` from pairing,
   so this is a reachability probe on an endpoint already trusted, **not**
   discovery. mDNS is explicitly out of scope: there is no publicly-valid
   certificate for a `.local` name, so discovering arbitrary hosts forces
   pinning or trust-on-first-use, both of which #192 rejected. The hard
   constraint is that the phone must never present its connector token to a
   host it has not already paired with — that credential is a full desktop
   session, not a read-scoped one. **One iOS issue at a time**; this touches
   `project.pbxproj`, so do not run it alongside other iOS work. #280 is
   Python and is the safe thing to run in parallel.

5. **#258's two test vectors.** Not a PR of their own — fold into whichever
   iOS change comes next. The rejected-landing list at
   `LocalBackendTests.swift:89-101` shares no substring with the base host, so
   weakening the same-origin host check to `hasSuffix` passes all 12 existing
   tests; adding `https://desktop.example.evil.com/` and
   `https://evil-desktop.example/` closes that. And every test base is
   `https://desktop.example`, so the port comparison never runs with a
   non-nil port — a `https://desktop.example:8443` base covers it.

6. **#249 — rotating full-suite test flakes. Re-scoped: do not spend more
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

**Done since this list was last written.** **#276** merged (`ed39ebb`) and
closed **#272**: a plan workout ridden late is now matched within a 7-day
forward grace window, by a two-pass matcher that keeps same-day first refusal
by construction and assigns late pairs globally by `(lag, score, start_time)`.
It was refuted once first — the original rewrote `activities_on_date` to
delegate to the new range helper, which dropped its naive-prefix fast path and
silently changed which day an activity is returned on for *every* caller,
breaking race weigh-in attribution. That is **#280**, still open: the same
date-rule change leaves legacy completions reading as unverified. **#284**
merged (`b1d8fcf`): the local iOS backend now accepts a proxy-rewritten
absolute `Location` on the auth landing, resolved against the configured
`baseURL` and checked for same-origin plus a root path — verified against 82
hostile vectors. **#279** merged (`a1e8d44`): revoking a lost device is
reachable with sync off, which is what made #156's kill-switch exception
worth having. **Cloud sync is on.** **#274**
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
