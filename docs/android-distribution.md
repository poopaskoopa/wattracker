# Android distribution — internal testing

How a WatTracker build reaches a rider's phone, and how it is promoted and
re-covered when the upload key is rotated. Step 7 (#199) is the issue; this
file is its runbook.

Android is simpler than the iOS equivalent, and it is worth naming why so
nobody budgets for the wrong thing: the Play Console developer account is a
one-time $25 fee, not an annual one; Play App Signing holds the release key,
so there is no distribution certificate or provisioning profile to rotate; and
internal-testing builds do not expire, unlike TestFlight's 90 days.

## What runs a release

`.github/workflows/android-release.yml`, and nothing else. It fires on a push
of an `android/v<major>.<minor>.<patch>` tag.

- **No `pull_request` trigger.** The job holds the upload key and the Play
  service account; a PR trigger would hand both to any fork's diff.
  `.github/workflows/android.yml` is the PR workflow and carries no signing
  material.
- **The tag is admin-only.** The "Android release tag policy" ruleset (id
  27997494) lets only the Admin role create, move or delete an `android/v*`
  tag. That ruleset is load-bearing: it is the only thing between a write
  collaborator and the signing secrets. Never add a bypass actor for Actions
  or for a workflow token.
- **The secrets live in the `android-code-signing` environment** (#486), which
  admits only `main` and `android/v*` tags and carries a required reviewer.
  A repository secret is readable by any workflow a write collaborator can
  push, on any branch; an environment secret is not.

## The five secrets

All five are at `android-code-signing` scope. None is in the repository.

| Secret | What it is |
| --- | --- |
| `ANDROID_UPLOAD_KEYSTORE_B64` | The upload keystore, base64-encoded. |
| `ANDROID_UPLOAD_KEYSTORE_PASSWORD` | The keystore's store password. |
| `ANDROID_UPLOAD_KEY_PASSWORD` | The key's own password. |
| `ANDROID_UPLOAD_KEY_ALIAS` | The key alias (`upload`). |
| `PLAY_SERVICE_ACCOUNT_JSON` | The Play Developer API service account key. |

The keystore signs the bundle; the service account authorizes the upload. They
are independent, and neither substitutes for the other.

`WATTRACKER_ANDROID_CLOUD_AUTHORITY` is the sixth, and it is not a signing
secret: it is the deployed read app's authority, a bare host with no scheme or
path. The workflow validates it before building and refuses it if it is empty,
whitespace-bearing, URI-shaped, or still ends in `.example`.

## Cutting a release

1. **Merge to `main`.** Only `main` and `android/v*` tags may reach the
   environment, so a release tag must point at a commit on `main`.
2. **Tag it.** The version comes from the tag and nowhere else:

   ```sh
   git tag android/v1.2.3 <sha-on-main>
   git push origin android/v1.2.3
   ```

   `versionName` becomes `1.2.3` and `versionCode` becomes `10203`
   (`major * 10000 + minor * 100 + patch`). Play refuses a `versionCode` it
   has already seen, so every release needs a higher one; the tag is the only
   place that number is written. A malformed tag fails the workflow before the
   build rather than silently falling back to `1`.
3. **Approve the environment.** The `android-code-signing` environment has a
   required reviewer with `can_admins_bypass: false`, so the run waits for an
   approval. The tag push alone does not ship.
4. **Watch the run.** It validates the authority and tag, decodes and checks
   the keystore, builds the signed bundle, proves the bundle carries a signer,
   mints a Play token, and commits the upload to the internal track. Nothing
   reaches the track until that last commit, so a mid-run failure leaves Play
   untouched rather than half-written.

## How a rider joins

Internal testing, not open or closed testing: up to 100 testers by email, no
review queue, builds do not expire.

1. In Play Console, open the app, then **Testing → Internal testing → Testers**.
2. Add the rider's Google account email, or hand them the opt-in link.
3. The rider opens the opt-in link, accepts, and installs from Play. This is a
   Play install, not a sideload — which matters, because the shipped release
   build has the strict network security config and no cleartext exception.
4. The rider pairs inside the app (Settings → cloud, or Settings → local). A
   pairing code is minted by the desktop app against the deployed cloud.

Two riders are expected, and the second one is not a duplicate: it proves the
credential vault is per installation and that no credential is shared between
devices.

## Promoting a build

The internal track is where a build is proven before it goes wider. To promote:

1. **Testing → Internal testing → Releases**, pick the release.
2. **Promote release**, then choose the target track (closed, open, or
   production).
3. Play carries the same bundle forward; the `versionCode` does not change, so
   do not re-tag for a promotion. A promotion is a Play Console action, not a
   workflow run.

A promotion to production needs the store listing, the content rating and the
data safety form filled in. None of that is in this repository.

## Rotating the upload key

Play App Signing holds the release key, so the upload key can be replaced
without a new build reaching riders: Play re-signs what it delivers.

1. **Generate a new key.** Alias `upload`, RSA-4096, long validity:

   ```sh
   keytool -genkeypair -v -keystore wattracker-upload.jks -alias upload \
     -keyalg RSA -keysize 4096 -validity 10000
   ```

   `*.jks` and `*.keystore` are in the root `.gitignore`. Keep the file out of
   the tree regardless, and never put a password in a command line that lands
   in a shell history.
2. **Record the certificate SHA-256.** `keytool -printcert -jarfile
   app-release.aab` prints it. It is public and is the value to quote when
   confirming which key signed a build; the release workflow prints it to the
   log for that reason.
3. **Set the four keystore secrets at environment scope:**

   ```sh
   gh secret set ANDROID_UPLOAD_KEYSTORE_B64 --env android-code-signing < upload.jks.b64
   gh secret set ANDROID_UPLOAD_KEYSTORE_PASSWORD --env android-code-signing
   gh secret set ANDROID_UPLOAD_KEY_PASSWORD --env android-code-signing
   gh secret set ANDROID_UPLOAD_KEY_ALIAS --env android-code-signing
   ```

   Secret values are write-only and never readable back through the API, so
   this means re-supplying them by hand.
4. **Request an upload-key reset in Play Console** (App integrity → Play App
   Signing → Request upload key reset). Google validates the request against
   the current key; the old key keeps working until the reset is approved.
5. **Cut the next release from a tag.** The workflow proves the new key signed
   the bundle before uploading, so a wrong keystore fails there rather than at
   Play.

The original keystore for this app could not be found on 2026-11-25, so the
secrets were re-supplied from a new `upload` key. Nothing had been uploaded to
Play, so no upload-key reset was needed. The repository-level `ANDROID_UPLOAD_*`
values from 2026-11-01 are stale and must not be used.

## What this cannot show

- **The bundle's signature is proven, its identity is not.** The workflow
  checks that a signer is present and prints the certificate SHA-256; it does
  not compare that value against the expected one, because doing so needs the
  expected value in the tree. A human confirms the SHA-256 against the value
  recorded above.
- **No emulator or device runs in CI.** A green run means the bundle was built,
  signed and uploaded — not that the app works. The release-shape device test
  in `plan.md` Step 7 item 5 is a separate, human-run verification.
- **The service account's Play permissions are not checked by the workflow.**
  A service account that lacks `Release apps to testing tracks` fails at the
  upload step, not earlier.
