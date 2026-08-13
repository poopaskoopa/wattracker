# Subscribing your phone to the workout calendar

wattracker can publish your scheduled workouts as a read-only iCalendar feed at
`/calendar.ics`, so they appear in the phone calendar you already use — 30 days
back and 180 days ahead, one all-day entry each, completed sessions marked `✓`.

A calendar feed is not the only way a phone sees wattracker — a phone is a
perfectly ordinary [screen](../README.md#how-it-is-put-together), and on a home
network it can load the whole app, live ride page included. What a calendar
subscription adds is the part a browser cannot do: your workouts showing up in
the calendar you already look at, without opening anything.

The app binds to loopback by default, so out of the box nothing else on your
network can reach it. There are two ways to change that, and they suit
different things:

- **A LAN bind** — the app answers on your home network directly. This is the
  one to use if what you want is the app on your phone; see
  [Reaching the server from other devices](../README.md#reaching-the-server-from-other-devices).
  A calendar subscription works over it too, as long as the phone is home when
  the calendar refreshes.
- **Tailscale**, which is what this guide uses: your devices join a private
  network and `tailscale serve` puts an HTTPS front end on the machine that
  proxies to the loopback port. Nothing is exposed to the internet, it works
  away from home, and the cookie travels encrypted. The trade-off is the 403 on
  guarded buttons described under "Known limitations".

If you already have another way to reach the machine (a reverse proxy, a VPN),
the only wattracker-specific part is step 2 — set `WATTRACKER_PUBLIC_HOST` to
whatever hostname the phone will use.

Throughout this guide "the Mac" means **the machine running the server**. In a
split install that is the NAS or container host, not the Zwift machine the
connector runs on.

---

## Before you start

- Tailscale installed and signed in on **both** the Mac running wattracker and
  the phone. `brew install --cask tailscale`, or the Mac App Store build.
- wattracker running (`./start.sh`).

---

## 1. Find your tailnet hostname

```sh
tailscale status
```

The first line is this machine. You want its full MagicDNS name, which looks
like `macbook.tail1a2b3c.ts.net`. If you only see a short name, the full form is
`<machine>.<tailnet>.ts.net`; the Tailscale admin console shows it in full.

If this prints `Tailscale is stopped`, start it (open the app, or
`sudo tailscale up`) and run it again.

## 2. Tell wattracker that hostname

wattracker has to be told the name it will be reached by, for two reasons: it
rejects requests carrying a `Host` header it does not recognise, and the
subscription link it generates has to contain a hostname your phone can actually
resolve. Without this it would hand you `http://127.0.0.1:8000/...`, which is
your phone talking to itself.

```sh
export WATTRACKER_PUBLIC_HOST=macbook.tail1a2b3c.ts.net
./stop.sh && ./start.sh
```

The variable is read at startup, so the restart is required. It is not persisted
anywhere — put the `export` line in your shell profile, or in whatever launches
wattracker, if you want it to survive a reboot.

`WATTRACKER_PUBLIC_SCHEME` defaults to `https`, which is what `tailscale serve`
gives you. Only set it (to `http`) if you are fronting the app with something
that does not terminate TLS.

**A malformed value stops the server from starting**, with a `ValueError` in the
log naming the variable. That is deliberate — this value is appended to a
security allowlist, so it fails closed rather than guessing. It accepts a plain
hostname, optionally with `:port`. It rejects wildcards in every form, schemes,
paths, and anything with `@`, `%`, or whitespace in it.

## 3. Put an HTTPS front end on it

```sh
tailscale serve --bg 8000
```

That proxies `https://<your-tailnet-name>/` to `127.0.0.1:8000`, reachable only
by devices on your tailnet. Check it with:

```sh
tailscale serve status
```

To undo it later: `tailscale serve reset`.

## 4. Generate the subscription link — on the Mac

Open wattracker on the Mac at `http://127.0.0.1:8000`, go to **Settings →
Calendar feed**, and press **Create calendar link**.

**Do this on the Mac, not the phone.** Buttons that change something are
protected by a same-origin check that does not recognise the tailnet origin, so
pressing them from a phone browser returns a 403. Reading the feed is
unaffected. See "Known limitations" below.

The link is shown **once**. Only a hash of it is stored, so it cannot be
displayed again — copy it now. It should look like:

```
https://macbook.tail1a2b3c.ts.net/calendar.ics?token=...
```

If it still says `127.0.0.1`, step 2 did not take effect: the variable was not
exported into the environment the server was started from, or the server was not
restarted.

## 5. Subscribe on the phone

Get the link to the phone in a way you would be comfortable sending a password —
see the security note below. AirDrop or a note to yourself is fine.

**iOS** — Settings → Calendar → Accounts → Add Account → Other → *Add
Subscribed Calendar*, and paste the URL.

**Google Calendar** — calendar.google.com on a computer → Other calendars → *+*
→ *From URL*. Google fetches server-side, so it must be able to reach the host;
on a tailnet it cannot. Prefer the iOS route, or a calendar app that fetches from
the device.

The phone re-fetches on its own schedule — typically every few hours, and not
usually on demand. A newly generated plan may take a while to appear.

---

## Security

**The link is the password.** Calendar apps cannot log in, so the URL carries a
token that is the only thing standing between the link and your schedule. Anyone
who has it can read your workouts — 30 days back, 180 ahead — until you rotate
it. Treat it as a credential:

- Only a SHA-256 hash of the token is stored, so the database and its backups
  are not a copy of a live credential.
- The token is stripped from the access log.
- The feed responds `Cache-Control: private, no-store`.
- **Rotating is instant**: pressing *Generate a new calendar link* replaces the
  stored hash, and the old link stops working immediately. Do this if the link
  is ever somewhere you did not intend, then re-subscribe the phone.

**What `WATTRACKER_PUBLIC_HOST` does and does not do.** It changes which
hostname the server will *answer to*. It does not change who can *connect* — the
socket is still bound to loopback, so the only way in is the proxy on that
machine, and Tailscale authenticates that. Access control is now your Tailscale
ACLs: anyone on your tailnet can reach the whole app, not just the feed. If you
share your tailnet with other people or devices, tighten the ACLs.

**Do not point a public DNS name at this.** wattracker is a single-user local
server. Its CSRF protection is a same-origin check, the session cookie is not
`Secure`, and there is no rate limiting beyond the login form. It is not built
to sit on the open internet, and `tailscale funnel` would put it there — use
`tailscale serve`, which does not.

---

## Known limitations

- **You cannot rotate the link from a phone _over the tailnet_.** The
  same-origin check compares the browser's `Origin` against the URL the server
  thinks it is serving; `tailscale serve` speaks https to the phone and plain
  http to this loopback socket, so the scheme and port differ and every guarded
  POST returns 403. Reading the feed is unaffected. Do link generation on the
  Mac.

  This is a property of the proxy, not of the phone: on a **direct LAN bind**
  the browser and the server agree on scheme, host and port, and buttons work
  from the phone like anywhere else. See "From a phone on the same network" in
  the README, and `tests/test_phone_access.py`.
- **The live-ride page works over the tailnet, and over a LAN name.** This used
  to say it could not, and that was true when the ride socket's origin
  allowlist was a separate, narrower list. It is not any more: `_ws_origin_ok`
  consults the same validated `WATTRACKER_PUBLIC_HOSTS` setting the Host
  allowlist uses, so the ride screen connects from a phone and shows live watts
  coming from the connector. Ride actions travel on that socket rather than as
  POSTs, so the 403 above does not reach them.

---

## Troubleshooting

**400 Bad Request from the phone.** The `Host` the server received is not on its
allowlist — almost always a mismatch between `WATTRACKER_PUBLIC_HOST` and the
name actually used. Check for a typo, and confirm the value reached the running
process:

```sh
tailscale serve status                                  # what name is fronting it
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Host: macbook.tail1a2b3c.ts.net" http://127.0.0.1:8000/login
```

That should print `200`. If it prints `400`, the server is not running with the
variable set — restart it from a shell where the `export` has been done.

**The generated link says `127.0.0.1`.** Same cause; see step 4.

**Server will not start after setting the variable.** The value failed
validation. The log has a `ValueError` naming it. Use a bare hostname —
no `https://`, no trailing slash, no wildcard.

**The phone shows an empty or stale calendar.** Calendar clients answer a
failed fetch by quietly keeping what they had, so an expired link looks like a
calendar that stopped updating rather than an error. If you have rotated the
token since subscribing, the old subscription is dead — delete it and re-add it
with the new link.
