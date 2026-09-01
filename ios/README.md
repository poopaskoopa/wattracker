# WatTracker for iOS

A SwiftUI app for iPhone and iPad. It is the app shell from issue #158: five
destinations -- Dashboard, Activities, Calendar, Volume, Settings -- in a
navigation structure that adapts to the idiom, with every screen still a stub.
No screen reads any data yet; filling them in is #161, #162 and #163.

Folded inside it is the walking skeleton from issue #171, which does read real
data: **Settings > Debug: FTP round-trip** renders one number, the rider's FTP,
carried out of their desktop database across the real pairing, signing and read
planes. `docs/ios-walking-skeleton.md` says exactly what that does and does not
prove. It is kept because it is the only on-device evidence those planes work
end to end; #161 can retire it once the Dashboard reads the same data properly.

No third-party dependencies. No package resolution step. `URLSession`,
`CryptoKit`, `Security`, SwiftUI.

## Layout

```
ios/WatTracker/
  WatTracker.xcodeproj/          the project, plus the shared WatTracker scheme
  ExportOptions.plist            App Store export options; note the absent teamID
  AppIcon.source.html            the vector the 1024 icon is rendered from
  Config/
    Base.xcconfig                settings shared by every configuration
    Debug.xcconfig               local server, software-key fallback compiled in
    Release.xcconfig             production host, no software-key fallback
  WatTracker/
    WatTrackerApp.swift          the app, and the iPhone landscape request
    Info.plist                   versions come from build settings, not literals
    Assets.xcassets/             AppIcon (1024) and AccentColor
    Shell/
      RootView.swift             picks the shell for the idiom; the rationale
      Destination.swift          the five destinations, the whole nav model
      SideRail.swift             the iPhone-landscape leading icon rail
    Screens/
      DashboardScreen.swift      stub - #161
      ActivitiesScreen.swift     stub - #162
      CalendarScreen.swift       stub - #163
      VolumeScreen.swift         stub - #163
      SettingsScreen.swift       stub - #160, plus the debug FTP round-trip
    Theme/
      Palette.swift              the desktop palette, ported
      Panel.swift                panel, screen scaffold, stub placeholder
    FTPScreen.swift              #171's walking skeleton, reached from Settings
    Cloud/
      AppConfiguration.swift     the base URL, from the build configuration
      CanonicalRequest.swift     the Swift half of canonical_request
      CloudClient.swift          pair, refresh, one GET
      DeviceKey.swift            P-256 key: Secure Enclave, or a gated fallback
  WatTrackerTests/
    CanonicalRequestVectorTests.swift
```

## The shell, and why it differs per idiom

`RootView` picks one of two shells.

**iPad (regular width): `NavigationSplitView`.** Five destinations in the
sidebar, the selected screen as detail. It collapses to a push stack on its own
when the window is narrow, which is what makes portrait correct rather than a
special case this code has to write.

**iPhone landscape: a 64pt leading icon rail.** Deliberately not a bottom
`TabView`. Landscape on an iPhone 17 Pro is roughly 874x402pt before safe
areas: vertical space is the scarce axis. A bottom tab bar costs ~49pt of that
402pt height on every screen, over 12% of the scarce axis; a 64pt rail costs
~7% of the abundant 874pt width. On a wide, short viewport the chrome belongs
on the long edge, and the rail also sits under the left thumb, which is where
the device is actually held in landscape.

The predicate is the size class *and* the idiom, not the size class alone: a
Max-sized iPhone in landscape also reports a regular horizontal size class, and
it has the same short viewport every other landscape iPhone has, so it gets the
rail too.

## Orientation: locked on iPhone, all four on iPad

A decision made by #158, and it is not symmetric.

- **iPhone: landscape only, and the system enforces it.** Every layout is
  designed for a wide, short viewport. Portrait is not an orientation the phone
  degrades into; it is one it does not have.
