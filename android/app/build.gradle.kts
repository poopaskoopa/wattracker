plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "com.wattracker.android"
    compileSdk {
        version = release(37)
    }

    defaultConfig {
        applicationId = "com.wattracker.android"
        minSdk = 30
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
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
            // Placeholder host until the deployment exists (#102); the field,
            // not code, is where the production host lives.
            buildConfigField("String", "WATTRACKER_CLOUD_SCHEME", "\"https\"")
            buildConfigField("String", "WATTRACKER_CLOUD_AUTHORITY", "\"cloud.wattracker.example\"")
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

// Guard: refuse release builds with the placeholder host until the #102
// hosting decision lands.
val releaseAuthority = "cloud.wattracker.example"
if (releaseAuthority.endsWith(".example") && !project.hasProperty("allowPlaceholderHost")) {
    afterEvaluate {
        tasks.named("assembleRelease") {
            doFirst {
                throw GradleException(
                    "Release build points at the #102 placeholder host. " +
                    "Pass -PallowPlaceholderHost to override."
                )
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
    testImplementation(libs.junit)
}
