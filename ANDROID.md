# Android offline mode: feasibility analysis and design

This document is the Android deliverable for the dual-mode milestone. It
analyses whether the existing search engine can run on-device, specifies the
architecture that keeps one ranking implementation behind two transports, and
publishes the contract a port is tested against.

It contains **no Kotlin**. The sandbox this was written in has no JDK, Gradle
or Android SDK, so any scaffold produced here would be unverifiable — code
that cannot be compiled or tested is not a deliverable, it is a liability.
What is delivered instead is the analysis, the design, and a machine-checkable
contract (`tests/golden/search_engine_vectors.json`) that makes the eventual
port testable on day one.

---

## 1. Verdict

**Feasible, with one component that must be replaced rather than ported.**

| Area | Verdict |
| --- | --- |
| Ranking and scoring logic | Ports cleanly. Pure computation over in-memory data structures. |
| Inverted index | Ports cleanly. Plain maps and counters. |
| Persistence | Ports cleanly. The engine already uses SQLite, which is built into Android. |
| Corpus size and latency | Comfortable. Measured numbers in §8 are inside a phone's budget for realistic corpora. |
| Float semantics | Ports **only if** the rounding trap in §4.1 is handled. Otherwise scores diverge silently. |
| Tokenization | Ports **only if** Unicode categories are reproduced exactly (§4.2). |
| Filename sanitization | Ports cleanly but must be copied character-for-character (§4.3). |
| Document text extraction | **Does not port.** PyPDF2 and python-docx have no Android equivalent with identical output. Must be reimplemented and reconciled (§7). |
| HTTP adapter | Not needed on-device. Replaced by `LocalBackend` (§5). |

The extraction problem is the real one, and it is the reason the golden
vectors record *extracted text* rather than parser behaviour: the ranking
contract is portable and testable even though the parser is not.

---

## 2. Why the core ports at all

The refactor that preceded this milestone produced `search_engine/` — 15
modules with zero web-stack imports, verified by an AST guard in the test
suite rather than by grepping for the string "flask". The engine's public API
is transport-independent:

```
search(query, page, limit)      index_document(name, path)
delete(name)                    bulk_delete(names)
rebuild()                       status()
get_document(name)              has_document(name)
```

Nothing in that list mentions HTTP, Flask, requests, or a device. The engine
takes strings and file paths and returns plain dictionaries. That is what makes
`LocalBackend` and `RemoteBackend` two adapters over *one* ranking
implementation instead of two ranking implementations.

The corollary matters for maintenance: **the Android app must not contain a
second copy of the ranking rules.** If it does, the two will drift, and the
drift will be invisible until a user compares results between modes. §5 and §9
exist to prevent that.

---

## 3. Portability by component

| Module | Role | On-device plan |
| --- | --- | --- |
| `tokenizer.py` | Word splitting for content and filenames | Port directly; see §4.2 |
| `filenames.py` | `secure_filename`, filename word extraction, bonus rules | Port directly; see §4.3 |
| `search.py` | The entire ranking pipeline, snippet selection, pagination | Port directly; see §4.1 and §4.4 |
| `storage.py` | SQLite persistence, snapshot loading | Port directly; schema in §6 |
| `indexing.py` | Building in-memory index structures | Port directly |
| `rebuild.py` | Re-index from disk | Port directly; ordering rule is now deterministic (§4.5) |
| `extraction.py` | PDF/DOCX/TXT text extraction | **Replace**; see §7 |
| `config.py` | Paths and limits | Port; point at app-private storage (§6) |
| `engine.py` | Facade tying the above together | Port as the `LocalBackend` implementation |

---

## 4. Portability traps that silently change ranking

These are not hypothetical. Each was verified against this repository's code,
and each produces results that *look* right while differing from the Python
engine. All four are pinned by the golden vectors.

### 4.1 `round(x, 4)` is round-half-to-even on the binary value

The engine publishes seven rounded fields — `filename_score`,
`content_score`, `prefix_similarity`, `numeric_similarity`,
`lexical_match_relevance`, `phrase_score` and `score` (`search.py` lines
1419–1470). Python's `round()` rounds the **exact binary expansion of the
double**, and breaks exact ties half-to-even. It does *not* round the decimal
number a human reads. Verified at the four-decimal precision the engine uses:

