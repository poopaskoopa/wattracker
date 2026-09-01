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

**None of this can be automated, and the reason is worth stating once: the App
Store Connect API cannot create an app record.** There is no `POST /v1/apps`.
The API can read apps, manage builds, testers and metadata for an app that
exists, and issue certificates and profiles — but the record itself is created
by a human in the web UI, once, and never again. Everything after step 3 is
automatic forever.

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

2. **Check the certificate situation — but do not create one.** The release job
   asks App Store Connect for an Apple Distribution certificate through the API
   key and manages it itself. Do not create a distribution certificate by hand:
   an account is limited to a small number of them, and a spare one created here
   is one the automated path may later be unable to replace.

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
`environment: ios-code-signing` can read them. Four secrets:

| Secret | What it is |
| --- | --- |
| `APPLE_TEAM_ID` | The 10-character Apple Developer team identifier |
| `APP_STORE_CONNECT_KEY_ID` | The 10-character App Store Connect API key id |
| `APP_STORE_CONNECT_ISSUER_ID` | The account's issuer UUID |
| `APP_STORE_CONNECT_PRIVATE_KEY` | The full PEM contents of the `.p8`, newlines included |

**None of those four values appears anywhere in this repository, and none may.**
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
5. `xcodebuild archive` for `generic/platform=iOS`, with
   `-allowProvisioningUpdates` and the three `-authenticationKey*` flags so
   Xcode registers the App ID and issues and downloads the certificate and
   profile itself. The signing settings from `Config/Base.xcconfig` — which
   ad-hoc sign, so that a clone with no credentials still builds — are
   overridden here on the command line and nowhere on disk.
6. `xcodebuild -exportArchive` with the templated options, then asserts no
   `.p8` ended up inside the archive or the export.
7. `xcrun altool --validate-app`, then `--upload-app`. Validation first because
   it is where a missing app record surfaces in seconds rather than after a
   full upload.
8. An `if: always()` step removes the key, the templated plist, the archive,
   the export and both DerivedData trees. The runner is a physical machine that
   is not discarded between jobs, which is the whole reason that step exists.

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
- `The ios-code-signing environment did not supply the signing credentials` —
  the job lost its `environment:` declaration, or a secret was renamed.
- A duplicate-build-number rejection should be impossible. If it happens, the
  workflow was re-run in a way that reused `run_number` and `run_attempt`
  together; cut a new tag.
