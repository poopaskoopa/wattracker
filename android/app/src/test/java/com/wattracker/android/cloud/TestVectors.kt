package com.wattracker.android.cloud

import com.wattracker.android.json.JsonValue
import java.io.File

/**
 * Locate and parse the shared interop vector files under the repository's
 * `tests/vectors/` directory.
 *
 * The files are referenced from the repository, not copied into the Android
 * project, so the Kotlin suite cannot drift from the Python and Swift suites
 * that read the same files. The working directory when Gradle runs a unit test
 * is not guaranteed, so the file is found by walking up from `user.dir` until
 * the `tests/vectors` layout appears -- which works whether the CWD is
 * `android/`, `android/app/`, or the repo root.
 */
object TestVectors {

    fun parse(name: String): JsonValue = locate(name).readText(Charsets.UTF_8).let { JsonValue.parse(it) }

    private fun locate(name: String): File {
        var dir: File? = File(System.getProperty("user.dir") ?: ".").absoluteFile
        while (dir != null) {
            val candidate = File(File(File(dir, "tests"), "vectors"), name)
            if (candidate.isFile) return candidate
            dir = dir.parentFile
        }
        throw AssertionError(
            "Could not locate tests/vectors/$name by walking up from ${System.getProperty("user.dir")}",
        )
    }
}
