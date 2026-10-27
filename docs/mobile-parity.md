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
against `android/` on `main` at `25e7220` (post-#419) on 2026-10-15. "Pending"
means not on `main` and not in an open PR.

| Area | Behaviour | iOS source | Android issue | Android status |
|---|---|---|---|---|
| Pairing | Typed pairing code | `Screens/PairingScreen.swift`, `Cloud/PairingCode.swift` | #195 | Done (#376). **Differs:** Android refuses a code that fails local normalization and also strips `\r`/`\n`; iOS sends typed input as written and normalizes only scans. `pairing_code_v1.json` pins the shared rule |
| Pairing | QR scan, four camera states, local check of scanned codes | `Screens/QRCodeScanner.swift`, `Screens/PairingScreen.swift` | #195 | **Open decision** on #195: typed only (deliberate difference), Play services scanner, or CameraX plus a decoder. Manifest currently says "No camera" |
| Settings | Remove this device: on 401/404 with a `Date` within clock tolerance, finish locally; keep the credential on a missing or skewed `Date`, 429/503 or offline (#410) | `Cloud/CloudSession.swift` (`removeDevice`, `serverNoLongerKnowsCredential`) | #195 | Done (#419); all 23 vector cases pinned by `RemovalDecisionVectorTest.kt` |
| Settings | Revoked from the desktop shows "Device removed", not unpaired (#153) | `Cloud/CloudSession.swift` (`markRemoved`), `Shell/AppGate.swift` | #195 | Done (#376, `markRemovedState`) |
| Transport | Cold start of the scale-to-zero read app: 60/120 s timeouts, one retry after 3 s, a 10 s waking gate that never shortens a stronger gate or counts toward removal, "Waking up the cloud…" after 5 s (#422) | `Cloud/CloudSession.swift`, `Cloud/CloudTransport.swift`, `Theme/Panel.swift` | #196 | Timeouts, the ride-out and the gate: Done (#419), and the detail/stream reads joined them on `feature/android-client-5`. **Gap, was over-marked:** Android's notice comes from a `Failure.Waking` answer, never from iOS's 5 s in-flight watch (`WakingWatch.during`, `Theme/Panel.swift`), so a read that spends the whole ride-out (60 s + 3 s + 60 s) shows only a spinner for all of it -- on the dashboard, the list and the detail alike. Unimplemented on every Android screen; not fixed by #419 |
| Transport | Pull to refresh lifts only a cold-start gate (`retryNowIfWaking`); detail and stream reads get the retry and waking gate | `Screens/ActivitiesModel.swift`, `Screens/ActivitiesScreen.swift`, `Screens/CalendarScreen.swift`, `Cloud/CloudSession.swift` | #196–#198 | Dashboard (#419), the activities list (`feature/android-client-5`) and the calendar and volume screens (#456) have pull to refresh; the detail/stream reads ride the wake out themselves (`ridingOutColdStart` + `noteColdStart` in `activityObject`) and hear `Failure.Waking`, and the detail pane gained its own pull to lift the gate, which iOS reaches through the list's (`feature/android-client-5`) |
| Transport | 413 `collection_too_large` on unpaginated routes keeps the cache (#379) | `Cloud/CloudSession.swift` | #196 | Done (#419) |
| Dashboard | Form/fitness/fatigue, training load, power curve; progress over ~15 pages; load window measured from the newest load point, not the clock | `Screens/DashboardScreen.swift`, `Cloud/MobileScreenData.swift` | #196 | Done (#419) |
| Dashboard | Zone `pct` is a display string (`"<56%"`, `"56–75%"`), top zone `max` may be null (#412) | `Cloud/CloudModels.swift` | #196 | Done (#419) |
| Activities | List and detail; two panes only on regular-width iPad; bounded detail/stream cache cleared on revoke (#351, #358); desktop publishes bounded/downsampled stream objects for every ride with a plottable power, heart-rate, cadence, or altitude channel; the 512 KiB decoded-input guard remains for derived reads and a separate 4 MiB stream-only fallback handles larger sources; a ride without plottable streams still maps 404 to "No recorded streams" | `Screens/ActivitiesScreen.swift`, `Screens/ActivitiesModel.swift`, `Cloud/CloudClient.swift` | #197 | Publisher/decoder contract in PR #445; Android built on `feature/android-client-5`: list + detail on the shared seam, streams 404 or empty payload → "No recorded streams", bounded (8, 5 min) detail/stream cache cleared on pair/sign-out/removal (iOS #351/#358), list/detail side by side on a phone in landscape and a tablet (the iOS two-pane is iPad-only). Verified on the emulators 2026-10-15 (paired list/detail/chart/zones, the no-streams mapping, the pushed portrait detail, the 300-ride scroll's flat memory); the paired tablet data check and a physical-device pass still owed. **Differs:** Android renders a ride's start as the instant in the rider's zone -- a naive published stamp is read as UTC, and a microsecond fraction is read at its own scale -- where iOS shows the raw string |
| Activities | **Ride graphs**: one combined chart, not per-channel charts; spec in [Ride graphs chart](#ride-graphs-chart) (#447, #448) | `Screens/ActivitiesScreen.swift` (`StreamCharts`), `Screens/ActivitiesModel.swift` (`CombinedChartSeries`, `MinutesClock`, `ChartAxis`, `ChartTicks`) | #197 | Built on `feature/android-client-5` to look like iOS (desktop palette not copied): one combined chart, altitude behind, shared 0...1 band, true-minutes `MinutesClock`, nice 1/2/5 axes, tappable legend that does not rescale. Kotlin `RideDisplayTest` ports the iOS `RideDisplayTests`; chart verified on the emulator against a live 1,500-point publish (2026-10-15). The shared clock/axis vector is requested, not yet added |
| Calendar | Ride detail shows the same Ride graphs chart, fed `duration_s` from the activity payload (#447, #448) | `Screens/CalendarScreen.swift` (`StreamCharts(streams:durationSeconds:)`) | #198 | Closed by reuse in #456: the day view's ride rows push the existing `activities/detail/<id>` route, so the calendar shows the one Ride graphs screen the activities list already feeds -- no second chart to keep in sync. Push verified on the paired emulators 2026-10-27 (ride opens with its metrics; the no-streams mapping renders) |
| Calendar | Paginated delta route with tombstones (#386) | `Cloud/CloudSession.swift` | #198 | Done (#419) |
| Calendar | Full Monday–Sunday weeks with adjacent months' real dates, date-only UTC arithmetic, cells keyed by ISO date (#431) | `Cloud/MobileScreenData.swift` (`CalendarMonth.gridDates()`), `Screens/CalendarScreen.swift` | #198 | Done (#456): Kotlin `CalendarMonth.gridDates()`; `CalendarGridVectorTest.kt` reads `calendar_grid_v1.json`, so the vector that pinned "Android consumes it once it has a calendar grid" now actually pins Android. Local adapter drops the desktop's adjacent-month week cells from the month's own cache and accumulates visited months. Verified on the emulators 2026-10-27, both backends: tablet portrait and landscape render the detailed cell (the pane is 520dp for a reason), a landscape phone the compact one; adjacent-month cells recede but still show real content -- an in-OOTO skipped 1 Nov appears inside October's last week, exactly as the desktop page draws it. **Differs:** the cell border and the compact content dot use a separate cell-content flag that leaves `phase` out, so a plan window does not light up every day it covers -- the #456 emulator pass found that happening, and iOS still does it, because its cell border uses `hasContent`, which counts `phase != nil`. Android keeps `phase` in the *month*-level `hasContent`, matching iOS, so a month whose only content is the plan still shows its grid instead of a "no data" panel (`CalendarDataTest.phaseIsTheMonthsContentButNotTheCells`) |
| Calendar | Day view: workout power graph, step list, "at FTP N W", back arrow (#413/#416) | `Screens/CalendarScreen.swift` | #198 | Done (#456): Canvas profile chart (zone-opacities fill; dashed FTP rule, and the y-axis is built to reach the FTP rather than only the targets -- the desktop's `maxW` starts at the FTP for the same reason, because the line is what makes a workout read as under or over threshold and an axis that stops at the top target dropped it for every below-threshold ride; %FTP x 100 scale when the rider has no FTP), step list and "at FTP %1$d W" via the ported formatter (step watts half-to-even, printed percents half away from zero as `Math.round` and Swift `.rounded()` do it, and the at-FTP number rounded to the nearest watt rather than truncated -- the steps under it are built on the unrounded FTP; `calendar_day_profile.json` vector read from Kotlin). A workout whose blocks are all max-effort or untargeted says "no power targets to plot for this workout", not "nothing planned or recorded on this day", which contradicted the heading it sat under. **Differs:** the day detail is a side pane on wide viewports and a bottom sheet in portrait, not iOS's push with a back arrow. Verified on the emulators 2026-10-27: the sheet (phone portrait) and the pane (phone landscape, tablet) render the profile chart, the step list and the at-FTP line in the desktop's exact wording (112–162 W (45%–65% FTP), 175 W (70% FTP) at FTP 250 W) |
| Calendar | Month states: what an empty month and an unreadable month show, and how the rider leaves either | `Screens/CalendarScreen.swift` (`CalendarModel.apply` → `.noData`; the chevrons live inside the ready-state content) | #198 | Done (#456): the month stepper is painted above the body in **every** paired state; a month that was read and held nothing says "nothing planned or recorded in <month>" (`monthIsEmpty`) instead of "your paired backend has not published calendar data yet"; the month-level decision is scoped to the month, so the cloud's one whole-feed snapshot can no longer call an empty September "has content" because October rode; and a month move drops the previous month's grid, so a June header never sits over May's rows with May's "updated" stamp. **Differs:** iOS shows its `.noData` panel with no chevrons (an empty month is a dead end there too, and the emulator pass showed the local backend reaches it easily) and keeps the old grid up after a failed month move. `CalendarViewModelTest.anEmptyMonthSaysSoAndStaysNavigable` and `movingToAMonthThatCannotBeReadLeavesNoGridUnderTheNewHeader` pin Android's rules |
| Calendar | Localized month name and day heading | `Cloud/MobileScreenData.swift` (`setLocalizedDateFormatFromTemplate("LLLLyyyy")`), `Screens/CalendarScreen.swift` (`displayDate`, fixed `"EEEE, MMM d, yyyy"`) | #198 | **Differs, and is wrong the same way on both:** Android names the month from a fixed `"LLLL yyyy"` pattern -- `java.time` has no month-year template to ask, and the ICU call that reorders (`android.text.format.DateFormat.getBestDateTimePattern`) is a framework API the JVM-tested cloud model cannot reach -- so a year-first locale (ja) reads the order wrong for the month title. iOS's day heading uses a fixed `dateFormat` too, so Android's fixed day pattern matches it exactly |
| Volume | Hours / TSS / Distance / Calories × last 4 weeks / 12 weeks / year to date | `Screens/VolumeScreen.swift`, `Cloud/MobileScreenData.swift` | #198 | Done (#456): bars in iOS's volume blue (the accent-gold desktop web colour deliberately not copied), dense zero weeks, YTD by the bucket's Monday year, change only against a complete previous window; `VolumeDataTest.kt` pins those rules. **Differs:** a change of exactly zero prints the desktop's flat ▬ and no "+" (iOS puts "+0%" under an up arrow), and the axis tick labels keep a decimal the value needs -- a window whose busiest week is under 2 h steps by 0.5 and reads "0, 0.5, 1", not "0, 0, 1, 1" Numbers cross-checked against the desktop server's own `/api/volume` on the paired emulators 2026-10-27, both backends: Last 12 26.1 h / 1573 TSS / 759.3 km / 17506 kcal, Last 4 10.2 h, YTD 76.2 h, change −33% -- every tile agrees with the server to the printed digit |
| Backend | Rider picks local desktop or cloud | `Shell/SessionGate.swift`, `Screens/SettingsScreen.swift` | #282 | Done: manual picker in `SettingsScreen.kt` |
| Backend | Prefer the paired desktop when reachable, cloud otherwise (#281) | `Cloud/LocalBackend.swift`, `Shell/SessionGate.swift` | #282 | Pending. iOS code is in; #281 stays open for the device check |
| Sync | **Foreground-only, owner decision 2026-10-26:** neither client syncs in the background. iOS refreshes on launch, foreground and network-path change; Android must match, with no WorkManager prefetch | `Shell/SessionGate.swift`, `Cloud/CloudSession.swift` | #194, #282 | Remaining gap: the connectivity-change refresh (`ConnectivityManager.NetworkCallback`) |
| Release | Real cloud host from a gitignored per-machine file, never from the tree (#420). iOS: `Release.xcconfig` does an optional `#include?` of the gitignored `Production.local.xcconfig` | `ios/WatTracker/Config/` | #199 | Done (#419): `-PwattrackerCloudAuthority` or `local.properties`, keeping the build's refusal to ship the `.example` placeholder `releaseCloudAuthority` |
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
| `calendar_day_profile.json` | `calendar_day` workout `profile` from the real publisher; drift-tested | `tests/test_cloud_snapshot_derived.py` | `CloudModelsTests.swift` | `CloudObjectsTest.kt` |
| `calendar_grid_v1.json` | Monday–Sunday month grid; agrees with the desktop's `/api/calendar` | `tests/test_behaviour_vectors.py` (`scripts/generate_behaviour_vectors.py`) | `BehaviourVectorTests.swift` | `CalendarGridVectorTest.kt` (since #456) |
| `pairing_code_v1.json` | Pairing code normalization; `client_may_accept` cases allow a client to also strip `\r`/`\n` | `tests/test_behaviour_vectors.py` (`scripts/generate_behaviour_vectors.py`) | `BehaviourVectorTests.swift` | `PairingCodeVectorTest.kt` |
| `removal_decision_v1.json` | Remove-device decision on 401/404 and `Date` skew (240 s, inside the server's 300 s) | `tests/test_behaviour_vectors.py` (`scripts/generate_behaviour_vectors.py`) | `BehaviourVectorTests.swift` (`CloudSession.removalCompletesLocally`) | `RemovalDecisionVectorTest.kt` (`CloudSession.removalCompletesLocally`) |
