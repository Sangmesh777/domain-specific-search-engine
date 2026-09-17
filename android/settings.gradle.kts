// Android build for the search engine.
//
// NOTE: this project has never been built. The authoring environment had
// no JDK, no Android SDK and no Gradle, and could not download any of
// them. See ../ANDROID.md for the full toolchain report.
//
// The `harness` module is a pure JVM module with no Android
// dependencies on purpose: it is the parity gate, and it should be
// runnable with only a JDK so that parity can be verified before the
// Android SDK is involved.

pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "domain-specific-search-engine"

include(":harness")