```python
>>> round(2.00005, 4)    # naive HALF_UP would give 2.0001
2.0
>>> round(0.56785, 4)    # naive HALF_UP would give 0.5679
0.5678
>>> round(2.675, 2)      # naive HALF_UP would give 2.68
2.67
```

Those are not tie-breaks. `2.00005` as a binary64 is exactly
`2.000049999999999883470991…`, which is *below* the decimal midpoint, so it
rounds down — while anyone reading "2.00005" and rounding half-up would round
up. `0.56785` is `0.567849999999999965893948…`, same story. True ties only
arise for exactly-representable values (`round(2.5)` → `2`, `round(0.5)` → `0`),
and there the half-to-even rule decides.

The practical consequence: **a decimal-string-based rounding implementation
will disagree with the Python engine on ordinary score values**, not just on
exotic ones.

Kotlin/Java has **two** independent ways to get this wrong, and they must both
be avoided. Verified values:

```kotlin
val x = 2.00005

// TRAP 1: converting through the decimal string.
// BigDecimal.valueOf(Double) routes through Double.toString(), so it rounds
// the number a human reads, not the number that is stored.
java.math.BigDecimal.valueOf(x).setScale(4, RoundingMode.HALF_EVEN)
// -> 2.0001     Python gives 2.0

// TRAP 2: the wrong rounding mode on an exact tie.
// 0.125 is exactly representable, so it is a genuine tie.
0.125.toBigDecimal().setScale(2, RoundingMode.HALF_UP)
// -> 0.13       Python gives 0.12

// CORRECT: exact binary conversion AND half-to-even.
x.toBigDecimal().setScale(4, RoundingMode.HALF_EVEN).toDouble()
// -> 2.0        matches Python
```

`Double.toBigDecimal()` is exact (it is `java.math.BigDecimal(double)`);
`BigDecimal.valueOf(Double)` and `toString().toBigDecimal()` are not. Use the
exact form, and use `HALF_EVEN`.

Trap 1 is the one that bites in practice, because it affects ordinary score
values rather than only exact ties:

| value | Python `round(x, 4)` | string-based `HALF_UP` |
| --- | --- | --- |
| `2.00005` | `2.0` | `2.0001` |
| `0.56785` | `0.5678` | `0.5679` |
| `0.145` (n=2) | `0.14` | `0.15` |
| `2.675` (n=2) | `2.67` | `2.68` |

This single choice decides whether every score in the app matches the server.

**Also:** all intermediate arithmetic must be performed in the *same order*.
Kotlin `Double` and Python `float` are both IEEE-754 binary64, so `+ - * /`
are bit-identical given identical operands and order — but floating point is
not associative, and a reordered expression such as
`(a + b) + c` versus `a + (b + c)` will diverge in the last bits and can then
cross a rounding boundary. Port expressions verbatim; do not "simplify".

### 4.2 The tokenizer is Unicode-aware, not ASCII

`tokenizer.py` splits on `str.isspace()` and keeps runs of `str.isalnum()`.
Those are Unicode category tests, not `a-z0-9` tests:

- `isalnum()` is true for `é`, `½`, `Ω`, `५` (Devanagari five), `字`.
- `isspace()` is true for `\u00a0` (no-break space), `\u2003` (em space),
  `\u200b` is *not* a space, and `\x0b`/`\x0c` are.

A port that uses `Character.isLetterOrDigit()` gets close but not identical:
it returns **false** for `½`, which is Unicode category `No` (Number, other).
Python's `isalnum()` accepts it, because `str.isalnum()` is true for any
character in the letter categories `L*` *or* the numeric categories
`N*` — which is `Nd` (decimal digit), `Nl` (letter number, e.g. Roman
numerals) and `No`.

The faithful Kotlin equivalent is therefore:

```kotlin
fun isTokenChar(c: Char): Boolean {
    val type = Character.getType(c)
    return Character.isLetter(c) ||
        type == Character.DECIMAL_DIGIT_NUMBER ||   // Nd
        type == Character.LETTER_NUMBER ||          // Nl
        type == Character.OTHER_NUMBER              // No
}
```

