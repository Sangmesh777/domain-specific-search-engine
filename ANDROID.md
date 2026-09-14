# Android Port Specification

> **STATUS: SPECIFICATION AND UNCOMPILED SOURCE ONLY.**
>
> No Android toolchain exists in the environment where this document was
> written: there is no JDK, no Android SDK, no Gradle, and the network
> blocks `dl.google.com`, `maven.google.com`, `services.gradle.org` and
> `repo.maven.apache.org`. Nothing under `android/` has been compiled,
> and no APK has been produced. See [Toolchain status](#toolchain-status).
>
> Everything in this document that describes *Python* behaviour was
> verified by executing the real engine. Everything that describes
> *Kotlin* behaviour is a specification that has not been executed.

---

## 1. Target architecture

```text
                        SEARCH ENGINE
                             │
              ┌──────────────┴──────────────┐
              │                             │
         ONLINE MODE                  OFFLINE MODE
              │                             │
         Flask/API                    Android APK
              │                             │
       RemoteBackend                   LocalBackend
              │                             │
        Server SQLite               Android SQLite
        Server files                Local app files
        Server index                Local index
```

One search contract, one set of semantics, two execution adapters.

Dependency direction is strictly one-way:

```text
UI  →  SearchBackend  →  LocalBackend / RemoteBackend  →  engine / persistence / transport
```

The UI must never reference SQLite, HTTP, the ranking algorithm or the
filesystem directly.

---

## 2. `SearchBackend` interface

Both backends implement this. Use project types, not new concepts.

```kotlin
interface SearchBackend {
    suspend fun search(query: String, page: Int, limit: Int): SearchResponse
    suspend fun status(): CorpusStatus
    suspend fun indexDocument(name: String, source: DocumentSource): IndexOutcome
    suspend fun delete(name: String): DeleteOutcome
    suspend fun bulkDelete(names: List<String>): BulkDeleteOutcome
    suspend fun rebuild(): RebuildOutcome
    suspend fun getDocument(name: String): DocumentPayload?
    suspend fun hasDocument(name: String): Boolean
}
```

`bulkDelete` takes the whole list on purpose. Bulk delete must be one
operation and one transaction; a UI loop of `delete()` calls is a bug.

---

## 3. Toolchain status

Checked in the authoring environment:

| Requirement | Result |
| --- | --- |
| `java -version` | **not found** |
| `javac -version` | **not found** |
| `gradle --version` | **not found** |
| `adb version` | **not found** |
| `ANDROID_HOME` / `ANDROID_SDK_ROOT` | **unset** |
| Android SDK / platform tools / build tools | **absent** |
| Emulator or connected device | **absent** |
| `apt-get install openjdk-17-jdk-headless` | **no candidate** (no package lists) |
| `dl.google.com` | **blocked** |
| `maven.google.com` | **blocked** |
| `repo.maven.apache.org` | **blocked** |
| `services.gradle.org` | **blocked** |
| `release-assets.githubusercontent.com` | **blocked** |

Reachable hosts: `pypi.org`, `github.com` (API/HTTPS only, not release
assets), `registry.npmjs.org`.

Because the Android SDK and the Android Gradle Plugin cannot be
downloaded, **an APK cannot be built here**. This is the single blocker
for the entire Android deliverable.

### To continue in an Android-capable environment

1. Install JDK 17, Android SDK (platform 34+, build-tools 34+), and
   Gradle 8.x, or open `android/` in Android Studio.
2. Set `ANDROID_HOME` (or `sdk.dir` in `android/local.properties`).
3. Run `android/tools/run_parity.sh`, which executes nothing but the
   JVM-side parity harness first. Do not build the APK until the
   harness reports zero mismatches.
4. Then build, install, and run the device matrix in section 12.

---

## 4. Golden-vector contract

`tests/golden/search_engine_vectors.json` is the parity contract. It
contains three independent sections:

| Section | Count | Purpose |
| --- | --- | --- |
| `vectors` | 74 | Complete search responses over the 13-document corpus |
| `empty_corpus_vectors` | 5 | Responses before any document is imported |
| `contract_vectors` | 94 cases | Tokenizer, sanitizer, rounding, query parsing |

The harness must compare the **entire** response. Comparing only
`title` and `score` will not expose the bugs this file exists to catch.

Comparison rules:

* response **shape** must match: bare JSON list vs paginated object
* every object's **key set** must match exactly
* every value must be identical
* floats must be **bit-identical binary64**, not approximately equal

### 4.1 Normalization

Exactly one field is removed before comparison:

* `path` — the absolute server filesystem path. It is environment
  specific, is not part of the contract, and must never be exposed to
  a client as a document identity.

Everything else is compared verbatim: `title`, `snippet`, `page`,
`highlights`, `phrase_occurrences`, `filename_score`, `content_score`,
`exact_content_match`, `prefix_similarity`, `numeric_similarity`,
`lexical_match_relevance`, `phrase_score`, `score`, `match_type`,
`filetype_filter`, `tag`, `document_url`, `open_url`, `page_url`.

The Python verifier implements this in
`tools/generate_golden_vectors.normalize_response`, and the harness must
apply the identical normalization to live responses.

### 4.2 Response shapes

Three published shapes exist. All must be preserved.

**Paginated object.** The normal case:

```json
{
  "results": [ ... ],
  "pagination": {
    "page": 1, "limit": 10, "total": 13, "total_pages": 2,
    "has_next": true, "has_previous": false, "start": 1, "end": 10
  }
}
```

**Bare list.** Reachable on two paths, and only these two:

* a quoted phrase that matches no document
  (`"zzzznope"` → `[]`)
* any normal keyword or prefix query against a corpus with **zero**
  documents (`network` → `[]`)

A filetype filter that eliminates every candidate does **not** produce
a bare list; it returns a paginated object with `total: 0`
(`beacon pdf`).

**Blank-query object.** A hardcoded shape that ignores the requested
page and limit:

```json
{ "results": [], "pagination": {
    "page": 1, "limit": 10, "total": 0, "total_pages": 0,
    "has_next": false, "has_previous": false } }
```

Note there is **no `start` and no `end`** key here, unlike the normal
paginated object. Querying `?q=&page=3&limit=25` still returns
`page: 1, limit: 10`.

### 4.3 Pagination clamping

Verified behaviour, all pinned by vectors:

| Input | Result |
| --- | --- |
| `page=1` | page 1, `start` 1, `end` 10 |
| `page=2` | page 2, `start` 11, `end` 13 |
| `page=3` | clamped to page 2 (last page) |
| `page=99` | clamped to last page |
| `page=0`, `page=-4` | page 1 |
| `page=abc` | page 1 |
| `limit=999` | clamped to 50 |
| `limit=abc` | 10 |
| empty results | `total_pages: 0`, `start: 0`, `end: 0` |

---

## 5. Tokenizer parity

The Python implementation is:

```python
def tokenize(text):
    text = text.lower()
    cleaned_text = "".join(
        character
        if character.isalnum() or character.isspace()
        else " "
        for character in text
    )
    return [word for word in cleaned_text.split() if len(word) > 1]

def tokenize_filename(text):
    text = text.lower()
    cleaned_text = "".join(
        character
        if character.isalnum() or character.isspace()
        else " "
        for character in text
    )
    return [word for word in cleaned_text.split() if word]
```

The only difference is the content tokenizer drops tokens shorter than
two characters.

### 5.1 Where the obvious Java port is wrong

Three divergences matter. All three are covered by contract vectors.

**1. Lowercasing.** `text.lower()` on the whole string is
context-sensitive and can change length. `"İstanbul".lower()` is
`"i" + U+0307 + "stanbul"` — three characters for two. `Character.toLowerCase(c)`
returns a single `char` and cannot express this. Use
`String.lowercase(Locale.ROOT)` on the whole string, which also applies
Java's built-in final-sigma rule and matches Python
(`"ΣΣ".lower() == "σς"`).

**2. `isalnum()`.** Python returns true for category **No** and **Nl**
numerics. Java's `Character.isLetterOrDigit` returns false for both.

| Character | Python `isalnum()` | Java `isLetterOrDigit()` |
| --- | --- | --- |
| `½` U+00BD | **true** | false |
| `²` U+00B2 | **true** | false |
| `Ⅻ` U+216B | **true** | false |
| `५` U+096B | true | true |
| `字` U+5B57 | true | true |
| U+0307 U+0307 | false | false |

Correct predicate:

```kotlin
fun isPythonAlnum(c: Int): Boolean =
    Character.isLetter(c) ||
    Character.isDigit(c) ||
    Character.getType(c) == Character.LETTER_NUMBER.toInt() ||
    Character.getType(c) == Character.OTHER_NUMBER.toInt()
```

**3. `isspace()`.** Java splits whitespace into two predicates and
Python's set is different from both.

| Character | Python `isspace()` | Java `isWhitespace()` | Java `isSpaceChar()` |
| --- | --- | --- | --- |
| U+00A0 NO-BREAK SPACE | **true** | false | true |
| U+2007 FIGURE SPACE | **true** | false | true |
| U+202F NARROW NO-BREAK SPACE | **true** | false | true |
| U+0085 NEXT LINE | **true** | false | false |
| U+001C–U+001F | **true** | true | false |
| U+200B ZERO WIDTH SPACE | false | false | false |

Correct predicate — note U+0085, which **neither** Java predicate
reports:

```kotlin
fun isPythonSpace(c: Int): Boolean =
    Character.isWhitespace(c) ||
    Character.isSpaceChar(c) ||
    c == 0x0085
```

### 5.2 Worked example: the Turkish dotted I

This case is pinned by `test_sidecar_reproduces_the_known_turkish_quirk`
and by the `unicode_*` vectors. It is the single best parity canary.

```text
input            "İstanbul"
lower()          "i" U+0307 "stanbul"      (length 7 -> 8)
U+0307 alnum?    no  -> replaced by space
cleaned          "i stanbul"
split            ["i", "stanbul"]
content tokenize ["stanbul"]               (len<2 drops "i")
filename tokenize["i", "stanbul"]
```

Consequences, both verified against the live engine:

* query `İstanbul` matches `unicode_notes.txt` (via `stanbul`)
* query `istanbul` matches **nothing** — `istanbul` is not in the index

A port using `Character.toLowerCase` per character would index
`istanbul` and produce the opposite result for both queries.

### 5.3 Filename tokenization

The filename index is built from the **stem**, not the full name:

```python
filename_words = tokenize_filename(os.path.splitext(filename)[0])
```

So `BCS502_Module_2.pdf` → `["bcs502", "module", "2"]`. The extension is
**not** indexed, and token order is meaningful — `" ".join(filename_words)`
is used for phrase matching inside filenames, and the phrase bonus is
`+= 100.0`. Storage must preserve order.

---

## 6. Filename sanitizer parity

Document identity is decided by the sanitizer, so it is a high-risk
port. Python delegates to Werkzeug's `secure_filename` after
`os.path.basename`, then rejects unsupported extensions.

Verified behaviour (`sanitize_upload_filename`):

| Input | Output |
| --- | --- |
| `a.txt` | `a.txt` |
| `My File.pdf` | `My_File.pdf` |
| `file.TXT` | `file.TXT` |
| `../../etc/passwd.txt` | `passwd.txt` |
| `..\..\x.txt` | `x.txt` |
| `a/b/c.txt` | `c.txt` |
| `/abs/path.txt` | `path.txt` |
| `.hidden.txt` | `hidden.txt` |
| `...hidden.txt` | `hidden.txt` |
| `My  File...txt` | `My_File...txt` |
| `  spaced  .txt` | `spaced_.txt` |
| `café.txt` | `cafe.txt` |
| `Ünïcödé Ñämé.docx` | `Unicode_Name.docx` |
| `no-ext` | `""` |
| `archive.zip` | `""` |
| `weird.pdf.txt` | `weird.pdf.txt` |
| `CON.txt` | `CON.txt` |
| `nul.pdf` | `nul.pdf` |
| `""` | `""` |

Rules, in order:

1. Take `basename` (strip every directory component, POSIX and Windows).
2. NFKD-normalise, then drop characters outside ASCII. `café` → `cafe`;
   `Ünïcödé Ñämé` → `Unicode_Name`.
3. Replace every run of non-alphanumeric, non-dot, non-underscore
   characters with a single `_`.
4. Strip leading dots and underscores. This is why `.hidden.txt` becomes
   `hidden.txt`. Note it also destroys path traversal.
5. Reject the result if it does not end in `.pdf`, `.docx` or `.txt`
   (case-insensitive check, original case preserved).

Two behaviours worth stating explicitly because they are platform
dependent:

* Reserved Windows device names are **not** rewritten on Linux.
  `CON.txt` and `nul.pdf` pass through unchanged. If the Android port
  adds device-name filtering it will diverge from the server.
* Multiple separators collapse but dots do not: `My  File...txt` keeps
  its three dots.

The sanitizer must never return a value containing `/`, `\`, or `..`.

---

## 7. Float and rounding parity

The engine rounds scores with `round(x, 4)`. Python rounds the *exact*
binary64 value to the given number of decimal places with ties to
**even**.

The port must reproduce this with the exact binary value:

```kotlin
import java.math.BigDecimal
import java.math.RoundingMode

fun pythonRound(value: Double, digits: Int): Double =
    BigDecimal(value).setScale(digits, RoundingMode.HALF_EVEN).toDouble()
```

`BigDecimal(value)` — the constructor taking a `Double` — is the exact
binary value. `BigDecimal.valueOf(value)` goes through a decimal string
and is wrong. `RoundingMode.HALF_UP` is wrong. Formatting to a decimal
string and parsing back is wrong.

Verified expectations, all in `contract_vectors.round_half_even`:

| Expression | Result |
| --- | --- |
| `round(2.00005, 4)` | `2.0` |
| `round(0.56785, 4)` | `0.5678` |
| `round(2.675, 2)` | `2.67` |
| `round(1.00005, 4)` | `1.0001` |
| `round(0.125, 2)` | `0.12` |
| `round(0.135, 2)` | `0.14` |
| `round(0.5, 0)` | `0.0` |
| `round(1.5, 0)` | `2.0` |
| `round(2.5, 0)` | `2.0` |
| `round(-0.5, 0)` | `0.0` |
| `round(-1.5, 0)` | `-2.0` |
| `round(2.6750000000000003, 2)` | `2.68` |

`round(2.675, 2)` returning `2.67` rather than `2.68` is the point: the
literal `2.675` is really `2.674999999999999822...`.

## 8. Float operation order

Do not refactor ranking expressions into mathematically equivalent
forms. Binary64 addition and multiplication are not associative, and
the vectors compare bit patterns. Preserve operand order, addition
order, multiplication order, division order, `log` order, clamping
order and rounding order exactly as written in `app.py`.

### 8.1 `math.log`

IDF and phrase-frequency scoring use natural logarithms. Start with
`kotlin.math.ln`, which delegates to `Math.log`. If a vector mismatches
in the fourth decimal place:

1. re-check the operation order first — that is the more likely cause
2. inspect the intermediate values
3. only then suspect the `log` implementation

Never adjust a formula constant to hide a discrepancy.

---

## 9. Engine port surface

Port exactly, do not redesign:

* query normalization (`normalize_search_query`)
* filetype parsing (`parse_filetype_filter`)
* filename scoring, content scoring
* IDF, prefix matching, numeric matching, lexical hierarchy
* phrase scoring, final score, result ordering
* snippet generation, highlight generation, pagination

The Python engine is the behaviour reference.

---

## 10. Persistence

A Room or SQLite schema must hold:

* `documents` — name, title, extension, total_words, page_count, path
* `term_postings` — term, document, frequency
* `filename_terms` — document, position, token (**position matters**)
* `pages` — document, page number, text

Requirements:

* filename token order is meaningful and must survive storage
* index reconstruction at load time must use a deterministic ordering
* the identifier exposed to the UI is the sanitized document name,
  never a filesystem path

---

## 11. Sidecar bootstrap

`artifacts/android/corpus_sidecar.json` is bundled as an asset. It
carries, per document: `name`, `title`, `extension`, `total_words`,
`page_count`, ordered `filename_terms`, `pages[]` with extracted text,
and `content_terms` (term → frequency).

Because it stores the **post-tokenisation** state, the device never
needs PyPDF2 or python-docx for the shipped corpus, and the harness can
prove the Kotlin tokenizer reproduces the Python index exactly rather
than assuming it.

Bootstrap:

```text
asset → parse → verify integrity hash → build SQLite rows
      → build in-memory snapshot → READY
```

Integrity: the `integrity.canonical_json_sha256` field is the SHA-256
of the document with the `integrity` block removed, serialised with
`sort_keys=True`, `ensure_ascii=False`, `separators=(",", ":")`.

Verified properties of the shipped sidecar:

* 13 documents
* 191 distinct content terms, matching the engine exactly
* every `filename_terms` equals `tokenize_filename(splitext(name)[0])`
* every content term is already lowercased
* all page text is present, so no PDF parsing is required on device

---

## 12. Index state model

Three states, held **in memory only**:

| State | Meaning |
| --- | --- |
| `READY` | search the current complete snapshot |
| `INDEXING` | a rebuild is running; the previous complete snapshot stays searchable |
| `ERROR` | the last rebuild failed; the previous complete snapshot stays searchable |

The status must **not** be persisted. Verified on the Python side: a
fresh process always starts `READY` with `generation: 1`, regardless of
what happened before. Persisting an `INDEXING` flag would let a process
death during a rebuild leave the application permanently wedged.

Rebuild must be: build a complete snapshot, then swap atomically.
Failure must leave the previous snapshot serving.

---

## 13. Document import

Use the Storage Access Framework with `ACTION_OPEN_DOCUMENT`:

```text
user picks file → content URI → copy to app-private storage
→ sanitize filename → extract text → index → persist → activate snapshot
```

Support PDF, DOCX and TXT where the chosen parser handles them. The
device-side extractor is **not** claimed to be byte-identical to the
server-side PyPDF2/python-docx pipeline; that equivalence is not
established by any test in this repository. Only search behaviour over
already-extracted text is under the parity contract.

---

## 14. Document viewer

```text
offline: search result → local document name → app-private file → viewer
online:  search result → server /api/documents/<name> → viewer
```

Never surface a raw absolute internal path as a document identity. The
server's own `/api/search` response does include an absolute `path`
field, which is one reason the golden vectors drop it.

---

## 15. Error model

One shared domain error set, mapped inside the backends. The UI must
not branch on HTTP 404, SQLite errors or IO exceptions.

```text
InvalidDocument   DocumentNotIndexed   DocumentNotFound
StorageError      IndexingFailed      RebuildFailed
SearchError       RemoteError
```

Verified server mappings to reproduce:

| Situation | Server response |
| --- | --- |
| `DELETE /api/documents/<unknown>` | HTTP 404 `{"error": "Document is not indexed."}` |
| bulk delete, unknown names | HTTP 200, name listed in `not_found`, `not_found_count` |
| bulk delete, no names found | HTTP 200, `deleted_count: 0`, index untouched |

---

## 16. Mode selection

```text
OFFLINE | ONLINE
```

Persisted in settings, always visible in the UI.

On an online failure: surface `RemoteError` and let the user choose to
switch to offline. Do **not** silently fall back — the local and remote
corpora can differ, and a silent fallback would return results from a
different document set than the user asked for.

---

## 17. Threading

Rebuild and indexing run off the main thread via coroutines on
`Dispatchers.IO`, or `WorkManager` for work that must outlive the
activity. Threading must stay outside the ranking core; the core must
remain a pure function of the snapshot.

---

## 18. Unverified and outstanding

Not done, and not claimed:

* nothing under `android/` has been compiled
* no APK, debug or release
* no `adb` install, no emulator run, no physical device run
* no on-device performance numbers (100 / 500 / 1000 documents)
* no on-device crash-during-rebuild test
* no cross-mode vector run (Python vs Local vs Remote)
* no device-side PDF/DOCX extraction parity

Blocked entirely by the missing toolchain described in section 3.
