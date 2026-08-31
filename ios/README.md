# WatTracker for iOS

A SwiftUI app for iPhone and iPad. Today it is the walking skeleton from
issue #171: it renders one number — the rider's FTP, read out of their desktop
database and carried across the real pairing, signing and read planes. It is
not a working app yet, and `docs/ios-walking-skeleton.md` says exactly what it
does and does not prove.

No third-party dependencies. No package resolution step. `URLSession`,
`CryptoKit`, `Security` and SwiftUI.

## Layout

```
ios/WatTracker/
  WatTracker.xcodeproj/          the project, plus the shared WatTracker scheme
  Config/
    Base.xcconfig                settings shared by every configuration
    Debug.xcconfig               local server, software-key fallback compiled in
    Release.xcconfig             production host, no software-key fallback
  WatTracker/
    WatTrackerApp.swift          the app, and the landscape request
    FTPScreen.swift              the one screen
    Info.plist
    Cloud/
      AppConfiguration.swift     the base URL, from the build configuration
      CanonicalRequest.swift     the Swift half of canonical_request
      CloudClient.swift          pair, refresh, one GET
      DeviceKey.swift            P-256 key: Secure Enclave, or a gated fallback
  WatTrackerTests/
    CanonicalRequestVectorTests.swift
```

## Open and run

```sh
open ios/WatTracker/WatTracker.xcodeproj
```

Pick an iPhone or iPad simulator and run. The Debug configuration points at
`http://localhost:8765`, so start the local server first:

```sh
python scripts/walking_skeleton_server.py --user-id <your local user id>
```

It reads your local database **read-only**, publishes one `profile` object
through the real signed sync route, and prints a pairing code. Type that code
into the app. To skip the typing, launch with the code in the environment —
this path is `#if DEBUG` only, and a shipped build always asks:

```sh
xcrun simctl install booted /path/to/WatTracker.app
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

## Landscape

`UISupportedInterfaceOrientations` (and the `~ipad` variant) list landscape
only, and `UIRequiresFullScreen` is set.

- **iPhone: enforced.** A portrait iPhone renders the app rotated ninety
  degrees, which is the restriction working. `simctl io screenshot` always
  writes the display's native framebuffer, so a capture of a portrait iPhone
  running this app looks sideways; `docs/images/171-iphone-landscape.png` is
  the same capture rotated for reading.
- **iPad: not enforced, as of iPadOS 26.** Measured on the iPad Pro 11-inch
  simulator running iPadOS 26.4: with the orientation list, `UIRequiresFullScreen`,
  and a runtime `requestGeometryUpdate(.iOS(interfaceOrientations: .landscape))`
  all in place, the app still lays out portrait on a portrait iPad. An app
  built against the iOS 26 SDK joins the new iPad windowing system, where a
  window is resizable and an orientation list no longer locks it.

  This is a real change to what issue #158 asked for, and it wants a decision
  rather than a workaround: either every iPad layout survives a portrait
  window, or the app stops building against the iOS 26 SDK. The skeleton's one
  screen survives it; a dashboard designed only for a wide, short viewport
  will not.

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