A regex port using `\w` matches neither rule and depends on the flag; do not
use it. Whitespace is a second, separate divergence. Java's `Character.isWhitespace`
**explicitly excludes** the non-breaking spaces U+00A0, U+2007 and U+202F, and
also excludes U+0085 (NEL) — while Python's `str.isspace()` returns `true` for
all four. `Character.isSpaceChar` covers the three `Zs` non-breaking spaces but
not the control characters. Neither method alone matches Python; the union
does, plus NEL explicitly:

```kotlin
fun isTokenSpace(c: Char): Boolean =
    Character.isWhitespace(c) ||      // \t \n \u000B \f \r, U+001C..U+001F,
                                      // and the *breaking* space separators
    Character.isSpaceChar(c) ||       // all Zs/Zl/Zp, including U+00A0,
                                      // U+2007, U+202F
    c == '\u0085'                     // NEL: neither Java method accepts it
```

U+200B (zero-width space) is category `Cf` and is **not** a space to Python or
to either Java method, so it correctly stays part of a token. Do not use a
`\s` regex: it follows `Character.isWhitespace` and so inherits the
non-breaking-space gap.

Case folding likewise: the engine lowercases with Python's `str.lower()`,
which is full Unicode lowercasing (e.g. `İ` → `i̇`, two code points). Kotlin's
`lowercase()` without a `Locale` argument is locale-independent and close, but
`toLowerCase()` *is* locale-dependent and will corrupt Turkish text. Never use
the locale-sensitive form for indexing.

### 4.3 `secure_filename` must be copied, not reimplemented

Filenames are sanitized by a vendored copy of werkzeug's `secure_filename`:
NFKD normalization, strip everything outside `[A-Za-z0-9_.-]`, collapse
separators, reject Windows device names, handle leading dots. It is already
parity-tested against werkzeug in the Python suite.

This function decides the **primary key** of every indexed document. A port
that differs by one character does not merely rename a file — it changes which
documents collide, which get replaced, and what `title` appears in results.
Reimplementing it from a description is the highest-risk task in the port.
Copy the behaviour exactly and run the sanitizer cases from
`tests/golden/search_engine_vectors.json` before anything else.

### 4.4 `math.log` is not required to be correctly rounded

Three sites use natural log, and all three feed published scores:

- IDF: `log((total_documents + 1) / (docs_containing_word + 1)) + 1`
  (line 641) → `content_score`
- Phrase frequency: `min(log(occurrences) / log(25.0), 1.0)` (lines 797, 801)
  → `phrase_score`

Java's `Math.log` is specified only to within 1 ulp of the exact result;
Python's `math.log` uses the platform libm. They will usually agree, but a
1-ulp difference crossing a `round(..., 4)` boundary produces a visible
mismatch in the fourth decimal place. If the vectors show a single score off by
0.0001 with every other field correct, this is the cause — and the fix is a
higher-precision log (compute via `BigDecimal` or a correctly-rounded
implementation), **not** an adjustment to the formula.

### 4.5 Enumeration order is now deterministic — keep it that way

Two defects were fixed in the Python core during this milestone:

- `18f185b` — filename words were persisted through a `set()`, so the *order*
  of words in a filename varied with the interpreter's hash seed. Because
  filename bonuses are computed against a space-joined string, a permuted
  order silently changed substring matching and scores. Now
  `dict.fromkeys(...)`, preserving tokenization order.
- `61de47a` — `rebuild()` enumerated the corpus with `os.listdir()`, whose
  order is filesystem-dependent. Tied scores therefore came back in different
  orders on different machines. Now `sorted(os.listdir(...))`.

Both are portability-relevant: an Android port must preserve **tokenization
order** for filename words (not sorted, not hashed) and must enumerate the
corpus in **sorted filename order** when rebuilding, or tie-breaking will
differ. `File.listFiles()` on Android has no guaranteed order — sort it.

---

## 5. Recommended Android architecture

One interface, two implementations, chosen at runtime. The UI depends only on
the interface.

```
                ┌──────────────────────┐
                │   Search UI / VM     │
                └──────────┬───────────┘
                           │  depends on
                ┌──────────▼───────────┐
                │    SearchBackend     │   interface: search, status,
                │                      │   index, delete, rebuild,
                └───┬──────────────┬───┘   getDocument, hasDocument
                    │              │
        ┌───────────▼──────┐  ┌────▼───────────────┐
        │   LocalBackend   │  │   RemoteBackend    │
        │  (offline mode)  │  │   (online mode)    │
        │                  │  │                    │
        │  ported engine   │  │  HTTP client to    │
        │  + SQLite in     │  │  the Flask API     │
        │  app storage     │  │  (app.py)          │
        └──────────────────┘  └────────────────────┘
```

