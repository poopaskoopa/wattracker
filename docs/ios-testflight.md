# Shipping the iOS app to TestFlight

The pipeline is `.github/workflows/ios-release.yml`. Pushing a tag that starts
with `ios-v` archives, signs, exports and uploads a build to TestFlight. That
is the whole of the release procedure once the one-time setup below is done.

```sh
git tag ios-v0.1.0
git push origin ios-v0.1.0
```

`ios-v`, not `v`: `macos-release.yml` and `windows-release.yml` both fire on
`v*`, and a shared prefix would mean every desktop release tag also started an
iOS archive on the one physical macOS runner.

## The one-time manual setup, in order

**One thing here can never be automated, and the reason is worth stating once:
the App Store Connect API cannot create an app record.** There is no
`POST /v1/apps`. The API can read apps, manage builds, testers and metadata for
an app that exists, and it can issue certificates and profiles — but the record
itself is created by a human in the web UI, once, and never again.

Step 2 *is* automated, by a script in this repository, but it is still a
one-time act with a yearly expiry rather than something CI does on every run.
Read it as setup, not as plumbing.

Do these in this order. Steps 1 and 2 are on the Apple Developer portal; step 3
is App Store Connect; they are different websites for the same account.

1. **Register the bundle identifier.**
   [developer.apple.com](https://developer.apple.com/account) → Certificates,
   Identifiers & Profiles → Identifiers → **+** → App IDs → App → Description
   `WatTracker`, Bundle ID **explicit**, `com.wattracker.ios`. Leave every
   capability off — the app uses none. Register.

   *You can skip this step.* `-allowProvisioningUpdates` with the API key
   registers the App ID itself on the first archive, which is why the release
   job passes it. Doing it by hand first is still the recommended path, because
   it is what lets the very first `ios-v*` tag actually ship: if the identifier
   does not exist yet, the first run registers it and then fails at the upload
   (step 3 has not happened, so there is no app record to upload to), and a
   second tag is needed. One minute in a web form saves a wasted release.

2. **Create the Apple Distribution certificate and the App Store profile.**

   ```sh
   export ASC_KEY_ID=...        # the 10-character API key id
   export ASC_ISSUER_ID=...     # the account's issuer UUID
   export ASC_KEY_PATH=~/.appstoreconnect/private_keys/AuthKey_$ASC_KEY_ID.p8
   python scripts/ios_distribution_cert.py --create-profile
   ```

   This step used to say the opposite — that the release job issued the
   certificate itself through the API key, and that creating one by hand would
   waste a slot. **That was wrong, and it is why the first attempts at this
   pipeline could not archive.** `-allowProvisioningUpdates` plus an App Store
   Connect API key registers App IDs and creates and downloads *provisioning
   profiles*; it does not issue *certificates*. It cannot: a certificate is a
   private key that Apple never sees, so something local has to generate the
   key and the CSR, and `xcodebuild` will not do that on its own. Left without
   one, automatic signing finds only the account's development certificates and
   the archive fails with one of two messages that both mean "there is no
   distribution identity here":

   > WatTracker has conflicting provisioning settings. WatTracker is
   > automatically signed for development, but a conflicting code signing
   > identity Apple Distribution has been manually specified.

   > "WatTracker" has entitlements that require signing with a development
   > certificate.

   What the script does: generates an RSA 2048 key and a CSR outside any git
   tree, `POST`s the CSR to `/v1/certificates` as `certificateType`
   `DISTRIBUTION` (the modern type; `IOS_DISTRIBUTION` is the legacy name and
   is not needed), converts the returned certificate to PEM, and bundles key +
   certificate + the Apple WWDR intermediate into a `.p12` under a random
   256-bit passphrase. `--create-profile` then creates the `WatTracker App
   Store` provisioning profile against that certificate and the bundle
   identifier — the profile the release job signs with by name.

   Everything it writes is mode 0600 in `~/.appstoreconnect/`, and it refuses
   to write into a git work tree at all.

   Then load the certificate into CI, piped rather than pasted:

   ```sh
   base64 < ~/.appstoreconnect/wattracker-dist.p12 | \
     gh secret set IOS_DIST_P12_B64 --env ios-code-signing
   gh secret set IOS_DIST_P12_PASSWORD --env ios-code-signing \
     < ~/.appstoreconnect/wattracker-dist.p12.pass
   ```

   **The certificate expires one year after issue** and nothing renews it. The
   script prints the exact date; put it in a calendar. See "Rotating the
   distribution certificate" below.

   The script refuses to create a second certificate unless you pass `--force`,
   which is the one true part of the old advice: Apple caps distribution
   certificates at two or three per account, and a spare burned here is one you
   cannot create when you need it.

3. **Create the App Store Connect app record.**
   [appstoreconnect.apple.com](https://appstoreconnect.apple.com) → Apps → **+**
   → New App.
   - Platform: **iOS**
   - Name: `WatTracker` (must be unique across the entire App Store; if it is
     taken, pick another — the name here is the App Store listing name, not the
     name on the home screen, which comes from `CFBundleDisplayName`)
   - Primary Language: English (U.S.)
   - Bundle ID: `com.wattracker.ios` — it appears in this dropdown only if
     step 1 or a previous archive registered it
   - SKU: any string unique within the account; `wattracker-ios` is fine. It is
     internal, never shown to anyone, and cannot be changed later.
   - User Access: Full Access

4. **Push the tag.** From here on, releases are `git tag ios-v… && git push`.

### Export compliance, which will stop the first build

App Store Connect asks an export-compliance question about encryption before a
build can be distributed to any tester, and it asks it per build unless the app
answers it in advance. WatTracker's iOS app uses HTTPS and CryptoKit P-256
signatures for request authentication, which is the shape of use Apple's
`ITSAppUsesNonExemptEncryption = false` declaration exists for — but that
declaration is a legal statement about the account's own product, so it is the
owner's to make, not CI's, and it is deliberately not in `Info.plist` today.

Until it is added, expect to answer the question by hand in App Store Connect
for each uploaded build. Adding it is one key in
`ios/WatTracker/WatTracker/Info.plist`.

## Credentials

They live in the **`ios-code-signing`** GitHub *environment* on this
repository, not in repository-wide secrets, so only a job that declares
`environment: ios-code-signing` can read them. Six secrets:

| Secret | What it is |
| --- | --- |
| `APPLE_TEAM_ID` | The 10-character Apple Developer team identifier |
| `APP_STORE_CONNECT_KEY_ID` | The 10-character App Store Connect API key id |
| `APP_STORE_CONNECT_ISSUER_ID` | The account's issuer UUID |
| `APP_STORE_CONNECT_PRIVATE_KEY` | The full PEM contents of the `.p8`, newlines included |
| `IOS_DIST_P12_B64` | The distribution certificate and its private key, as a base64 `.p12` |
| `IOS_DIST_P12_PASSWORD` | The random passphrase protecting that `.p12`, with no trailing newline |

The last two are the actual signing key, and they are the most sensitive thing
in this list: the API key can be revoked and replaced in a minute, while the
distribution key signs everything the account ships and is capped at two or
three per account. `IOS_DIST_P12_PASSWORD` must be stored with **no trailing
newline** — `gh secret set` stores stdin verbatim, and a stored `"<hex>\n"`
makes `security import -P` fail on the runner with "wrong password" for a
reason nobody would ever find.

**None of those six values appears anywhere in this repository, and none may.**
This repository is public. A team identifier and a key id are account
identifiers: once one is in a git history on github.com it is public
permanently, and unlike the key itself it cannot be rotated. That is why
`DEVELOPMENT_TEAM` is empty in `Config/Base.xcconfig`, why
`ios/WatTracker/ExportOptions.plist` has no `teamID` key, and why the release
job builds its export options into a temporary copy at run time.

The private key reaches the runner as a file in `$RUNNER_TEMP`, created under
`umask 077`, and is removed by an `if: always()` step. It is never written to
the workspace, never to `DerivedData`, and never uploaded.

### Rotating the API key

Do this if the key leaks, if the person who created it leaves, or on whatever
schedule you decide — there is no expiry, which is exactly why a schedule is
worth having.

1. App Store Connect → Users and Access → **Integrations** → App Store Connect
   API → **+**. Name it, give it the **App Manager** role. Download the `.p8`
   **immediately**; Apple will not offer it a second time.
2. Update `APP_STORE_CONNECT_KEY_ID` and `APP_STORE_CONNECT_PRIVATE_KEY` in the
   `ios-code-signing` environment. `APP_STORE_CONNECT_ISSUER_ID` does not change
   — it identifies the account, not the key.
3. Push a tag and confirm a build reaches TestFlight.
4. Only then, revoke the old key in the same Integrations page.

Keep the local copy at `~/.appstoreconnect/private_keys/AuthKey_<KEYID>.p8`,
mode 600, if you want to run `xcrun altool` by hand. The release job
deliberately does **not** read that directory — it passes `--p8-file-path`
explicitly, so a job can never silently authenticate with a credential that
happens to be sitting in the runner user's home directory.

### Rotating the distribution certificate

**This one has a deadline.** An Apple Distribution certificate is valid for one
year from issue. Nothing renews it, nothing warns you, and the failure when it
lapses is a red release job — the archive cannot find a valid identity — at the
moment you wanted to ship. Put the expiry date the creation script printed in a
calendar, with a reminder a month early.

Rotate early rather than on the day, and in this order. Both the old and the
new certificate can be valid at once, which is what makes the order safe:

1. `python scripts/ios_distribution_cert.py --force --create-profile`. `--force`
   is needed because the account still has the old certificate; the script
   refuses without it precisely so this is a decision and not an accident.
   `--create-profile` reissues the App Store profile against the new
   certificate — a profile embeds the certificates it accepts, so an old
   profile will not validate a new signature.
2. Update `IOS_DIST_P12_B64` and `IOS_DIST_P12_PASSWORD` in the
   `ios-code-signing` environment, with the two piped `gh secret set` commands
   the script prints.
3. Push an `ios-v*` tag and confirm the build reaches TestFlight.
4. **Only then**, revoke the old certificate in the developer portal
   (Certificates, Identifiers & Profiles → Certificates). Revoking first would
   invalidate the profile the current pipeline signs with and leave you with no
   working path and a deadline.

The same order applies if the key leaks, with one change: revoke immediately at
step 1 and accept the outage, because a leaked distribution key can sign
software as this account until it is revoked. Builds already on TestFlight are
unaffected — revocation does not invalidate a signature Apple already accepted.

Two things that are *not* rotation. Renewing the App Store Connect API key does
not touch the certificate; they are independent credentials with independent
lifetimes. And re-running the release job does not renew anything — the
workflow only ever imports the certificate it is given.

## Build numbers

The release job sets both version numbers on the `xcodebuild` command line;
nothing on disk changes.

- `CFBundleShortVersionString` comes from the tag: `ios-v0.1.0` → `0.1.0`. The
  job rejects a tag that is not one to three dot-separated integers after the
  prefix.
- `CFBundleVersion` is `<run_number>.<run_attempt>`.

App Store Connect **rejects** an upload whose build number it has already
accepted for that version string, and it rejects it after the archive, the
export and the upload have all run. `run_number` never repeats. `run_attempt`
is there for the specific case of re-running a failed run from the Actions UI:
that keeps `run_number`, so without it a re-run would send Apple the exact
build number it just refused and fail for a reason unrelated to the original
failure.

The consequence: the build number is a property of the CI run, not of the tag.
Re-tagging the same commit produces a different build number, which is correct.

## TestFlight builds expire after 90 days

This is the thing that surprises people. A build uploaded to TestFlight stops
being installable **90 days after upload**, for internal and external testers
alike. There is no extension, no renewal, and no setting. The counter starts at
upload, not at first install, so a build that sat in processing for a day is a
day closer to expiry when a tester first sees it.

**Re-issuing is just cutting another release.** Push a new `ios-v*` tag; the
new build gets a fresh 90 days and testers are prompted to update. If nothing
in the app changed, re-tag the same commit — the build number differs by
construction, so Apple accepts it.

Two practical notes:

- Internal testers (up to 100, all with App Store Connect access on the
  account) get builds as soon as processing finishes, with no review. External
  testing needs a Beta App Review for the first build of a version.
- Expiry is per build, not per app. Older builds expiring does not affect a
  newer one.

## What the release job does, step by step

1. Checks out the tag with `persist-credentials: false`.
2. Derives the version from the tag and the build number from the run. Nothing
   secret is in scope yet, so a malformed tag fails before any credential is
   written to disk.
3. Runs the Swift test suite on the iPhone 17 Pro simulator. A failing test
   costs nothing but time at this point.
4. Writes the `.p8` to `$RUNNER_TEMP` under `umask 077`, verifies it parses as
   a private key, and templates `teamID` into a temporary copy of
   `ExportOptions.plist`.
5. Decodes `IOS_DIST_P12_B64` into `$RUNNER_TEMP`, creates a keychain with a
   random password, `security import`s the certificate into it with `-x` (not
   extractable), runs `set-key-partition-list` so `codesign` does not block on
   a GUI prompt, puts that keychain first in the search list, deletes the
   `.p12`, and asserts an `Apple Distribution` identity actually landed. A
   dedicated keychain, not the runner user's login keychain, because this
   runner is a physical machine that persists between jobs.
6. `xcodebuild archive` for `generic/platform=iOS`, with **manual** signing:
   `CODE_SIGN_IDENTITY="Apple Distribution"`,
   `PROVISIONING_PROFILE_SPECIFIER="WatTracker App Store"`, and
   `OTHER_CODE_SIGN_FLAGS="--keychain …"` pinning `codesign` to the job's own
   keychain. `-allowProvisioningUpdates` and the three `-authenticationKey*`
   flags are still there, now doing the one job they can do: registering the
   App ID and downloading that profile from the portal, so it is neither
   installed on the runner nor carried as a secret.

   Manual rather than automatic, and this is the part that took the longest to
   get right. Xcode decides development-vs-distribution for *automatic* signing
   from the target's `ProvisioningStyle` attribute in the `.pbxproj`, and this
   project deliberately has none — every signing setting lives in
   `Config/Base.xcconfig` so that a credential-less clone still builds for a
   simulator. With nothing to read, automatic signing resolves to development
   and the archive fails with one of the two messages quoted in step 2 of the
   setup, *even once a distribution certificate exists*. Naming the profile
   removes the guess, and an explicit profile is what a CI job wants anyway.

   The signing settings from `Config/Base.xcconfig` — which ad-hoc sign — are
   overridden here on the command line and nowhere on disk.
7. `xcodebuild -exportArchive` with the templated options, then asserts no
   `.p8` ended up inside the archive or the export. The export **re-signs**, so
   it needs the signing keychain too — which is why the cleanup that deletes it
   is at the end of the job and not straight after the archive.
   `ios/WatTracker/ExportOptions.plist` names the same profile the archive used
   (`signingStyle` `manual`, `provisioningProfiles` mapping
   `com.wattracker.ios`); a mismatch there re-signs with something else and is
   not caught until Apple rejects the upload.
8. `xcrun altool --validate-app`, then `--upload-app`. Validation first because
   it is where a missing app record surfaces in seconds rather than after a
   full upload.
9. An `if: always()` step restores the keychain search list, deletes the
   signing keychain, and removes the API key, the templated plist, the archive,
   the export and both DerivedData trees — and fails the job if either the
   keychain or the key directory survives. The runner is a physical machine
   that is not discarded between jobs, which is the whole reason that step
   exists: a distribution private key left in a keychain there is usable by
   every later job on the box.

**No `.ipa` is uploaded as a build artifact, on purpose.** A
distribution-signed `.ipa` embeds `embedded.mobileprovision`, whose
`TeamIdentifier` is the team id, and the signing certificate's subject carries
it too. Artifacts on a public repository are downloadable by anyone, so
publishing the `.ipa` would publish the one identifier the rest of this design
keeps out of the tree. Nothing needs it: the build's destination is TestFlight.

## Reading a failure

- `No App Store Connect app record` / `The bundle ID could not be found` at the
  validate step — the one-time setup above has not been done, or the bundle id
  in the record does not match `com.wattracker.ios`.
- `did not parse as a private key` — `APP_STORE_CONNECT_PRIVATE_KEY` was pasted
  without its newlines or without the BEGIN/END lines. It must be the file's
  full contents.
- `The ios-code-signing environment did not supply the signing credentials` /
  `... did not supply the distribution certificate` — the job lost its
  `environment:` declaration, or a secret was renamed.
- `SecKeychainItemImport: MAC verification failed during PKCS12 import (wrong
  password?)` or `Unknown format in import` at the import step — the `.p12` was
  built with OpenSSL 3's defaults. macOS's `security` tool understands only the
  legacy PKCS#12 algorithms; rebuild with `-keypbe PBE-SHA1-3DES -certpbe
  PBE-SHA1-3DES -macalg sha1`, which is what `scripts/ios_distribution_cert.py`
  does. `MAC verification failed` is also what a trailing newline in
  `IOS_DIST_P12_PASSWORD` looks like. `Unknown format in import` has one other
  cause worth knowing when reproducing this by hand: `security import` picks
  its format from the file *extension*, so a `.p12` saved under any other
  suffix is rejected unmodified. The workflow always writes `.p12`.
- `No Apple Distribution identity landed in the signing keychain` — the `.p12`
  decoded, but no identity in it chains to a trusted root inside the job's
  keychain. `find-identity -v` lists only *valid* identities, and validity
  needs the whole chain.

  Check the bundled intermediate before you suspect the certificate itself.
  Apple has issued several generations of the WWDR intermediate (G2..G6) that
  all share one common name and differ only in the OU, and bundling the wrong
  generation fails exactly here: the import step still reports `1 identity
  imported. 1 certificate imported.`, and the identity is still invalid because
  nothing in the keychain issued it.

  This is invisible on a developer machine. A local check passes because
  `login.keychain-db` is in the search list and already holds every generation
  the machine has ever used, so the chain resolves from there rather than from
  the `.p12`. To reproduce what CI sees, take the login keychain out of the
  search list — `security list-keychains -d user -s "$keychain"` with nothing
  else — and restore the full previous list afterwards, which on a real
  workstation is usually more than just `login.keychain-db`.

      openssl x509 -in ~/.appstoreconnect/wattracker-dist.pem -noout -issuer

  names the generation that must be in the `.p12`;
  `scripts/ios_distribution_cert.py` now selects on that issuer and refuses to
  build a `.p12` around any other one.

  A development certificate in the `.p12` produces the same message, but it is
  the rarer cause.
- `conflicting provisioning settings` or `has entitlements that require signing
  with a development certificate` at the archive — the signing settings drifted
  back to `CODE_SIGN_STYLE=Automatic`. See step 6 above; automatic signing
  cannot work for this project.
- The archive hangs at `CodeSign` until the job times out, with no error — the
  `set-key-partition-list` call did not run or did not take, and `codesign` is
  waiting on a keychain-access dialog nobody can click.
- `Your account does not have permission to create a new certificate` /
  a 409 from `/v1/certificates` — the account is at Apple's distribution
  certificate cap. Revoke an expired one in the portal first.
- A duplicate-build-number rejection should be impossible. If it happens, the
  workflow was re-run in a way that reused `run_number` and `run_attempt`
  together; cut a new tag.
