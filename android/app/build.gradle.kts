import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    // KSP generates the Room DAO bindings (Step 2 cache). No other annotation
    // processor is in the project, so this is the whole codegen surface.
    alias(libs.plugins.ksp)
    // Exports the Room schemas to app/schemas (exportSchema = true writes
    // nothing without a directory) so #266 can build migrations against them.
    alias(libs.plugins.androidx.room)
}

room {
    schemaDirectory("$projectDir/schemas")
}

val localProperties = Properties()
val localPropsFile = rootProject.file("local.properties")
if (localPropsFile.exists()) {
    localPropsFile.inputStream().use { stream ->
        localProperties.load(stream)
    }
}
val releaseCloudAuthority: String = providers.gradleProperty("wattrackerCloudAuthority")
    .orNull
    ?: localProperties.getProperty("wattrackerCloudAuthority")
    ?: "cloud.wattracker.example"

// Release signing material (#486). No value is in the tree: the keystore path,
// its two passwords and the key alias arrive as environment variables from the
// release workflow, which decodes the base64 secret to a file on the runner
// first. All four or none -- a partial set would build a half-configured
// signer, so it counts as absent and the release stays unsigned.
val uploadKeystoreFile: String? = providers.environmentVariable("ANDROID_UPLOAD_KEYSTORE_FILE").orNull
val uploadKeystorePassword: String? = providers.environmentVariable("ANDROID_UPLOAD_KEYSTORE_PASSWORD").orNull
val uploadKeyPassword: String? = providers.environmentVariable("ANDROID_UPLOAD_KEY_PASSWORD").orNull
val uploadKeyAlias: String? = providers.environmentVariable("ANDROID_UPLOAD_KEY_ALIAS").orNull
val hasUploadKey: Boolean = listOf(
    uploadKeystoreFile, uploadKeystorePassword, uploadKeyPassword, uploadKeyAlias,
).all { !it.isNullOrBlank() }

// versionCode/versionName come from the tag, not a constant: Play refuses a
// re-upload at a versionCode it already has, and the update-in-place check in
// Step 7 needs a higher one. The release workflow passes the tag, so a human
// never has to remember. Absent (a local build), the static pair stands.
val releaseTag: String = providers.gradleProperty("wattrackerReleaseTag").orNull.orEmpty()
val releaseTagMatch: MatchResult? = Regex("""^android/v(\d+)\.(\d+)\.(\d+)$""").matchEntire(releaseTag)
val releaseVersionName: String = releaseTagMatch?.let { match ->
    "${match.groupValues[1]}.${match.groupValues[2]}.${match.groupValues[3]}"
} ?: "1.0"
val releaseVersionCode: Int = releaseTagMatch?.let { match ->
    // Monotonic across tags and far under Play's 2100000000 ceiling: 1.2.3 -> 10203.
    (match.groupValues[1].toInt() * 10_000) +
        (match.groupValues[2].toInt() * 100) +
        match.groupValues[3].toInt()
} ?: 1

android {
    namespace = "com.wattracker.android"
    compileSdk {
        version = release(37)
    }

    defaultConfig {
        applicationId = "com.wattracker.android"
        minSdk = 30
        targetSdk = 36
        // From the android/v* tag in the release workflow, so Play never sees a
        // repeated versionCode; the static pair stands for a local build.
        versionCode = releaseVersionCode
        versionName = releaseVersionName

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        // Only when the four environment variables are present (#486). The
        // file itself is written by the workflow from ANDROID_UPLOAD_KEYSTORE_B64
        // and lives on the runner, never in the tree: .gitignore covers
        // *.jks / *.keystore, and no key material is committed.
        if (hasUploadKey) {
            create("release") {
                storeFile = file(uploadKeystoreFile!!)
                storePassword = uploadKeystorePassword
                keyAlias = uploadKeyAlias
                keyPassword = uploadKeyPassword
            }
        }
    }

    buildTypes {
        // The cloud endpoint is a build field, never a literal in code (the
        // iOS Config/*.xcconfig precedent). The authority holds host:port.
        debug {
            // The cloud dev harness (scripts/walking_skeleton_server.py) on the
            // host machine; 10.0.2.2 is the emulator's alias for the host's
            // loopback. Cleartext for it is permitted by the debug
            // network_security_config only.
            buildConfigField("String", "WATTRACKER_CLOUD_SCHEME", "\"http\"")
            buildConfigField("String", "WATTRACKER_CLOUD_AUTHORITY", "\"10.0.2.2:8765\"")
        }
        release {
            // R8 minify/shrink is on: with material-icons-extended removed the
            // app is small enough that keep-rule fallout is trivial to track,
            // and enabling it now (before Step 2 adds Room + reflection) means
            // we debug R8 against a small surface, not a large one.
            optimization {
                enable = true
            }
            // The production host. It arrives from the android-code-signing
            // environment secret through -PwattrackerCloudAuthority (#468); the
            // default is the placeholder and the guard below refuses it.
            buildConfigField("String", "WATTRACKER_CLOUD_SCHEME", "\"https\"")
            buildConfigField("String", "WATTRACKER_CLOUD_AUTHORITY", "\"$releaseCloudAuthority\"")
            // Absent outside the release workflow, which is what keeps a local
            // release build unsigned rather than half-signed.
            if (hasUploadKey) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
}

// Guard: refuse release builds that still point at the placeholder host. #102
// is closed and the cloud is deployed (#468), so the real authority comes from
// the android-code-signing environment secret and this default is only ever a
// local build. -PallowPlaceholderHost exists for android.yml's unsigned PR
// smoke build; the release workflow never passes it, so a placeholder bundle
// fails instead of shipping. Reads the same val the release buildConfigField
// ships -- see the comment on it above.
if (releaseCloudAuthority.endsWith(".example") && !project.hasProperty("allowPlaceholderHost")) {
    afterEvaluate {
        // Both release entry points: the APK path and the bundle path (Step 7
        // ships a bundle, not an APK) -- a guard on one alone is a door left
        // ajar on the other.
        listOf("assembleRelease", "bundleRelease").forEach { name ->
            tasks.named(name) {
                doFirst {
                    throw GradleException(
                        "Release build points at the placeholder host. " +
                        "Pass -PwattrackerCloudAuthority=<host>, or " +
                        "-PallowPlaceholderHost for an unsigned smoke build.",
                    )
                }
            }
        }
    }
}

dependencies {
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.material.icons.core)
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.navigation.compose)

    // Step 2 (#194): the revision-keyed cloud cache. Room is the AndroidX
    // store. The credential store is not a dependency at all (#457): the cloud
    // binding and the desktop connector token are wrapped by an Android
    // Keystore AES-GCM key that cannot be exported, which is the property
    // `security-crypto` used to supply through Tink -- deprecated, and now gone
    // from the tree. No JSON, HTTP or DI library is introduced anywhere
    // (HttpURLConnection + the in-project JSON model are the rest of the
    // transport).
    implementation(libs.androidx.room.runtime)
    implementation(libs.androidx.room.ktx)
    implementation(libs.kotlinx.coroutines.core)
    ksp(libs.androidx.room.compiler)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
}
