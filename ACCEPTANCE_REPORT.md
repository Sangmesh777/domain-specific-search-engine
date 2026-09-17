# Acceptance report

Everything below was produced by running the command named next to it
in this checkout. Nothing here is estimated, and "implemented" is kept
distinct from "compiled" and "tested".

```text
CURRENT COMMIT  c9a6930  (backend tip; this report is added on top)
                PR #1 head, base main @ 81faff9, 19 commits, 76 files
BACKEND         PASS      app.py 1448 lines; search_engine has 9 modules
TESTS           444 passed, 1 warning          ./run_tests.sh
                435 passed, 9 skipped          no server listening
GOLDEN          79 of 79 vectors passed        tools.verify_golden_vectors
                13 documents, 191 content terms
SHADOW          12 functions, 21,454 comparisons, 0 differences
STORAGE         10 fixtures, 324,500 values compared, 0 differences
                18 mutation comparisons, 0 differences
                8 of 8 storage negative controls detected
                10 of 10 sanitizer controls resolved, 0 undetected
ANDROID TOOLCHAIN  ABSENT   no javac, no kotlinc, no gradle, no
                            Android SDK, no adb, no device
ANDROID         NOT BUILT  Kotlin sources exist and are unreviewed by
                            any compiler; no APK exists
LOCALBACKEND    NOT BUILT  designed and specified, never executed
REMOTEBACKEND   NOT BUILT  designed and specified, never executed
CROSS-MODE PARITY  0 mismatches - but only model-to-engine, in Python.
                   The Kotlin side has never run against the vectors.
DEVICE          NONE
APK             NONE
GIT             branch arena/01a09e59-domain-specific-search-engine
                pushed, local == remote == c9a6930
BLOCKERS        Android toolchain unavailable and unreachable
NEXT            merge PR #1, create the Android repo, build there
```

## What this session changed

Two defects and two pieces of instrumentation were added on top of the
work already on the branch.

### Defect 1: two copies of the extraction rule

The rule that turns a file into index rows existed twice, in the
rebuild loop in `search_engine/indexing.py` and inline in
`incrementally_index_document` in `app.py`. Both now call
`indexing.extract_document(file_path, filename)`. The two callers keep
their different policies: the rebuild prints
`[REBUILD] Skipped empty document: <name>` and skips, the upload path
raises `ValueError("Document contains no readable text.")`.

`app.py` went from 1476 to 1448 lines. Re-inlining the extraction fails
two tests, which is how the deduplication is held in place.

### Defect 2: the engine core imported the web stack

`search_engine` is meant to be shared by the Flask API and an offline
Android backend, so it must not import HTTP. An AST scan found exactly
one violation: `sanitize.py` did
`from werkzeug.utils import secure_filename`. No `request`, `jsonify`
or `session` node exists anywhere in the package.

`secure_filename` is now transcribed into the package with the same
order of operations, the same regex and the same Windows device-name
check. The two host-dependent lines are unchanged on purpose, because
changing them would change stored document names.

The transcription is verified, not assumed:
`tests/test_sanitizer_independence.py` compares it to werkzeug's real
function over a 6,000-case corpus built to hit every branch, and asserts
the corpus is wide enough to mean something. The Windows branch cannot
fire on this host, so `tools/sanitizer_windows_check.py` drives it in a
bare interpreter: 6,205 calls, 27 device names actually prefixed,
0 divergences.

### The defect removing the import uncovered