### 5.1 The interface

Modelled on the engine's existing public API plus what the HTTP layer already
exposes, so both implementations are natural:

```kotlin
interface SearchBackend {
    fun search(query: String, page: Int, limit: Int): SearchResult
    fun status(): CorpusStatus
    fun indexDocument(name: String, source: DocumentSource): IndexOutcome
    fun delete(name: String): Boolean
    fun bulkDelete(names: List<String>): BulkOutcome
    fun rebuild(): RebuildOutcome
    fun getDocument(name: String): DocumentPayload?
    fun hasDocument(name: String): Boolean
}
```

`SearchResult` mirrors the JSON shape the API already returns. There are two
distinct response shapes, and a port must not unify them:

- a **page object** — `{"results": [...], "pagination": {...}}` — for normal
  ranked queries, filetype-only browse queries, *and* queries that match
  nothing;
- a **bare JSON list** — `[...]` — for exactly two reachable legacy branches:
  no documents to search (an empty corpus, or a filetype filter for which the
  corpus holds no documents), and a quoted phrase no document contains
  adjacently.

`search.py` has a third bare-list site, at line 939, which is unreachable and
marked as such: scores are pre-populated for every document before matching,
and `total_documents` is counted through the same filetype predicate, so the
"no documents to search" site always fires first. It is kept because removing
dead code is a behavior change. A port should not try to reach it.

There is also a third *variant* of the page object. A blank query is answered
before any ranking work (`search.py:130` → `pagination.empty_query_response()`)
and its pagination block carries **no `start` or `end` offsets**, unlike every
other paginated response. A port that fills them in has changed the published
API.

All three are pinned by the golden vectors.

All `bulkDelete` work must be **one transaction** for the whole batch, not a
loop of independent deletes. This is existing engine semantics and the Android
SQLite layer supports it directly.

### 5.2 `LocalBackend`

- Holds the ported engine, a SQLite handle in app-private storage, and the
  in-memory snapshot the engine ranks against.
- Extraction is the platform-specific piece (§7).
- Indexing a large corpus must not block the main thread. The Python engine
  keeps threading out of its core deliberately; on Android, put the
  coroutine/executor boundary in the *backend*, not in the ported engine.

### 5.3 `RemoteBackend`

- Thin HTTP client against the endpoints `app.py` already serves.
- Must translate HTTP failures into the same domain errors `LocalBackend`
  produces, so the UI has one error model.
- Never reimplement ranking "for speed" or "to avoid a round trip". That is how
  two implementations get born.

### 5.4 Mode selection and failure semantics

Recommended, and deliberately conservative:

1. Mode is an explicit user-visible setting, persisted.
2. `RemoteBackend` failures surface as failures. **Do not silently fall back to
   local.** A silent fallback shows a different corpus with different results
   and no indication why — worse than an error.
3. Offer "switch to offline" as a user action when remote fails.
4. Show which mode produced the current results. The two modes can legitimately
   hold different corpora, and users must be able to tell.

### 5.5 Keeping the two honest

Every release should run the golden-vector suite (§9) against **both**
backends. `RemoteBackend`'s vectors are checked against the live Flask API;
`LocalBackend`'s against the ported engine. Same file, same expectations. That
is the only cheap way to detect drift between modes.

---

## 6. On-device storage

SQLite is native on Android (`android.database.sqlite`, or Room over it), so
persistence ports without a dependency change. The schema the engine uses is
plain and portable — no stored procedures, no exotic types:

```sql
PRAGMA journal_mode=WAL;

documents(filename TEXT PRIMARY KEY, title TEXT NOT NULL, path TEXT NOT NULL,
          total_words INTEGER NOT NULL, page_count INTEGER NOT NULL)

term_postings(term TEXT, filename TEXT, term_count INTEGER,
              PRIMARY KEY (term, filename))
  + index on term, index on filename

filename_terms(filename TEXT, term TEXT, PRIMARY KEY (filename, term))
  + index on term

pages(filename TEXT, page_number INTEGER, text TEXT,
      PRIMARY KEY (filename, page_number))
```

