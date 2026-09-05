# iOS device validation

Run this checklist on a physical iPhone and iPad; CI cannot validate the
camera, the Secure Enclave, permission prompts, or device-idiom layout. The
simulator is not a substitute: it has no Secure Enclave, so
`DeviceKeyStore` silently takes the software-key fallback there and the
Enclave branch never executes.

## Point the app at a server first

- Start a server. For the local harness:
  `./.venv/bin/python scripts/walking_skeleton_server.py`, which enrols a
  writer, pushes a real profile, and prints a writer-signed pairing code.
- Set `WATTRACKER_API_HOST` in `ios/WatTracker/Config/Debug.xcconfig` to the
  serving machine's address and port. **Re-check it immediately before every
  run** with `ipconfig getifaddr en0` — the address changes with the network
  (a phone hotspot puts the Mac on `172.20.10.x`), and a stale one fails as
  what looks like a pairing error rather than a networking one.
- Build the **Debug** configuration. Debug uses `Info-Debug.plist`, which
  carries `NSAllowsLocalNetworking`; Release has no ATS exception at all and
  every request to a plain-HTTP LAN address fails silently.
- Do not commit the edited xcconfig. `git checkout --` it when finished.

## Checklist

- Delete any existing install first; the fresh-install path is one of the
  things under test.
- Fresh install lands on the pairing screen, not a Dashboard it cannot fill.
- Redeem the printed code by typing it, with a rider-set device label; confirm
  the label appears in the desktop's device list.
- Redeem by QR scan; confirm the camera permission prompt appears and its
  wording matches `NSCameraUsageDescription`.
- Deny the camera, relaunch, and confirm the typed path still pairs.
- Show the scanner a QR that is not a pairing code; confirm it is ignored
  rather than spent, and that the app says so.
- Enter a wrong, expired, and already-used code; confirm all three render the
  same sentence and never distinguish themselves.
- Confirm the Dashboard renders real data from the harness rather than
  `.noData`.
- Rotate to landscape on both iPhone and iPad; check portrait iPad separately
  (#158).
- Remove the device from Settings; confirm it revokes server-side, then shows
  as revoked rather than disappearing.
- Repeat pairing after a revoke to confirm a replacement pairing succeeds.

Record device models, iOS versions, the server the run was pointed at, and
pass/fail per item. A run against `scripts/walking_skeleton_server.py` does
not satisfy #160, which needs a rider pairing against a real deployment; the
same checklist applies there with the host pointed at the deployment.
