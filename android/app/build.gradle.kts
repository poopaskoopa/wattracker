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
        // The cloud base URL is a build field, never a literal in code (the
        // iOS Config/*.xcconfig precedent). Split into scheme + host because
        // a full "scheme://host" cannot be a build-config value cleanly.
        debug {
            // The cloud dev harness (scripts/walking_skeleton_server.py) on the
            // host machine; 10.0.2.2 is the emulator's alias for the host's
            // loopback. Cleartext for it is permitted by the debug
            // network_security_config only.
            buildConfigField("String", "WATTRACKER_CLOUD_SCHEME", "\"http\"")
            buildConfigField("String", "WATTRACKER_CLOUD_HOST", "\"10.0.2.2:8765\"")
        }
        release {
            optimization {
                enable = false
            }
            // Placeholder host until the deployment exists (#102); the field,
            // not code, is where the production host lives.
            buildConfigField("String", "WATTRACKER_CLOUD_SCHEME", "\"https\"")
            buildConfigField("String", "WATTRACKER_CLOUD_HOST", "\"cloud.wattracker.example\"")
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

dependencies {
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.material.icons.extended)
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.navigation.compose)
    testImplementation(libs.junit)
    androidTestImplementation(platform(libs.androidx.compose.bom))
    androidTestImplementation(libs.androidx.compose.ui.test.junit4)
    androidTestImplementation(libs.androidx.espresso.core)
    androidTestImplementation(libs.androidx.junit)
    debugImplementation(libs.androidx.compose.ui.test.manifest)
    debugImplementation(libs.androidx.compose.ui.tooling)
}