- **iPad: all four, because the platform no longer lets us choose.** #171
  measured that on iPadOS 26 a landscape-only
  `UISupportedInterfaceOrientations~ipad`, plus `UIRequiresFullScreen`, plus a
  runtime `requestGeometryUpdate(.iOS(interfaceOrientations: .landscape))`
  still lays out portrait on a portrait iPad. An app built against the iOS 26
  SDK joins the new iPad windowing system, where a window is resizable and an
  orientation list no longer locks it.

  Dropping to an older SDK was the alternative, and was rejected: #166 ships
  this to TestFlight and App Store submission requires a current SDK, so that
  route trades a layout problem for a distribution blocker. The app adapts
  instead. `UIRequiresFullScreen` is gone -- it was only ever there to make the
  lock stick, which it does not do.

  **The standing requirement this creates: every iPad layout must be correct in
  portrait.** Not necessarily optimal, but never broken. A screen designed only
  for a wide, short viewport will be rotated into portrait anyway and will
  render wrong. Check both before calling an iPad screen done.

`WatTrackerApp.requestLandscape()` is kept, but gated to iPhone. On iPad it was
already ineffective, and it is now actively unwanted: if it ever started
working it would yank the rider out of a portrait layout the app supports.

When reading captures: `simctl io screenshot` always writes the display's
native framebuffer, so a landscape app on a portrait-oriented iPhone simulator
captures as a portrait image with the content rotated 90 degrees. Rotate the
capture 270 degrees to read it. The layout is not wrong.

## Building

```sh
open ios/WatTracker/WatTracker.xcodeproj
```

From the command line, by simulator destination:

```sh
xcodebuild -project ios/WatTracker/WatTracker.xcodeproj -scheme WatTracker \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build
```

