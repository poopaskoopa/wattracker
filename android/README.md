# android/

The wattracker Android client: a single-activity Jetpack Compose app that reads
from the local desktop app (over proxied HTTPS) or the hosted cloud. Both backends
are TLS; the app never speaks cleartext in a release build.
`plan.md` is the authoritative step-by-step plan for this directory; this file
records what the steps assume of the machine around them.

The app shell today (issue #193): five destinations — Dashboard, Activities,
Calendar, Volume, Settings — behind one navigation model, presented as a
leading navigation rail on phones and a permanent navigation drawer on
tablets. The screens are stubs; the cloud/local split, Room, and the
KeyStore-backed credential store land in steps 2–4 (#194–#196).

## Build and run

```
.\gradlew.bat :app:assembleDebug          # from this directory
adb install -r app\build\outputs\apk\debug\app-debug.apk
adb shell am start -n com.wattracker.android/.MainActivity
```

`targetSdk` is 36 and `minSdk` is 30. Release builds compile with the
production network security config (cleartext denied) and a placeholder
cloud host (`BuildConfig.WATTRACKER_CLOUD_HOST`, Step 3 replaces it with the
production value).

### The two AVDs

The shell is validated on both idioms, so two AVDs are the working set. They
are created from the new `android` CLI's **size profiles** (not `avdmanager`
device models — the old tool NPEs on this machine's SDK, and the new CLI has
no `pixel_9`/`pixel_tablet` to pick; profiles are the closest equivalent):

| name           | profile         | image that gets pulled                       | role                        |
|----------------|-----------------|----------------------------------------------|-----------------------------|
| `medium_phone` | `medium_phone`  | google_apis (Play Store), x86_64, **API 36** | rail shell, landscape       |
| `medium_tablet`| `medium_tablet` | google_apis (Play Store Tablet), x86_64, **API 35** | drawer shell, both |

`medium_tablet` is what the plan's "tablet in landscape" acceptance criterion
means concretely, and `medium_phone` is the landscape phone that justifies the
rail. Create + boot with the `android` CLI (it auto-downloads the image, and
`start` blocks until the device is fully up):

```
android emulator create medium_phone
android emulator create medium_tablet
android emulator start medium_phone      # returns when booted
```

> The tablet's only x86_64 image is **API 35**, not 36 (the CLI pulls
> `google_playstore_tablet/x86_64-35`). The app's `minSdk` is 30, so it runs
> fine; the phone stays on API 36. Deviation from the plan's "API 36" for
> both, recorded in the PR.

On this machine the emulators run without KVM (Windows, Hypervisor-driven),
so expect a slow first boot — that is the emulator, not the app. When capturing
screenshots, `adb shell screencap` on this machine grabs a stale lock frame even
when `isKeyguardShowing=false`; the IDE's screenshot of the running emulator is
the reliable path.

## Why a rail on the phone and a drawer on the tablet

The full arithmetic is in the KDoc on `RootScreen.kt`; the short version:

- **Phone = leading rail.** The phone is landscape: a wide, short viewport
  (roughly 900x400dp on a Pixel 9 before insets). A bottom bar costs ~80dp of
  the ~400dp of height — a fifth of the scarce axis — on every screen. An
  80dp rail costs under a tenth of the ~900dp of width, and it sits under the
  left thumb, where the device is held in landscape. This deliberately
  departs from the platform default, which is the right answer in portrait
  and the wrong one here.
- **Tablet = permanent drawer.** `targetSdk` 36 means the app is built
  against the Android 16 large-screen rules, where orientation restrictions
  are ignored on large screens: a tablet window can be portrait whatever the
  manifest asks for, so the drawer layout has to be *correct* in portrait.
  `PermanentNavigationDrawer` is the list-detail scaffold (the
  `NavigationSplitView` analogue): the list stays visible at every width the
  tablet reaches and nothing has to be written to collapse it.
- **The idiom test is `smallestScreenWidthDp >= 600`.** The iOS twin of this
  predicate had to check both the width size class and the idiom because a
  Max-sized iPhone in landscape reports a regular width class. On Android the
  two halves collapse into one: `smallestScreenWidthDp` is the device's
  smallest short edge, so a phone in landscape cannot fake a tablet — and the
  Compose window-size-class composables it would have approximated were
  removed in Foundation 1.10 anyway.

### Measured on the AVDs

Measured from `dumpsys activity com.wattracker.android` (`mCurrentConfig`),
not assumed from the profile metadata:

| device          | orientation | `smallestScreenWidthDp` | config orient / rotation | shell shown |
|-----------------|-------------|-------------------------|--------------------------|-------------|
| `medium_phone`  | landscape   | **411dp**               | `land` / ROTATION_90     | leading rail |
| `medium_tablet` | portrait    | **800dp**               | `port` / ROTATION_90     | permanent drawer |
| `medium_tablet` | landscape   | **800dp**               | `land` / ROTATION_0      | permanent drawer |

Two things the measurement confirms (the plan asked to check, not trust the
changelog):

- The tablet's `smallestScreenWidthDp` is **800dp in both orientations** — it
  is the device's smallest short edge, so rotating does not move it across the
  600dp line. The `smallestScreenWidthDp >= 600` idiom predicate is therefore
  **stable across rotation**: the drawer never flips to a rail on this device.
- **Both orientations are actually reachable on the tablet** under `targetSdk`
  36. The Android 16 large-screen rule that orientation restrictions are
  ignored on large screens is real: a `permanentDrawer`-only layout is shown
  correctly in portrait, which is exactly why the drawer (not a collapsible
  rail) is the right scaffold.

## Reaching the local backend

