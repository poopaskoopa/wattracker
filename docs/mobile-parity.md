# iOS and Android parity

The iOS app (`ios/`, Swift) and the Android app (`android/`, Kotlin) are two
native clients of one cloud read plane. They are meant to be the same product
on two platforms. Android kept falling behind because keeping it in step
depended on someone remembering to leave a comment on the epic, #192.

This file replaces that. It is the one place to read before Android work, and
the one place to update when a change alters what a rider sees on either app.
The earlier brief (the 2026-09-29 comment on #192, and the 2026-10-01 comments
on #195–#199) is folded in below; those comments stay as history.

## How the two apps stay in sync

**1. Every PR that touches shared surface says what it means for Android.**
`.github/workflows/mobile-parity.yml` fails any pull request that changes
`ios/`, `wattracker/cloud/` or `tests/vectors/` unless its description has a
line of its own in one of these forms:

```
Android: posted on #198
Android: n/a - server-only refactor, no client-visible change
```

The first names the Android issue or PR that has the note. The second needs a
reason; a bare `Android: n/a` fails. Editing the description re-runs the check.
The logic is `scripts/check_android_parity.py`.

**2. Notes go on the specific Android screen issue, not the epic.** Pairing is
#195, Dashboard #196, Activities #197, Calendar and Volume #198, release #199,
backend selection #282. A note says what changed on iOS, which iOS file to
read, the exact copy if any, and whether a vector covers it. Android issues are
assigned to taksmon; anyone else posts notes there and nothing more.

**3. Any rule both apps must agree on gets a shared vector.** If the two
clients have to compute the same answer (a grid, a normalization, a decision
on a status code), put cases in `tests/vectors/`, and have both the Swift and
the Kotlin tests read the same file. A rule written down twice in prose drifts;
a rule in one file that both suites read cannot. `android.yml` runs on
`tests/vectors/**` for this reason, so a changed vector always runs the Kotlin
half.

**4. This file is updated in the same PR** whenever a change alters
user-visible behaviour on either client, or moves a row in the matrix below.

## Matrix

iOS paths are under `ios/WatTracker/WatTracker/`. Android status was checked
against `android/` on `main` and PR #419 on 2026-10-01. "Pending" means not on
`main` and not in an open PR.

| Area | Behaviour | iOS source | Android issue | Android status |
|---|---|---|---|---|
| Pairing | Typed pairing code | `Screens/PairingScreen.swift`, `Cloud/PairingCode.swift` | #195 | Done (#376). **Differs:** Android refuses a code that fails local normalization and also strips `\r`/`\n`; iOS sends typed input as written and normalizes only scans. `pairing_code_v1.json` pins the shared rule |
| Pairing | QR scan, four camera states, local check of scanned codes | `Screens/QRCodeScanner.swift`, `Screens/PairingScreen.swift` | #195 | **Open decision** on #195: typed only (deliberate difference), Play services scanner, or CameraX plus a decoder. Manifest currently says "No camera" |
| Settings | Remove this device: on 401/404 with a `Date` within clock tolerance, finish locally; keep the credential on a missing or skewed `Date`, 429/503 or offline (#410) | `Cloud/CloudSession.swift` (`removeDevice`, `serverNoLongerKnowsCredential`) | #195 | In PR #419. `main` signs out on any 404 and ignores `Date` and 401 |
| Settings | Revoked from the desktop shows "Device removed", not unpaired (#153) | `Cloud/CloudSession.swift` (`markRemoved`), `Shell/AppGate.swift` | #195 | Done (#376, `markRemovedState`) |
| Transport | Cold start of the scale-to-zero read app: 60/120 s timeouts, one retry after 3 s, a 10 s waking gate that never shortens a stronger gate or counts toward removal, "Waking up the cloud…" after 5 s (#422) | `Cloud/CloudSession.swift`, `Cloud/CloudTransport.swift`, `Theme/Panel.swift` | #196 | Pending. Android is still 15 s connect / 30 s read (`CloudTransport.kt`); raised in #419's review |
| Transport | Pull to refresh lifts only a cold-start gate (`retryNowIfWaking`); detail and stream reads get the retry and waking gate | `Screens/ActivitiesModel.swift` | #196–#198 | Pending, and pending on iOS too (#429) except Activities pull to refresh |
| Transport | 413 `collection_too_large` on unpaginated routes keeps the cache (#379) | `Cloud/CloudSession.swift` | #196 | In PR #419 (test pins it) |
| Dashboard | Form/fitness/fatigue, training load, power curve; progress over ~15 pages; load window measured from the newest load point, not the clock | `Screens/DashboardScreen.swift`, `Cloud/MobileScreenData.swift` | #196 | In PR #419 |
| Dashboard | Zone `pct` is a display string (`"<56%"`, `"56–75%"`), top zone `max` may be null (#412) | `Cloud/CloudModels.swift` | #196 | In PR #419 (`main` decodes it as `Double`) |
| Activities | List and detail; two panes only on regular-width iPad; bounded detail/stream cache cleared on revoke (#351, #358); a 404 on streams is "No recorded streams" | `Screens/ActivitiesScreen.swift`, `Screens/ActivitiesModel.swift` | #197 | Pending (stub screen) |
| Calendar | Paginated delta route with tombstones (#386) | `Cloud/CloudSession.swift` | #198 | In PR #419 (`servesDeltas`) |
| Calendar | Full Monday–Sunday weeks with adjacent months' real dates, date-only UTC arithmetic, cells keyed by ISO date (#431) | `Cloud/MobileScreenData.swift` (`CalendarMonth.gridDates()`), `Screens/CalendarScreen.swift` | #198 | Pending (stub screen). `calendar_grid_v1.json` pins the grid |
| Calendar | Day view: workout power graph, step list, "at FTP N W", back arrow (#413/#416) | `Screens/CalendarScreen.swift` | #198 | `profile` decoding in PR #419; the screen is pending |
| Volume | Hours / TSS / Distance / Calories × last 4 weeks / 12 weeks / year to date | `Screens/VolumeScreen.swift`, `Cloud/MobileScreenData.swift` | #198 | Pending (stub screen) |
| Backend | Rider picks local desktop or cloud | `Shell/SessionGate.swift`, `Screens/SettingsScreen.swift` | #282 | Done: manual picker in `SettingsScreen.kt` |
| Backend | Prefer the paired desktop when reachable, cloud otherwise (#281) | `Cloud/LocalBackend.swift`, `Shell/SessionGate.swift` | #282 | Pending. iOS code is in; #281 stays open for the device check |
| Release | Real cloud host from a gitignored per-machine file, never from the tree (#420). iOS: `Release.xcconfig` does an optional `#include?` of the gitignored `Production.local.xcconfig` | `ios/WatTracker/Config/` | #199 | In PR #419: `-PwattrackerCloudAuthority` or `local.properties`, keeping the build's refusal to ship the `.example` placeholder `releaseCloudAuthority` |
| Release | App shown as `wattracker`, store identity unchanged (#427) | `Info.plist` (`CFBundleDisplayName`) | #199 | iOS now uses `wattracker`; Android remains pending and must not change `applicationId` |

## Shared vectors

Python generates or checks each file; the mobile suites read the same file
from the repository root. A Kotlin cell marked pending means the Android code
that would consume the vector does not exist on `main` yet.

| File | Pins | Python | Swift | Kotlin |
|---|---|---|---|---|
| `canonical_request_v1.json` | Canonical request bytes and ECDSA signatures | `tests/test_canonical_request_vectors.py` (`scripts/generate_canonical_vectors.py`) | `CanonicalRequestVectorTests.swift` | `CanonicalRequestVectorTest.kt`, `EcdsaTest.kt` |
| `cloud_objects_v1.json` | The published object kinds and their shapes | `tests/test_cloud_api.py` | `CloudModelsTests.swift` | `CloudObjectsTest.kt` (real zone strings arrive with PR #419) |
| `calendar_day_profile.json` | `calendar_day` workout `profile` from the real publisher; drift-tested | `tests/test_cloud_snapshot_derived.py` | `CloudModelsTests.swift` | Pending: consumed by PR #419 |
| `calendar_grid_v1.json` | Monday–Sunday month grid; agrees with the desktop's `/api/calendar` | `tests/test_behaviour_vectors.py` (`scripts/generate_behaviour_vectors.py`) | `BehaviourVectorTests.swift` | Pending: #198 must read it |
| `pairing_code_v1.json` | Pairing code normalization; `client_may_accept` cases allow a client to also strip `\r`/`\n` | `tests/test_behaviour_vectors.py` (`scripts/generate_behaviour_vectors.py`) | `BehaviourVectorTests.swift` | `PairingCodeVectorTest.kt` |
| `removal_decision_v1.json` | Remove-device decision on 401/404 and `Date` skew (240 s, inside the server's 300 s) | `tests/test_behaviour_vectors.py` (`scripts/generate_behaviour_vectors.py`) | `BehaviourVectorTests.swift` (`CloudSession.removalCompletesLocally`) | Pending: the rule lands with PR #419 |