Fixing that import exposed a genuine cross-mode bug.
`tools/port_model.py` and the Kotlin `SecureFilename.kt` both stripped
directory components by scanning for `/` **or** `\`, with a comment
claiming this mirrored `os.path.basename`. On the platform the engine
runs on, it does not. Only `/` separates, so
`os.path.basename("dir\sub\file.txt")` returns the whole string and
`secure_filename` then deletes the backslash, giving `dirsubfile.txt`.

The port would have stored `file.txt` where the server stores
`dirsubfile.txt`: a different document name, different filename tokens,
different search results for the same file. That is precisely the
cross-mode drift the whole project exists to prevent.

The recorded contract could not have caught it. It held exactly one
backslash input, `..\..\x.txt`, which sanitizes to `x.txt` either way.
Because the Android harness has no Python engine and can only compare
Kotlin against `tests/golden/search_engine_vectors.json`, ten
discriminating cases were added to `SANITIZE_INPUTS`.

Regeneration is deterministic, and the diff is exactly those ten
entries: all 79 corpus vectors and the engine index state are
byte-identical, which is the evidence that no engine behaviour changed.

### Instrumentation: the controls are now part of the gate

`run_tests.sh` ran seven gates and none of them was the
negative-control suite. When the extraction rule moved, two storage
controls silently stopped matching anything, because their anchors still
referred to code that had moved. The gate reported `ALL GATES PASSED`
the whole time it was degraded.

The anchors are repointed and the controls now run as gates of their
own. A broken control anchor makes the runner exit non-zero, which was
verified by breaking one deliberately. Both runners report "not
applied" distinctly from "not detected", and each patch asserts its
anchor matched and that the file digest changed.

`test_search_runs_without_a_flask_app` asserted
`"flask" not in sys.modules or True`, which is always true. It is
replaced by `tests/test_core_is_transport_free.py`, which blocks flask,
werkzeug, requests, http and urllib.request in a subprocess, then
imports every core module and runs a search.

## Verification performed

| Claim | Method | Result |
| --- | --- | --- |
| Handlers are adapters | `tools/handler_audit` | no SQL; engine reached only via app-level helpers |
| Core has no web stack | AST scan of `search_engine` | `urllib.parse` only, which is percent-encoding |
| Core imports with no web stack | subprocess with imports blocked | 8 of 8 modules, plus a search |
| Sanitizer matches werkzeug | 6,000-case differential | 0 divergences |
| Windows device branch | bare interpreter, `os.name` patched | 27 names prefixed, 0 divergences |
| Port model matches engine | differential over 39 names | 0 divergences |
| Recorded contract discriminates | vector inspection | 10 discriminating cases |
| Snapshot is copy-on-write | contract tests | aliasing the containers fails 5 tests |
| Readers never see a partial write | 3 readers, 2,000 mutations | 100 reads, both generations, 0 torn |
| Filename tokens survive restart | rebuild under seed 0, load under seed 999 | identical, order preserved |
| Rebuild order | source-pinned test | build, save, publish, sync - left alone |
| Gate can fail | broken control anchor | exit 1 |

## What was deliberately not done

* The four handlers were audited and then left alone. They parse
  requests, orchestrate, and serve files; that is transport work.
* The rebuild order `build -> save -> publish -> sync` was not
  "corrected". A note once assumed sync-then-publish, but no test
  proves the shipped order wrong, so it stands.
* `urllib.parse.quote` was left in `engine.py`. It is string
  percent-encoding with no transport behaviour, carried over from the
  monolith. Reimplementing it by hand would add risk and change
  nothing; `urllib.request` and `urllib.error` are blocked instead, so
  the network is still unreachable from the core.
* No cosmetic extraction. The goal is separation of responsibility,
  not a smaller line count.

## The Android blocker

Nothing in the Android half of this project has been compiled or run.
The Kotlin sources under `android/harness/` were written earlier and
modelled in Python, but no Kotlin compiler has ever seen them.

Measured in this environment:

```text
java, javac, kotlinc, gradle, adb, sdkmanager   not found
ANDROID_HOME, ANDROID_SDK_ROOT                  unset
jdk4py                                          Temurin JRE 25.0.2, no javac

dl.google.com                    000
maven.google.com                 000
repo.maven.apache.org            000
services.gradle.org              000
api.adoptium.net                 000
release-assets.githubusercontent.com  000
pypi.org                         200
github.com                       200
```

The JVM and Android toolchain hosts are unreachable, so no compiler can
be fetched. `jdk4py` ships a runtime only, which is enough for
`tools/verify_jvm_semantics.py` (9,392 version-skew cases, 0
divergences) and not enough to build anything.

Per the standing instruction, Android implementation is therefore **not
claimed complete**, no APK is claimed to exist, and the blocker is
reported rather than worked around. It needs an environment with the
Android SDK on a network that can reach `dl.google.com`.

One thing the Python side can still pin, and now does: the port model's
agreement with the engine, and the discriminating vectors the Kotlin
harness will be checked against once it can run.
