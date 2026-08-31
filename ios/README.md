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
  Config/
    Base.xcconfig                settings shared by every configuration
    Debug.xcconfig               local server, software-key fallback compiled in
    Release.xcconfig             production host, no software-key fallback
  WatTracker/
    WatTrackerApp.swift          the app, and the iPhone landscape request
    Info.plist
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

## Building, and the simulator runtime this machine is missing

```sh
open ios/WatTracker/WatTracker.xcodeproj
```

Building from Xcode works. Building from the command line **by simulator
destination** currently does not, on this machine:

```
xcodebuild -project WatTracker.xcodeproj -scheme WatTracker \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' build
# xcodebuild: error: Unable to find a destination matching the provided
# destination specifier
```

This is a toolchain gap, not a project fault -- it reproduces on the unmodified
pre-#158 project too. Xcode 26.6 carries the iOS 26.5 SDK and no iOS 26.5
*simulator runtime* is installed (26.2, 26.4 and 26.4.1 are), so Xcode reports
every iOS destination ineligible. Installing the matching runtime from
Xcode > Settings > Components fixes it. Until then, build by SDK, which needs
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

The same gap is why there is **no asset catalog**. An `Assets.xcassets` holding
only an AppIcon placeholder and an AccentColor made every build fail with
`No simulator runtime version from [...] available to use with iphonesimulator
SDK version`, because `actool` needs a runtime matching the SDK. It bought
nothing -- the accent colour is `Palette.accent` in code and the icon was blank
-- so it was dropped rather than left as a landmine under six blocked issues.
#166 has to add a real app icon before TestFlight and should add the catalog
back then, on a machine with a matching runtime.

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

The project ad-hoc signs (`CODE_SIGN_IDENTITY = -`), which is all a simulator
needs and all a public repository should carry. A device or TestFlight build
sets `DEVELOPMENT_TEAM` locally; a team identifier is an account identifier and
is not committed here.

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
