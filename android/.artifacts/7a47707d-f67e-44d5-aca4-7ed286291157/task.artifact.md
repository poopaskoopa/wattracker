# PR #419 review — 8 findings (round 2)

Addressing the code-review findings on the three local commits
(`a847c66`, `ec7e84d`, `b18a69b`) of `feature/android-client-4`.

- [ ] **#1 Waking spinner dead-end** — add pull-to-refresh (iOS parity) to the dashboard; wire `retryNowIfWaking()`/`mayBeWaking`; make the no-data waking state a static notice (no perpetual spinner). *(user chose pull-to-refresh)*
- [ ] **#2 Stale tiles survive a re-pair** — clear `dashboardData` when identity changes at the top of `DashboardViewModel.load()`; stop carrying the previous identity's rides into the fresh snapshot.
- [ ] **#3 Dropped-connection arm** — `CloudSession.isColdStart`: match `SSLException` and unwrap the cause chain (HTTPS reset surfaces wrapped).
- [ ] **#4 Refresh retry not lifecycle-guarded** — re-check `isCurrent(...)` inside the `ridingOutColdStart` retry closure and before `noteColdStart`/`noteFailure`.
- [ ] **#5 Missing tests** — positive `retryNowIfWaking()` lift; `isWaking`-set; deadline throws `SocketTimeoutException`; strengthen `aSignOutDuringTheCachedReadIsNotPainted`; plus tests for #3/#4.
- [ ] **#6 Connect timeout 60s** — leave the value (parity), add a comment naming the dead-network cost.
- [ ] **#7 `cached()` check-then-read** — re-validate identity after the disk read so the guarantee holds for all callers.
- [ ] **#8 Docs/hygiene** — (a) PR body (push-time), (b) attribute the "Agent tooling notes" section, (c) fix the unreachable `Failure.Waking` fallback (localize via `dashboard_waking_notice`).

## Verification
- [ ] `:app:testDebugUnitTest` green
- [ ] `:app:assembleDebug` green
- [ ] Walkthrough artifact

## Open items to confirm with user
- Commit / git strategy (rebase to fix `b18a69b` message attribution vs. stack new commits + `--force-with-lease`).
- PR body update at push time.