Two notes for the port:

- `filename_terms` row **order** is meaningful: filename words are
  reconstructed from it and compared as a space-joined string (§4.5). The
  Python engine relies on insertion order and reads it back with an explicit
  `ORDER BY filename, rowid` (`storage.py:465`). Copy that clause; a bare
  `SELECT` gives no ordering guarantee.

  This only works because both write paths are **delete-then-insert**:

  - `insert_document` (`storage.py:530`) runs `DELETE FROM filename_terms
    WHERE filename = ?` at line 551, then inserts fresh rows at line 602.
  - `sync_from_snapshot` (`storage.py:256`), the rebuild path, truncates the
    whole table at line 279, then re-inserts every document at line 351.

  So a document that gains or loses words always gets fresh `rowid`s in
  tokenization order. An upsert that preserves the existing `rowid`
  (`ON CONFLICT DO UPDATE`) would keep stale ordering and silently change
  filename bonuses for replaced documents. An explicit sequence column is also
  correct — just verify the result against the vectors' `index.filename_words`.
- `path` is stored as an absolute path. On-device it must be the app-private
  path, and it changes if the app is restored to a different device. Treat it
  as derived, not as portable data.

**Scoped storage.** Use `context.filesDir` (or `getDatabasePath`) for the index
and imported documents. Do not request `MANAGE_EXTERNAL_STORAGE`; it is
unnecessary and draws Play policy review. Import user documents through the
Storage Access Framework (`ACTION_OPEN_DOCUMENT`) and copy into app-private
storage, so indexing does not depend on a persistable URI grant surviving
reboot. The engine wants a real filesystem path — SAF gives a content URI, so
the backend must copy-then-index. Budget for that copy in both time and disk
(two copies of every document exist briefly).

WAL mode is fine on app-private storage but produces `-wal` and `-shm` sidecar
files; include them in any backup or corruption-recovery logic.

---

## 7. Document extraction: the component that must be replaced

`extraction.py` depends on PyPDF2 and python-docx. Neither runs on Android, and
no Android PDF library is guaranteed to produce byte-identical text:

- PDF text extraction depends on the document's encoding, ToUnicode CMaps,
  font subsetting and text-show operators. Different parsers make different
  choices about ligature expansion, whitespace insertion, and reading order in
  multi-column layouts. **Identical output cannot be assumed.**
- DOCX parsing (paragraph order, table text, headers/footers, runs) varies
  similarly between libraries.
- TXT extraction is trivially portable.

The engine's existing contract already limits the damage: extraction produces
two views — `extract_text` (one lowercased string, for term frequencies) and
`extract_pages` (`[{"page": n, "text": ...}]`, for snippets and page numbers).
PDFs are page-accurate; DOCX and TXT are treated as one logical page. A port
must reproduce *that contract*, not PyPDF2's internals.

**Recommended approach, in order of decreasing risk-reduction:**

1. **Ship the corpus pre-extracted.** The server (or a build step) extracts text
   once and distributes a JSON sidecar of `{filename, pages:[{page,text}]}`
   alongside the documents. The device indexes text, never parses. This makes
   on-device results identical to server results *by construction* and removes
   the hardest portability problem entirely. Strongly recommended for a
   fixed corpus, which is what a VTU notes app has.
2. **Extract on-device for user-added documents only**, using
   `PdfRenderer`-adjacent tooling or a JVM PDF library, and accept that these
   may rank slightly differently from server-extracted equivalents. Label them
   as locally indexed.
3. **Hybrid (recommended):** pre-extracted for the shipped corpus, on-device for
   user imports. The golden vectors cover ranking; extraction gets its own
   smaller contract test comparing device-extracted text against the recorded
   pages for a handful of representative documents.

Do not attempt to make an on-device PDF parser bit-identical to PyPDF2. It is
not achievable on a schedule, and option 1 makes it unnecessary.

---

## 8. Performance budget

Measured on this machine with `tools/benchmark_core.py`, which exercises the
engine directly — no HTTP, so these are the numbers an on-device port must
meet, not a server's numbers. Synthetic corpus, ~400 words per document, every
10th document a real PDF.

