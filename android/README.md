# Android Application

> ## UNCOMPILED — NO APK EXISTS
>
> Nothing in this directory has been compiled. It has never been run
> through a Kotlin compiler or Gradle. The authoring environment had no
> JDK, no Android SDK and no Gradle, and the network blocked every host
> that could supply them (`dl.google.com`, `maven.google.com`,
> `repo.maven.apache.org`, `services.gradle.org`).
>
> Treat everything here as **source under review**, not as working
> software. See [../ANDROID.md](../ANDROID.md) for the full toolchain
> report and the port specification.

## What is here

```text
android/
  settings.gradle.kts          Gradle build entry point
  build.gradle.kts             Pinned plugin versions
  gradle.properties
  harness/                     Pure JVM parity gate - NO Android dependencies
    src/main/kotlin/com/vtu/search/parity/
      PythonText.kt            Exact Unicode tokenizer port
      PythonRound.kt           Exact HALF_EVEN binary64 rounding port
      SecureFilename.kt        Exact filename sanitizer port
      GoldenVectors.kt         Vector loader and exact comparator
      ContractHarness.kt       Runs the contract vectors
    src/main/kotlin/com/vtu/search/backend/
      SearchBackend.kt         The one search contract, both adapters
      SearchError.kt           Shared domain error model
    src/test/kotlin/           JUnit tests for the ports
  tools/run_parity.sh          Parity gate; run this before any APK build
```

## Why the harness is a separate, Android-free module

Parity bugs are the most expensive kind in this project: a tokenizer
that disagrees with Python produces wrong search results that look
plausible. Those bugs must be catchable before the Android SDK is
involved.

So `harness` is a plain Kotlin/JVM module. It needs **only a JDK** to
build and test. The moment a JDK is available, you can verify the
tokenizer, sanitizer and rounding ports against the real Python
vectors, without installing the Android SDK, an emulator or a device.

The Android `app` module is intentionally **not** included yet: it was
not written, because writing a large body of Android UI code that
cannot be compiled even once would produce unverifiable source rather
than progress.

## Build order

1. **`android/tools/run_parity.sh`** — must print `PARITY OK`.
   Only needs a JDK.
2. Use the sidecar-backed `RankingEngine` replay first, then implement
   `LocalBackend` and pass it as an oracle to `ContractHarness.run` if
   you want adapter-level parity on top of the algorithm replay.
3. Add the `app` module (Compose UI), compile, install, and run the
   device matrix.

Do not skip step 1. A working APK that returns different results than
the server is worse than no APK.

## What the harness currently verifies

| Check | Status |
| --- | --- |
| 40 content-tokenizer cases | implemented, unverified (never executed) |
| 10 filename-tokenizer cases | implemented, unverified |
| 33 sanitizer cases | implemented, unverified |
| 17 rounding cases | implemented, unverified |
| 79 search vectors | implemented, unverified — sidecar-backed replay exists, never executed here |

## Not written

`LocalBackend`, `RemoteBackend`, SQLite persistence, the sidecar
bootstrap, Storage Access Framework import, the document viewer, and
the entire UI. `SearchBackend.kt` defines the contract they must
satisfy, and `../ANDROID.md` specifies the rest.
