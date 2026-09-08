# iOS device validation

Run this checklist on a physical iPhone and iPad; CI cannot validate the
camera, the Secure Enclave, permission prompts, or device-idiom layout. The
simulator is not a substitute: it has no Secure Enclave, so
`DeviceKeyStore` silently takes the software-key fallback there and the
Enclave branch never executes.

## Point the app at a server first

- Start the local harness with `--lan`:
  `./.venv/bin/python scripts/walking_skeleton_server.py --lan`. It enrols a
  writer, pushes a real snapshot, prints a writer-signed pairing code, and —
  the part that matters for a device — detects the Mac's own LAN address,
  binds every interface, and writes it to `ios/WatTracker/Config/Local.xcconfig`,
  which `Debug.xcconfig` optionally includes. Never hardcode an address: it
  changes with the network (a phone hotspot puts the Mac on `172.20.10.x`),
  and a stale one fails as what looks like a pairing error rather than a
  networking one. `Local.xcconfig` is gitignored; `--no-xcconfig` skips it.
- Check the address it prints against the network the phone is on. Detection
  skips tunnels (`utun`, `ipsec`, …), so a VPN does not hijack it, but with
  several live interfaces it says so and you pick: pass `--host <address>`, or
  disconnect the VPN and re-run.
- Rebuild after starting the server — Xcode reads xcconfig at build time, so a
  build made before the harness wrote the file still carries the old host.
- Build the **Debug** configuration. Debug uses `Info-Debug.plist`, which
  carries `NSAllowsLocalNetworking`; Release has no ATS exception at all and
  every request to a plain-HTTP LAN address fails silently.
- Phone and Mac must be on the same network, and the Mac's firewall must allow
  incoming connections for the Python binary.

## Checklist

- Delete any existing install first; the fresh-install path is one of the
  things under test.
- Fresh install lands on the pairing screen, not a Dashboard it cannot fill.
- Redeem the printed code by typing it, with a rider-set device label; confirm
  the label appears in the device list in Settings. Against the harness that
  list is the only place it can be checked -- there is no desktop device list,
  because the desktop app is not an enrolled cloud writer and the harness is
  standing in for one.
- Redeem by QR scan; confirm the camera permission prompt appears and its
  wording matches `NSCameraUsageDescription`.
- Deny the camera, relaunch, and confirm the typed path still pairs.
- Show the scanner a QR that is not a pairing code; confirm it is ignored
  rather than spent, and that the app says so.
- Enter a wrong, expired, and already-used code; confirm all three render the
  same sentence and never distinguish themselves.
- On iPhone, flip the device 180 degrees between the two landscape
  orientations; confirm the rail tracks the *leading* edge rather than staying
  on one physical side, and that its buttons still clear the sensor housing.
  There is nothing to check about rotating *into* landscape: the phone build
  declares only `LandscapeLeft` and `LandscapeRight`, so it is never anything
  else.
- Open the scanner and confirm the camera preview is upright in both landscape
  orientations. This needs a human: `AVCaptureMetadataOutput` reads QR codes at
  any angle, so a preview drawn a quarter turn out still scans and still passes
  every test (fixed once already, found this way).
- On iPad, rotate through all four orientations; portrait is the one that
  matters (#158), including Slide Over and a narrow Stage Manager window, which
  fall through to the rail layout.
- Remove the device from Settings; confirm it revokes server-side, then shows
  as revoked rather than disappearing.
- Repeat pairing after a revoke to confirm a replacement pairing succeeds.

## Not on this branch

Activities is still a stub. Dashboard, Calendar and Volume now consume the
shared session and cache, but no physical-device run is recorded on this
branch. After pairing against the LAN harness, open Calendar in every iPad
orientation and in a narrow Stage Manager/Split View window; verify month
navigation, day detail, planned/completed rows and the ride-detail link. Open
Volume and verify the weekly bars, metric picker, range picker and last-4,
last-12 and YTD summaries against the desktop for the same snapshot. Record
the device models, OS versions and results before calling either screen
validated.

## Budget the pairing codes

A code lives 900 seconds and this checklist takes about an hour, so plan on
minting several times. `--codes N` does not help as much as it looks: every
code in a batch is minted at startup and shares one expiry.

Worse, the store is in memory, so **restarting the harness to get fresh codes
destroys the server-side state the later items depend on** -- an existing
pairing dies and any revoked-device record is gone. Do the revoke and
revoked-display checks in one unbroken stretch, and do not remint in the middle
of them. Raising the TTL is not available from here: the registry is built
inside `create_cloud_app` with the default and the mint route takes no TTL.

Order the items so one unpaired visit to the pairing screen covers all of the
failure cases -- non-pairing QR, wrong code, expired code, already-used code --
before pairing again, because the scanner and the code field only exist while
unpaired, and each re-pair costs a code.

Record device models, iOS versions, the server the run was pointed at, and
pass/fail per item. A run against `scripts/walking_skeleton_server.py` does
not satisfy #160, which needs a rider pairing against a real deployment; the
same checklist applies there with the host pointed at the deployment.