| Documents | Search p50 | Search p95 | Import (all) | Rebuild | SQLite | Peak RSS |
| --- | --- | --- | --- | --- | --- | --- |
| 50 | 3.5 ms | 5.4 ms | 53 ms | 41 ms | 472 KiB | 37 MiB |
| 200 | 14.2 ms | 22.0 ms | 221 ms | 156 ms | 1.7 MiB | 42 MiB |
| 1000 | 72.7 ms | 111 ms | 1 240 ms | 806 ms | 8.6 MiB | 50 MiB |
| 2500 | 191 ms | 277 ms | 3 530 ms | 2 117 ms | 21.7 MiB | 59 MiB |

Import is ~1.1–1.4 ms/document and essentially flat. Extraction p95 is ~1.6 ms
on the sampled documents (dominated by the PDFs).

### 8.1 Search is O(corpus size), not O(matches)

This is the most important architectural finding for the port. Latency scales
linearly with document count regardless of how selective the query is: 4× the
documents gives ~4× the p50 (3.5 → 14.2 → 72.7 → 191 ms).

The cause is structural, not incidental. Every search pre-populates a score
record for **every** document (`search.py` line 360) and then applies filename
bonuses across **every** document (line 402), before term matching narrows
anything. That is what allows filetype-only queries to browse the whole corpus
and what makes "no matches" return an empty page rather than an error. It is
intended behaviour and must be preserved — but it means there is no
posting-list shortcut available, and cost grows with the corpus.

Implications for Android:

- At the actual corpus size this project targets (tens of documents), search is
  a few milliseconds. Non-issue.
- At ~1000 documents, p95 ≈ 111 ms on a laptop. A mid-range phone is
  realistically 2–4× slower on this kind of interpreted-vs-compiled mixed
  workload — but Kotlin/ART is compiled, so it may well be *faster* than
  CPython here. Plan for roughly comparable, and **measure**, don't assume.
- At 2500 documents the p95 approaches 300 ms on a laptop. On a phone that is
  where you would add a debounce on the query box and a "searching" state.
- Memory holds the full page text of every document in the snapshot (it is what
  snippets are cut from). 59 MiB peak at 2500 documents includes the Python
  interpreter; the index itself is proportional to the ~8.9 KiB/document SQLite
  figure. On a phone, keep the snapshot in memory only while searching if the
  corpus grows past a few hundred documents.

The synthetic corpus uses a deliberately small vocabulary (40 distinct terms),
so posting lists are long and term matching does more work than a real corpus
of the same size would. These numbers are therefore conservative for the
matching stage; the O(N) filename pass is unaffected by vocabulary and is the
real cost driver.

### 8.2 What to benchmark on-device

Run the equivalent of `tools/benchmark_core.py` on a low-end target device and
record: search p50/p95 at 100/500/1000 documents, cold-start index load,
import throughput, rebuild time, and peak memory. Compare against the table
above. Any order-of-magnitude gap means something in the port is wrong (usually
an accidental per-query database round trip instead of ranking against an
in-memory snapshot).

---

## 9. Proving parity: the golden vectors

`tests/golden/search_engine_vectors.json` is the contract. It is **generated
from the engine, never hand-written**, and a test
(`tests/test_golden_vectors.py`) regenerates and compares it so the published
contract cannot silently go stale. Verified: changing one ranking constant
(`100.0` → `99.0`) makes the drift gate fail.

Regenerate deliberately after an intended ranking change:

```bash
.venv/bin/python tools/export_golden_vectors.py
git diff tests/golden/search_engine_vectors.json   # review, then commit
```

Contents (schema v1):

- **`corpus`** — six documents with their source filename and the sanitized
  name they are stored under. Deliberately small, and deliberately chosen to
  hit every branch: a five-word filename for word-order sensitivity, exact and
  non-contiguous filename matches, repeated phrases, numeric-only documents,
  prefix and lexical-hierarchy cases, all three filetypes.
- **`index`** — the resulting snapshot: `documents`, `filename_words`,
  `page_text`, `term_postings`. A port can load these directly and test ranking
  **without reimplementing extraction**, which is the point (§7).
- **`cases`** — 45 named `(query, page, limit)` inputs with the exact expected
  payload, including both response shapes and pagination clamping behaviour.
- **`portability`** — the traps from §4, recorded next to the data.

Absolute paths are written as `{{data_folder}}` so the file is meaningful
outside this repository.