This used to fail on this machine, and the reason is worth keeping because it
will recur on any machine that updates Xcode without updating its runtimes:
Xcode 26.6 carries the iOS 26.5 SDK, and with no iOS 26.5 *simulator runtime*
installed every iOS destination is reported ineligible ("Unable to find a
destination matching the provided destination specifier"). It is a toolchain
gap, not a project fault. Install the matching runtime from
Xcode > Settings > Components. The 26.5 runtime is installed here now, and
`xcodebuild test` resolves too — the TestFlight release job runs the Swift
suite by destination before it signs anything.

The same gap is why there was no asset catalog until #166: an `Assets.xcassets`
made every build fail with `No simulator runtime version from [...] available
to use with iphonesimulator SDK version`, because `actool` needs a runtime
matching the SDK. The catalog is back — TestFlight will not accept a build
without an app icon — and it builds because the runtime is present.

On a machine where no destination resolves, build by SDK instead, which needs
no resolvable destination:

```sh
cd ios/WatTracker
xcodebuild -project WatTracker.xcodeproj -target WatTracker \
  -configuration Debug -sdk iphonesimulator -arch arm64 \
  SYMROOT=/tmp/wt-build build

xcrun simctl boot "iPhone 17 Pro"          # or "iPad Pro 13-inch (M5)"
xcrun simctl install booted /tmp/wt-build/Debug-iphonesimulator/WatTracker.app
xcrun simctl launch booted com.wattracker.ios
```

## Theme

`Theme/Palette.swift` is the desktop palette, ported value-for-value from the
`:root` block of `wattracker/web/static/style.css`, with the CSS custom
property names kept so a change on one side is greppable on the other. There is
no second iOS-only palette on purpose: the rider looks at the web app and this
app in the same session, and two palettes drift.

The app is dark only and forces `.preferredColorScheme(.dark)`. There is no
light variant to fall back to.

## Running the debug FTP round-trip

Settings > **Debug: FTP round-trip**. The Debug configuration points at
`http://localhost:8765`, so start the local server first:

```sh
python scripts/walking_skeleton_server.py --user-id <your local user id>
```

It reads your local database **read-only**, publishes one `profile` object
through the real signed sync route, and prints a pairing code. Type that code
into the app. To skip the typing, launch with the code in the environment --
this path is `#if DEBUG` only, and a shipped build always asks:

```sh
SIMCTL_CHILD_WATTRACKER_PAIRING_CODE=ABCD-EFGH-JKLM \
  xcrun simctl launch booted com.wattracker.ios
```

Against a deployed server, build the Release configuration. Nothing else
changes: the base URL is a build setting, not a literal.

## The API base URL is a build setting

`Config/Base.xcconfig` carries `WATTRACKER_API_SCHEME` and
`WATTRACKER_API_HOST`; `Debug.xcconfig` overrides them to a local server. They
reach the app through `Info.plist` and `AppConfiguration.apiBaseURL`, and
nothing in Swift contains a URL. Moving the host is an xcconfig edit and a
rebuild, not a grep through the source.

It is two settings rather than one because `//` starts a comment in an xcconfig
file, so a full URL cannot be written there without an escaping trick.

## Signing keys, and the fallback that cannot ship

The device signs requests with a P-256 key. On hardware that has a Secure
Enclave, the private half is generated in and never leaves the Enclave.

The simulator on an Intel Mac does not have one, so `Debug.xcconfig` defines
`WATTRACKER_SOFTWARE_KEYS_ALLOWED`, which compiles in a keychain-stored
software key. This is a **compile-time** condition, not a runtime flag:
`DeviceKey.swift` fails to compile with `#error` if it is ever seen in a
non-DEBUG build, so shipping a software key is a build failure rather than a
mistake somebody can make at runtime. The app also prints which key it used on
screen, so a screenshot cannot be mistaken for evidence about the other one.

Measured on this machine: `SecureEnclave.isAvailable` is **true** on the
iOS 26.4 simulator on Apple Silicon, and the whole flow ran on a real
Enclave-backed key. The fallback stays because that is not true everywhere.

CryptoKit exports the public half as `publicKey.x963Representation`: 65 bytes,
`0x04` prefix, uncompressed SEC1 — exactly the one encoding
`validate_public_key` accepts. Do not reach for `rawRepresentation`, which is
the same point without the prefix byte and is rejected.

## Signing

The project ad-hoc signs (`CODE_SIGN_IDENTITY = -`, `CODE_SIGNING_REQUIRED =
NO`, `CODE_SIGN_STYLE = Manual`, and an empty `DEVELOPMENT_TEAM`), which is all
a simulator needs and all a public repository should carry. Clone this with no
Apple credentials at all and it still builds and runs on a simulator; that is a
property worth not breaking.

A team identifier is an Apple account identifier and this repository is public,
so it is not committed — and unlike a key it cannot be rotated once it is in a
git history on github.com. It reaches a build in exactly one place: the release
job overrides all four settings on the `xcodebuild` command line from the
`APPLE_TEAM_ID` secret. Nothing on disk changes. A developer who wants a device
build sets `DEVELOPMENT_TEAM` in a local, uncommitted xcconfig or in Xcode's
signing pane.

**A device or App Store build needs an Apple Distribution certificate, and
nothing creates one for you.** It is easy to assume `-allowProvisioningUpdates`
with an App Store Connect API key covers this — that assumption is what stalled
the first version of the release pipeline. That combination registers App IDs
and creates and downloads provisioning profiles; it does not issue
certificates, and it cannot, because a certificate is a private key Apple never
sees. Without one, automatic signing falls back to the account's development
certificates and an archive fails with either "conflicting provisioning
settings" or "has entitlements that require signing with a development
certificate". `scripts/ios_distribution_cert.py` creates the certificate once
against the API; `docs/ios-testflight.md` has the procedure, the expiry and the
rotation order.

**The release job signs manually, not automatically**, and that is deliberate
rather than legacy. Xcode picks development-vs-distribution for *automatic*
signing from the target's `ProvisioningStyle` attribute in the `.pbxproj`, and
this project has none — keeping every signing setting in the xcconfig is what
lets a credential-less clone build. With nothing to read, automatic signing
resolves to development however the certificate situation looks, so the job
names the identity and the profile
(`PROVISIONING_PROFILE_SPECIFIER="WatTracker App Store"`) instead. If you ever
switch the archive back to `CODE_SIGN_STYLE=Automatic`, expect those two errors
back.

## Release: shipping to TestFlight

**`docs/ios-testflight.md` is the runbook.** The short version:

```sh
git tag ios-v0.1.0
git push origin ios-v0.1.0
```

`.github/workflows/ios-release.yml` fires on any `ios-v*` tag and archives,
signs, exports and uploads to TestFlight on the self-hosted macOS runner. The
`ios-v` prefix is deliberately distinct from the `v*` that the two desktop
release workflows use, so a desktop release does not start an iOS archive.

**There is a one-time manual setup before the first tag will work**, and it
cannot be automated: the App Store Connect API has no way to create an app
record. Register `com.wattracker.ios` in the Developer portal, then create the
app record in App Store Connect (My Apps → **+** → New App → iOS, bundle id
`com.wattracker.ios`, plus a name, primary language and SKU). Full instructions,
including why the order matters and what the first run does if you skip the
first half, are in `docs/ios-testflight.md`.

**Credentials.** Six secrets in the `ios-code-signing` GitHub *environment* —
`APPLE_TEAM_ID`, `APP_STORE_CONNECT_KEY_ID`, `APP_STORE_CONNECT_ISSUER_ID`,
`APP_STORE_CONNECT_PRIVATE_KEY`, and the distribution certificate itself as
`IOS_DIST_P12_B64` plus `IOS_DIST_P12_PASSWORD`. An environment rather than
repository secrets so that only a job declaring `environment: ios-code-signing`
can read them. None of their values appears anywhere in this repository, and
none may. The `.p8` is written to `$RUNNER_TEMP` under `umask 077` at run time;
the `.p12` is decoded there, imported into a keychain created for that one job,
and deleted in the same step. An `if: always()` step deletes the keychain and
the key and fails the job if either survives — the macOS runner is a physical
machine that is not discarded between jobs. Nothing signing-related touches the
workspace, `DerivedData`, or an artifact.

**The distribution certificate expires one year after issue** and nothing
renews it. Creation, the expiry date, and the rotate-then-revoke order are in
`docs/ios-testflight.md`.

**Versioning.** `CFBundleShortVersionString` comes from the tag (`ios-v0.1.0` →
`0.1.0`); `CFBundleVersion` is `<run_number>.<run_attempt>`. Both are build
settings, which is why `Info.plist` carries `$(MARKETING_VERSION)` and
`$(CURRENT_PROJECT_VERSION)` rather than literals. App Store Connect refuses a
build number it has already accepted for a version — including on a re-run of a
failed job, which is what `run_attempt` is for.

**TestFlight builds expire 90 days after upload**, internal and external alike.
There is no extension and no setting. Re-issuing is just cutting another
release: push a new `ios-v*` tag, or re-tag the same commit if nothing changed
— the build number differs by construction, so Apple accepts it and testers get
a fresh 90 days.

## Tests

The Swift suite asserts against `tests/vectors/canonical_request_v1.json` — the
same file the Python suite reads, referenced from the repository rather than
copied, so the two cannot drift.

`xcodebuild test` needs a resolvable simulator destination. On a machine where
`xcodebuild -showdestinations` lists none — which happens when the installed
simulator runtimes are older than the SDK — build the bundle and run it
directly:

```sh
cd ios/WatTracker
xcodebuild -project WatTracker.xcodeproj -target WatTrackerTests \
  -configuration Debug -sdk iphonesimulator -arch arm64 \
  SYMROOT=/tmp/wt-tests build

xcrun simctl boot "iPhone 17 Pro"
xcrun simctl spawn booted \
  "$(xcode-select -p)/Platforms/iPhoneSimulator.platform/Developer/Library/Xcode/Agents/xctest" \
  -XCTest All /tmp/wt-tests/Debug-iphonesimulator/WatTrackerTests.xctest
```
