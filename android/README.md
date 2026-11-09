# android/

The wattracker Android client is a single-activity Jetpack Compose app. It reads
from the local desktop app over proxied HTTPS, or from the hosted cloud. Both
backends use TLS. The app never uses cleartext in a release build.

`plan.md` is the authoritative step-by-step plan for this directory. This file
records the machine assumptions that the steps depend on. `AGENTS.md` holds the
standing working rules and machine facts. Read `AGENTS.md` before you do any
work here.

The app shell (issue #193) has five destinations: Dashboard, Activities,
Calendar, Volume, and Settings. One navigation model presents them as a leading
navigation rail on phones and a permanent navigation drawer on tablets. The
screens are stubs. The cloud/local split, Room, and the Keystore-backed
credential store arrive in steps 2–4 (#194–#196).

## Build and run

```
.\gradlew.bat :app:assembleDebug          # from this directory
adb install -r app\build\outputs\apk\debug\app-debug.apk
adb shell am start -n com.wattracker.android/.MainActivity
```

`targetSdk` is 36 and `minSdk` is 30. Release builds use the production network
security config, which denies cleartext.

Release builds read the cloud host from `releaseCloudAuthority` in
`app/build.gradle.kts`. It comes from a Gradle property or `local.properties`
`wattrackerCloudAuthority`, and defaults to `cloud.wattracker.example`. #102 is
closed and the cloud is deployed, so set the real authority. Do not ship the
placeholder. Until it is set, the guard refuses `assembleRelease` and
`bundleRelease` unless you pass `-PallowPlaceholderHost`.

### The two AVDs

The shell is validated on both idioms, so two AVDs are the working set. The new
`android` CLI creates them from **size profiles**. They are not `avdmanager`
device models: the old tool fails with an NPE on this machine's SDK, and the new
CLI has no `pixel_9` or `pixel_tablet`. A profile is the closest equivalent.

| name           | profile         | image that gets pulled                       | role                        |
|----------------|-----------------|----------------------------------------------|-----------------------------|
| `medium_phone` | `medium_phone`  | google_apis (Play Store), x86_64, **API 36** | rail shell, landscape       |
| `medium_tablet`| `medium_tablet` | google_apis (Play Store Tablet), x86_64, **API 35** | drawer shell, both |

`medium_tablet` is the concrete meaning of the plan's "tablet in landscape"
acceptance criterion. `medium_phone` is the landscape phone that justifies the
rail. Create and boot them with the `android` CLI. The CLI downloads the image,
and `start` blocks until the device is fully up:

```
android emulator create medium_phone
android emulator create medium_tablet
android emulator start medium_phone      # returns when booted
```

> The tablet's only x86_64 image is **API 35**, not 36. The CLI pulls
> `google_playstore_tablet/x86_64-35`. The app's `minSdk` is 30, so the app runs.
> The phone stays on API 36. This deviates from the plan's "API 36" for both
> devices, and the PR records it.

On this machine, the emulators run without KVM (Windows, Hypervisor-driven), so
the first boot is slow. The emulator causes this, not the app. For screenshots,
`adb shell screencap` on this machine returns a stale lock frame even when
`isKeyguardShowing=false`. The IDE's screenshot of the running emulator is the
reliable path. The full set of capture and inspection commands is in `AGENTS.md`
("Inspecting and capturing the UI").

## Why a rail on the phone and a drawer on the tablet

The full arithmetic is in the KDoc on `RootScreen.kt`; the short version:

- **Phone = leading rail.** The phone is landscape, so the viewport is wide and
  short (about 900x400dp on a Pixel 9 before insets). A bottom bar costs about
  80dp of the 400dp of height on every screen. That is a fifth of the scarce
  axis. An 80dp rail costs less than a tenth of the 900dp of width, and it sits
  under the left thumb, where the device is held in landscape. This departs
  from the platform default on purpose. The default is correct in portrait and
  wrong here.
- **Tablet = permanent drawer.** With `targetSdk` 36, the app uses the Android
  16 large-screen rules. Those rules ignore orientation restrictions on large
  screens, so a tablet window can be portrait whatever the manifest asks for.
  The drawer layout therefore must be *correct* in portrait.
  `PermanentNavigationDrawer` is the list-detail scaffold, the
  `NavigationSplitView` analogue. The list stays visible at every width the
  tablet reaches, and no code has to collapse it.
- **The idiom test is `smallestScreenWidthDp >= 600`.** The iOS twin of this
  predicate had to check both the width size class and the idiom, because a
  Max-sized iPhone in landscape reports a regular width class. On Android, the
  two halves collapse into one. `smallestScreenWidthDp` is the device's smallest
  short edge, so a phone in landscape cannot look like a tablet. Foundation 1.10
  removed the Compose window-size-class composables that would have approximated
  it.

### Measured on the AVDs

Measured from `dumpsys activity com.wattracker.android` (`mCurrentConfig`),
not assumed from the profile metadata:

| device          | orientation | `smallestScreenWidthDp` | config orient / rotation | shell shown |
|-----------------|-------------|-------------------------|--------------------------|-------------|
| `medium_phone`  | landscape   | **411dp**               | `land` / ROTATION_90     | leading rail |
| `medium_tablet` | portrait    | **800dp**               | `port` / ROTATION_90     | permanent drawer |
| `medium_tablet` | landscape   | **800dp**               | `land` / ROTATION_0      | permanent drawer |

The measurement confirms two points. The plan asked for a check, not a reliance
on the changelog:

- The tablet's `smallestScreenWidthDp` is **800dp in both orientations**. It is
  the device's smallest short edge, so rotation does not move it across the
  600dp line. The `smallestScreenWidthDp >= 600` idiom predicate is therefore
  **stable across rotation**. The drawer never flips to a rail on this device.
- **The tablet reaches both orientations** under `targetSdk` 36. The Android 16
  large-screen rule is real: orientation restrictions are ignored on large
  screens. A `permanentDrawer`-only layout shows correctly in portrait. That is
  why the drawer, not a collapsible rail, is the right scaffold.

## Reaching the local backend

### Release: HTTPS, terminated by whatever front end you run

The app reaches the local desktop server at `https://<hostname>`, with TLS
terminated in front of it. **Both options are accepted, and the app cannot tell
them apart:**

- **`tailscale serve`** gives `https://<machine>.<tailnet>.ts.net`. This is the
  path that `docs/calendar-feed.md` documents, and the reason
  `WATTRACKER_PUBLIC_SCHEME` already defaults to `https`. It keeps the server off
  the public internet. It needs a Tailscale account.
- **An ordinary reverse proxy** is nginx, Caddy, or any service that terminates
  TLS with a certificate the phone already trusts. It needs no account and no
  third party.

**Your choice must not be publicly reachable.** A tailnet, a LAN-only bind, or a
VPN qualifies. An internet-facing proxy does not. Transport secrecy is not the
only reason. The server's `127.0.0.1` default keeps the desktop UI unreachable
for anyone who is not at the keyboard. **A reverse proxy defeats that even while
the server stays bound to loopback, because the proxy accepts the connection.**
`/login` and the rest of the UI would then be internet-facing, and the auth was
never designed for that. Terminate TLS where only your own devices can dial.

Pick either option. The server keeps binding loopback and speaks plain http to
the co-located terminator, so **no bind variable changes**. It dials
`127.0.0.1:8000`, which is a loopback connection.

Three variables on the server, none of them a bind (see `README.md` in the
repo root, "Reaching the server from other devices"):

```
WATTRACKER_COOKIE_SECURE=1
WATTRACKER_PUBLIC_SCHEME=https
WATTRACKER_PUBLIC_HOSTS=<the hostname the phone uses>
```

The last variable matters because the server returns 400 for any request whose
`Host` header it does not answer to. It is the tailnet name under `tailscale
serve`, or the proxy's `server_name` otherwise. The front end must pass the
original `Host` through, for example with `proxy_set_header Host $host;` in
nginx. `tailscale serve` already does this. The front end must also forward at
the root. The app has no `root_path` support.

**Set `WATTRACKER_COOKIE_SECURE` in the launch environment. Do not export it
into your shell.** Use the same care for any other server environment variable
that a phone-facing run needs.

The app pins no hostname. It is not in the manifest, the network security
config, or `BuildConfig`. The rider types the origin at pairing, and the app
accepts any `https://` host, name, or port.

`WATTRACKER_HOST` + `WATTRACKER_ALLOW_NON_LOOPBACK=1` are needed **only** if
the terminator runs on a different machine than the server. That gate stays,
because it is correct. Binding beyond loopback turns the personal app into a
networked service. In that case, a host firewall rule limits who can dial the
port.

### Debug: the emulator talks to loopback

The debug build's `BuildConfig` points at `10.0.2.2:8765`. The debug network
security config permits cleartext to `localhost`, `127.0.0.1`, and `10.0.2.2`
only. `10.0.2.2` is the emulator's NAT alias for **the host's loopback**, so a
server on its default `127.0.0.1` bind is already reachable. The emulator path
needs no wider bind. Only the Host allowlist needs the name:

```
set WATTRACKER_PUBLIC_HOSTS=10.0.2.2
python -m wattracker
```

Never point an emulator at `127.0.0.1`. That is the emulator's own loopback.
The debug config lives in `src/debug/res/xml/`, so it cannot ship.

### Debug: a physical phone talks to loopback too, over USB

A tethered device needs no wider bind and no TLS terminator. `adb reverse` maps
the phone's own loopback to the host's:

```
adb reverse tcp:8765 tcp:8765
```

Then point the app at `http://localhost:8765`. The debug network security config
above already permits it, so nothing else changes. Set
`WATTRACKER_PUBLIC_HOSTS=localhost` for the Host allowlist, and leave the server
on its default `127.0.0.1` bind.

For this reason, the Android dev loop never needs `--lan` or
`WATTRACKER_HOST=0.0.0.0`. iOS binds every interface only because it has no
`adb reverse` equivalent. Do not copy that approach here.

## Dependencies

The app uses only AndroidX, the Kotlin standard library, and the AndroidX extra
that the plan names: Room.

`security-crypto` and its Tink tree left the app with #457. The credential store
is wrapped by an Android Keystore AES-GCM key directly, so no crypto library is
in the dependency graph. The shell adds navigation-compose (AndroidX).

The five navigation icons are vendored as `ImageVector`s in `ui/theme/Icons.kt`
with Material Design path data. Only `Settings` comes from `material-icons-core`.
The other four are vendored by hand to avoid the 50 MB
`material-icons-extended` AAR.

Any other third-party artifact fails review. If a later step needs one, it
comes back to the owner first.

## How an IDE agent drives this

There are no MCP servers in this environment. An agent or a human drives the app
through exactly two surfaces. The commands above are the whole toolchain:

- `.\gradlew.bat <task>` for build and assemble. The wrapper pins the Gradle
  version, so the build is reproducible outside the IDE.
- `adb` for install, start, logcat, and UI inspection. For a **visual
  screenshot** on this machine, use the IDE's emulator capture. The on-disk
  `screencap` paths return a stale lock frame; see the note above. For a
  **scriptable, per-device** UI check that needs no surface capture, dump the
  hierarchy. Run `adb -s <serial> shell uiautomator dump /sdcard/ui.xml`, then
  `adb -s <serial> pull /sdcard/ui.xml out.xml`, then parse the `node` bounds.
  Use `adb logcat -d` for logs.

If you use the Android Studio AI assistant, it runs in bring-your-own-key mode
against the same shell. It reads `gradlew` output and `adb` output, not a private
channel. Treat anything that `gradlew` + `adb` cannot express as a toolchain gap
to file. Do not invent a workaround.

Setup, the dev loop, key handling and the verification requirements are in
`AGENTS.md`.
