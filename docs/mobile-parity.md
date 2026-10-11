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
| Transport | Pull to refresh lifts only a cold-start gate (`retryNowIfWaking`); detail and stream reads get the retry and waking gate | `Screens/ActivitiesModel.swift`, `Screens/ActivitiesScreen.swift`, `Screens/CalendarScreen.swift`, `Cloud/CloudSession.swift` | #196–#198 | iOS done (#429); Android pending |
| Transport | 413 `collection_too_large` on unpaginated routes keeps the cache (#379) | `Cloud/CloudSession.swift` | #196 | In PR #419 (test pins it) |
| Dashboard | Form/fitness/fatigue, training load, power curve; progress over ~15 pages; load window measured from the newest load point, not the clock | `Screens/DashboardScreen.swift`, `Cloud/MobileScreenData.swift` | #196 | In PR #419 |
| Dashboard | Zone `pct` is a display string (`"<56%"`, `"56–75%"`), top zone `max` may be null (#412) | `Cloud/CloudModels.swift` | #196 | In PR #419 (`main` decodes it as `Double`) |
| Activities | List and detail; two panes only on regular-width iPad; bounded detail/stream cache cleared on revoke (#351, #358); desktop publishes bounded/downsampled stream objects for every ride with a plottable power, heart-rate, cadence, or altitude channel; the 512 KiB decoded-input guard remains for derived reads and a separate 4 MiB stream-only fallback handles larger sources; a ride without plottable streams still maps 404 to "No recorded streams" | `Screens/ActivitiesScreen.swift`, `Screens/ActivitiesModel.swift`, `Cloud/CloudClient.swift` | #197 | iOS publisher/decoder contract in PR #445; Android note on #197, including the 404 → "No recorded streams" mapping for rides without streams |
| Activities | **Ride graphs**: one combined chart, not per-channel charts; spec in [Ride graphs chart](#ride-graphs-chart) (#447, #448) | `Screens/ActivitiesScreen.swift` (`StreamCharts`), `Screens/ActivitiesModel.swift` (`CombinedChartSeries`, `MinutesClock`, `ChartAxis`, `ChartTicks`) | #197 | **Android gap to build.** Must look like iOS; do not copy the desktop palette |
| Calendar | Ride detail shows the same Ride graphs chart, fed `duration_s` from the activity payload (#447, #448) | `Screens/CalendarScreen.swift` (`StreamCharts(streams:durationSeconds:)`) | #198 | **Android gap to build** (screen is pending anyway) |
| Calendar | Paginated delta route with tombstones (#386) | `Cloud/CloudSession.swift` | #198 | In PR #419 (`servesDeltas`) |
| Calendar | Full Monday–Sunday weeks with adjacent months' real dates, date-only UTC arithmetic, cells keyed by ISO date (#431) | `Cloud/MobileScreenData.swift` (`CalendarMonth.gridDates()`), `Screens/CalendarScreen.swift` | #198 | Pending (stub screen). `calendar_grid_v1.json` pins the grid |
| Calendar | Day view: workout power graph, step list, "at FTP N W", back arrow (#413/#416) | `Screens/CalendarScreen.swift` | #198 | `profile` decoding in PR #419; the screen is pending |
| Volume | Hours / TSS / Distance / Calories × last 4 weeks / 12 weeks / year to date | `Screens/VolumeScreen.swift`, `Cloud/MobileScreenData.swift` | #198 | Pending (stub screen) |
| Backend | Rider picks local desktop or cloud | `Shell/SessionGate.swift`, `Screens/SettingsScreen.swift` | #282 | Done: manual picker in `SettingsScreen.kt` |
| Backend | Prefer the paired desktop when reachable, cloud otherwise (#281) | `Cloud/LocalBackend.swift`, `Shell/SessionGate.swift` | #282 | Pending. iOS code is in; #281 stays open for the device check |
| Release | Real cloud host from a gitignored per-machine file, never from the tree (#420). iOS: `Release.xcconfig` does an optional `#include?` of the gitignored `Production.local.xcconfig` | `ios/WatTracker/Config/` | #199 | In PR #419: `-PwattrackerCloudAuthority` or `local.properties`, keeping the build's refusal to ship the `.example` placeholder `releaseCloudAuthority` |
| Release | App shown as `wattracker`, store identity unchanged (#427) | `Info.plist` (`CFBundleDisplayName`) | #199 | iOS now uses `wattracker`; Android remains pending and must not change `applicationId` |

## Ride graphs chart

Activities ride detail and Calendar ride detail show **one** combined chart
titled "Ride graphs" inside a panel. There are no per-channel charts. Android
is to be built to look like iOS. Sources of truth: iOS `StreamCharts`
(`Screens/ActivitiesScreen.swift`), `CombinedChartSeries`, `MinutesClock`,
`ChartAxis` and `ChartTicks` (`Screens/ActivitiesModel.swift`); the desktop
reference is `renderActivityDetail` in `wattracker/web/static/app.js`. Where
iOS and desktop differ (colours), follow iOS.

Chart height 220 pt. Under the title, a caption row: power axis title ("W") at
the left in the power colour, shared axis title ("bpm / rpm") at the right in
`muted`; each is empty when that axis is not shown. Under the chart: the
caption "Time (min). Altitude is a faint background without an axis." in
`muted`, then the legend.

### Series, order and style

Drawn in this order (first is behind): altitude, power, heart rate, cadence.
A channel that is absent, empty or all nil/non-finite is dropped (nil samples
are skipped individually). If no series remains, the chart is not shown (iOS
shows the loading or "No recorded streams" state instead).

| Series | Legend title | Colour (iOS token, hex) | Style | Scale |
|---|---|---|---|---|
| Altitude | Altitude (m) | `Palette.muted` `#8a94a0` | Filled area, fill opacity 0.18, plus 1 pt line at opacity 0.5; drawn first | Own min..max of the altitude values; no axis, no ticks |
| Power | Power (W) | `Palette.accent` `#f2a900` | 1.5 pt line | Left axis, zero-based, "W" |
| Heart rate | Heart rate (bpm) | `Palette.hr` `#d55181` | 1.5 pt line | Right axis, zero-based, shared with cadence, "bpm / rpm" |
| Cadence | Cadence (rpm) | `Palette.ok` `#4caf7d` | 1.5 pt line | Right axis, shared with heart rate |

Other tokens: axis tick labels `Palette.muted`, grid lines
`Palette.surfaceBorder` `#2a333d`, legend text `Palette.text` `#e6e6e6`, title
`Palette.textBright` `#f5f7fa`. Lines are linear (no smoothing) and have no
point markers.

Desktop hex differs and must not be copied: the web chart uses `--s-2`
`#c98500` for power and `--s-3` `#3987e5` for cadence. Heart rate
(`--s-hr` `#d55181`) and the altitude/context grey (`--s-context` `#8a94a0`)
match. The desktop altitude fill/border opacities (0.18 / 0.5) are the same.

### Scaling

Every series is plotted on a shared internal 0..1 vertical band, and axis tick
labels are mapped back to real values (`value = lower + position * (upper - lower)`).

- Power axis is built from every power sample; right axis from every heart-rate
  and cadence sample together. "Nice" ticks: about 5 intervals with a 1/2/5 x 10^n
  step; bounds enclose the data, always span a positive range. Zero-based means
  the lower bound is `min(0, data min)`.
- Axes are built from **all** series on that side whether or not they are hidden,
  so toggling the legend never rescales the plot.
- An axis (labels and title) disappears when all of its series are hidden. Grid
  lines come from the power axis, or from the right axis when power is hidden.
- Altitude: `(v - min) / (max - min)`; a flat altitude (max == min) plots at 0.5.
- Other series with a zero span cannot occur on an axis (bounds always span a
  positive range); a constant series plots as a flat line at its value, so an
  all-zero series is flat at the bottom.
- Negative values: iOS does not clip. A negative sample extends the axis floor
  below zero. (If Android wants clipping, that is a deliberate difference to be
  agreed first.)
- A single sample draws nothing visible (a one-point line has no segment).

### X axis: true minutes

Domain is `0 ... ride duration` in minutes (maximum plotted minute across all
series, never less than 1/60). Tick step is the first of
1, 2, 5, 10, 15, 20, 30, 60, 120, 240 minutes with `minutes / step <= 6` (480
if none), ticks at `0, step, 2*step ...` up to the duration. Labels are plain
numbers; one decimal place only when not whole.

**Data trap: the published `time` channel is effectively all nil.** The desktop
stores ISO timestamp strings and `_downsample` in
`wattracker/cloud/snapshot.py` only handles numbers, so clients cannot use the
sample index or the time channel. iOS `MinutesClock` rules, in order:

1. If the `time` channel has any finite numeric value, use each sample's time
   (seconds) / 60. A sample without its own time falls back to its index as seconds.
2. Else, if the ride duration (seconds, finite, > 0) is known and the stream
   has more than one sample, spread samples evenly:
   `minutes = index / (count - 1) * duration_s / 60`, where `count` is the
   longest channel length (indexes are positions in the original array, so
   dropped nil samples leave gaps rather than shifting the rest).
3. Else assume 1 Hz: `minutes = index / 60`.

The ride duration is `duration_s` of the activity summary (Activities) or of
the calendar activity payload (Calendar).

Worked example: an 86-minute ride (`duration_s` 5160) downsampled to 1,500
samples. Sample 0 is minute 0; sample 749 is `749/1499*86` = 42.97 min; sample
1499 is minute 86. Using the index as seconds would label the axis 0..25 min,
which is the bug this rule replaced. Ticks for 86 min: step 15 (86/15 = 5.7),
labels 0, 15, 30, 45, 60, 75.

### Legend

Below the chart, a wrapping grid (adaptive columns, min 150 pt) of tappable
items: coloured 9 pt dot plus "`<title> <low>-<high>`" in caption text. Tapping
toggles the series (hidden items are drawn at opacity 0.4). Range is rounded
integers of the data: altitude shows min-max; power, heart rate and cadence show
`0-<max>`. Hidden state is per chart instance and starts empty (all visible).

### Not yet pinned by a shared vector

The clock and axis rules above are covered only by iOS unit tests. A
`tests/vectors/` file for the 86-minute example is not yet added; adding one
needs a generator in `scripts/generate_behaviour_vectors.py`, a Python test and a
Swift reader, and is left for the Android issue to request.

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