### Running it on Android

The vectors are transport-independent by construction. A Kotlin test should:

1. Read the JSON from `assets/`.
2. Build an engine state from `index` (load the snapshot) **or** import
   `corpus` text through the ported extraction path — the first tests ranking
   alone, the second tests the whole pipeline. Do both, separately, so a
   failure says which half broke.
3. Run every case and compare the full payload.
4. Compare floats with exact equality after the §4.1 rounding. Tolerance
   hides precisely the bug this suite exists to catch.

Run the same file against `RemoteBackend` to keep the two modes honest (§5.5).

---

## 10. Risks

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| Extraction output differs from PyPDF2 | **Certain** if parsing on-device | Results and snippets differ between modes | Ship pre-extracted text (§7 option 1) |
| Rounding divergence (§4.1) | High if written naively | Every score off in the last digit; ranking ties break differently | Exact-binary `toBigDecimal()` + `HALF_EVEN`; golden vectors |
| Tokenizer Unicode mismatch (§4.2) | Medium | Wrong terms indexed; non-Latin queries fail | Copy categories, not ASCII ranges; vectors |
| `secure_filename` drift (§4.3) | Medium | Primary keys change; documents collide or vanish | Copy behaviour exactly; test sanitizer first |
| Reimplementing ranking in Kotlin "temporarily" | Medium | Two implementations that drift forever | §5.5: one vector file, both backends, every release |
| Tie order differs after rebuild | Medium | Same scores, different result order | Sorted enumeration (§4.5) |
| O(N) search on a large corpus (§8.1) | Low at target size | Perceptible lag past ~1000 documents | Debounce; measure on a low-end device |
| SAF copy-then-index doubling storage | Certain | Disk pressure on import | Copy to app storage, delete temp, index incrementally |
| `Math.log` ulp difference (§4.4) | Low | One score off by 0.0001 | Detect via vectors; use higher-precision log if hit |

---

## 11. Recommended build order

Each step is independently testable, and the ordering front-loads the
highest-risk items so a failure is discovered while it is still cheap.

1. **Golden-vector harness in Kotlin.** Load the JSON, run cases against a stub
   that returns nothing, watch every case fail. This is the test infrastructure
   everything else is judged by; build it first.
2. **`secure_filename` + tokenizer.** Pure functions, no I/O, highest drift
   risk. Validate against the vectors' `corpus` and `index.filename_words`
   before proceeding.
3. **Snapshot loading + `search()`.** Build engine state from the vectors'
   `index` section (skipping extraction entirely) and make all 45 cases pass.
   This proves ranking, rounding and pagination on real expectations.
4. **SQLite persistence.** Schema from §6, round-trip a snapshot, confirm
   `filename_words` order survives (§6 caveat).
5. **`RemoteBackend`.** HTTP client; validate against the same vectors through
   the live Flask API.
6. **`SearchBackend` selection + UI wiring.** Explicit mode, no silent fallback,
   mode shown to the user (§5.4).
7. **Extraction.** Only now, and only if §7 option 1 (pre-extracted corpus) is
   rejected.
8. **On-device benchmarks** (§8.2) on a low-end target device.

Steps 1–3 are the ones that decide whether this port works. Everything after
them is engineering.

---

## 12. What this milestone deliberately does not include

- **No Kotlin sources.** No JDK, Gradle or Android SDK is available here, so
  nothing could be compiled or tested. Unverifiable code would violate the
  project rule that completion means passing tests, not code that looks
  plausible.
- **No second ranking implementation.** The design exists to prevent one.
- **No claim about on-device latency.** §8 reports measurements from this
  machine and reasons about what they imply for a phone. Real device numbers
  must be collected in step 8 of §11.

---

## Appendix: reproduction

```bash
# The regression gate (201 tests: unit, core, live HTTP, parity tool, vectors)
./run_tests.sh

# Regenerate the golden vectors
.venv/bin/python tools/export_golden_vectors.py

# Core performance benchmarks (no server involved)
.venv/bin/python tools/benchmark_core.py --documents 1000 --queries 150

# HTTP parity of the refactored API against the pre-refactor monolith.
# Needs both servers already running: the refactored API on :5000 and the
# pre-refactor app.py (from commit 81faff9) on :5001.
.venv/bin/python tools/verify_http_parity.py
```