### Release: HTTPS, terminated by whatever front end you run

The local desktop server is reached at `https://<hostname>`, with TLS
terminated in front of it. **Both are accepted and the app cannot tell them
apart:**

- **`tailscale serve`** — `https://<machine>.<tailnet>.ts.net`, the path
  `docs/calendar-feed.md` documents and the reason `WATTRACKER_PUBLIC_SCHEME`
  already defaults to `https`. Keeps the server off the public internet;
  requires a Tailscale account.
- **An ordinary reverse proxy** — nginx, Caddy, anything that terminates TLS
  with a certificate your phone already trusts. No account, no third party.

**Whichever you pick, it must not be publicly reachable.** A tailnet, a
LAN-only bind, or a VPN all qualify; an internet-facing proxy does not. This is
not only about transport secrecy: the server's `127.0.0.1` default is what
keeps the desktop UI unreachable by anyone who is not at the keyboard, and **a
reverse proxy defeats that even while the server stays bound to loopback,
because the proxy is what accepts the connection.** `/login` and the rest of
the UI would then be internet-facing, against auth that was never designed for
it. Terminate TLS somewhere only your own devices can dial.

Pick either. The server itself keeps binding loopback and speaks plain http to
the co-located terminator, so **no bind variable changes**: `127.0.0.1:8000` is
what it dials, and that is a loopback connection.

Three variables on the server, none of them a bind (see `README.md` in the
repo root, "Reaching the server from other devices"):

```
WATTRACKER_COOKIE_SECURE=1
WATTRACKER_PUBLIC_SCHEME=https
WATTRACKER_PUBLIC_HOSTS=<the hostname the phone uses>
```

The last one matters because the server 400s any request whose `Host` header it
does not answer to — so it is the tailnet name under `tailscale serve`, or the
proxy's `server_name` otherwise. The front end must pass the original `Host`
through (in nginx, `proxy_set_header Host $host;`; `tailscale serve` already
does) and forward at the root — the app has no `root_path` support.

**`WATTRACKER_COOKIE_SECURE` must be set in the launch environment, not
exported into a shell that later runs the test suite:** `conftest`'s `delenv`
list does not clear it, and a stray export produces mass test failures that
look unrelated to it (see #249).

The app pins no hostname anywhere — not in the manifest, not in the network
security config, not in `BuildConfig`. The rider types the origin at pairing
and any `https://` host, name or port is accepted.

`WATTRACKER_HOST` + `WATTRACKER_ALLOW_NON_LOOPBACK=1` are needed **only** if
the terminator runs on a different machine than the server. That gate is right
and stays: binding beyond loopback is what turns the personal app into a
networked service, and there a host firewall rule is what limits who may dial
the port.

### Debug: the emulator talks to loopback

The debug build's `BuildConfig` points at `10.0.2.2:8765` and the debug network
security config permits cleartext to `localhost`/`127.0.0.1`/`10.0.2.2` only.
`10.0.2.2` is the emulator's NAT alias for **the host's loopback**, so a server
on its default `127.0.0.1` bind is already reachable — no wider bind is needed
for the emulator path. Only the Host allowlist needs the name:

```
set WATTRACKER_PUBLIC_HOSTS=10.0.2.2
python -m wattracker
```

Never point an emulator at `127.0.0.1` — that is the emulator's own loopback.
The debug config lives in `src/debug/res/xml/`, so it cannot ship.

### Debug: a physical phone talks to loopback too, over USB

A tethered device needs no wider bind and no TLS terminator either. `adb
reverse` maps the phone's own loopback to the host's:

```
adb reverse tcp:8765 tcp:8765
```

Then point the app at `http://localhost:8765` — already permitted by the debug
network security config above, so there is nothing to change. Set
`WATTRACKER_PUBLIC_HOSTS=localhost` for the Host allowlist and leave the server
on its default `127.0.0.1` bind.

This is the reason the Android dev loop never wants `--lan` or
`WATTRACKER_HOST=0.0.0.0`. iOS binds every interface only because it has no
`adb reverse` equivalent; do not copy that posture here.

## Dependencies

Only AndroidX, Kotlin stdlib, and the two AndroidX extras the plan names:
Room and Tink (`security-crypto`). The shell adds navigation-compose (AndroidX)
The five nav icons are vendored as `ImageVector`s in `ui/theme/Icons.kt`
(Material Design path data). Only `Settings` comes from `material-icons-core`;
the other four are hand-vendored to avoid the 50 MB `material-icons-extended`
AAR. Any other third-party
artifact fails review; if a later step needs one, it comes back to the owner
first (the plan's wording).

## How an IDE agent drives this

There are no MCP servers in this environment. An agent (or a human) drives the
app through exactly two surfaces, and the commands above are the whole
toolchain:

- `.\gradlew.bat <task>` for build/assemble (the wrapper pins the Gradle
  version, so this is reproducible off-IDE).
- `adb` for install/start/logcat and UI inspection. For a **visual
  screenshot** on this machine use the IDE's emulator capture (the on-disk
  `screencap` paths return a stale lock frame — see the note above). For a
  **scriptable, per-device** UI check that needs no surface capture, dump the
  hierarchy: `adb -s <serial> shell uiautomator dump /sdcard/ui.xml` then
  `adb -s <serial> pull /sdcard/ui.xml out.xml` and parse the `node` bounds.
  `adb logcat -d` for logs.

If the Android Studio AI assistant is used, it runs in bring-your-own-key
mode against the same shell: it reads `gradlew` output and `adb` output, not
a private channel. Anything that cannot be expressed as `gradlew` + `adb`
should be treated as a toolchain gap to file, not a workaround to invent.
