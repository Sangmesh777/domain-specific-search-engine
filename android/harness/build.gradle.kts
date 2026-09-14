// Pure JVM parity harness.
//
// Deliberately has NO Android dependencies. Everything in this module
// must be verifiable with only a JDK, because parity bugs are the most
// expensive kind and they should be caught before the Android SDK is
// involved.
//
// Run with:
//     ./gradlew :harness:test
//     ./gradlew :harness:run

plugins {
    kotlin("jvm")
    application
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    // Only the runtime is needed: this module manipulates JsonElement
    // directly and does not use @Serializable classes.
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")

    testImplementation(kotlin("test"))
    testImplementation("org.junit.jupiter:junit-jupiter:5.11.3")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

application {
    mainClass.set("com.vtu.search.parity.ContractHarnessKt")
}

tasks.test {
    useJUnitPlatform()

    // The golden artifacts are owned by the Python tools at the
    // repository root, so point the harness at them rather than copying
    // them into the module.
    val vectors = rootProject.extra["goldenVectorsPath"] as File
    val sidecar = rootProject.extra["corpusSidecarPath"] as File

    systemProperty("golden.vectors.path", vectors.absolutePath)
    systemProperty("corpus.sidecar.path", sidecar.absolutePath)

    testLogging {
        events("passed", "failed", "skipped")
        showStandardStreams = true
    }
}

tasks.named<JavaExec>("run") {
    val vectors = rootProject.extra["goldenVectorsPath"] as File
    val sidecar = rootProject.extra["corpusSidecarPath"] as File

    systemProperty("golden.vectors.path", vectors.absolutePath)
    systemProperty("corpus.sidecar.path", sidecar.absolutePath)
}
