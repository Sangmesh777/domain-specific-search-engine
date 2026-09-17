// Root build file.
//
// Pin plugin versions here so every module resolves the same toolchain.
// These versions are NOT verified: see ../ANDROID.md.

plugins {
    kotlin("jvm") version "2.0.21" apply false
    kotlin("android") version "2.0.21" apply false
    id("com.android.application") version "8.7.3" apply false
}

// Repository layout: the golden vectors and the corpus sidecar live at
// the repository root, because the Python tools own them. Resolve them
// once here so both modules agree.
val repositoryRoot: File = rootProject.projectDir.parentFile

extra["goldenVectorsPath"] = File(repositoryRoot, "tests/golden/search_engine_vectors.json")
extra["corpusSidecarPath"] = File(repositoryRoot, "artifacts/android/corpus_sidecar.json")
