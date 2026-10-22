# Portfolio Upgrade Roadmap

This roadmap prioritizes demonstrable engineering quality over adding fashionable dependencies. The current implementation already has a ranking contract, golden vectors, SQLite persistence, a React UI, and parity tooling. Preserve those invariants while improving reproducibility, operational safety, relevance measurement, and deployment readiness.

## Baseline (verified from repository documentation)

- Flask search API and React/Vite UI.
- Search supports content and filename matching, prefix and numeric-substring matching, phrase scoring, file-type filtering, snippets/highlights, and pagination.
- Ranking behavior is pinned by golden vectors; the repository documents 448 tests passing with the live server and 439 passing plus 9 skipped without it.
- Android is a specification/port-in-progress, not a deliverable APK. Do not claim an APK exists until it is built, installed, and tested.

These figures are repository-documented claims, not a fresh test run performed by this roadmap change.

## Upgrade sequence

### P0 — Reproducible quality gates (start here)
- Run backend verification and frontend lint/build automatically on pull requests and pushes.
- Make failures visible in GitHub Actions instead of relying on manual local commands.
- Keep CI dependencies explicit and use the repository's existing verification script.
- Confirm the workflow itself is green before using it as a portfolio badge.

**Acceptance:** the workflow passes on a clean GitHub-hosted runner; all required gates run, and no required job silently skips.

### P1 — Security and reliability baseline
- Add a documented threat model for file uploads, path handling, resource exhaustion, and cross-origin requests.
- Test upload size limits, allowed/unsupported file types, malformed PDF/DOCX input, duplicate filenames, traversal-like filenames, and concurrent writes.
- Verify uploaded filenames cannot escape the configured corpus directory; store generated server-side names if necessary.
- Add bounded request sizes and safe error responses; avoid returning tracebacks or absolute server paths.
- Add explicit CORS allowlists for deployed environments; do not treat permissive development CORS as production-safe.
- Add health/readiness semantics and structured, privacy-conscious request logs.

**Acceptance:** each security requirement has a regression test; upload and search failures return stable API contracts without leaking local paths or stack traces.

### P2 — Relevance evaluation, not just implementation tests
- Build a versioned query/document relevance dataset based on the target domain.
- Define metrics before changing ranking: Recall@k, Precision@k, MRR, and nDCG@k where graded relevance exists.
- Add a reproducible benchmark command that records dataset version, corpus size, query count, ranking configuration, and metric values.
- Compare candidate ranking changes against the current golden vectors and relevance baseline. Do not replace exact-contract tests with metric tests; they catch different failure classes.
- Report latency distributions (p50/p95), index-build time, and memory usage on a declared hardware/corpus profile.

**Acceptance:** a new ranking change includes before/after metric results and does not regress the pinned API contract without an explicit reviewed decision.

### P3 — Search and indexing capability
Prioritize by measured user need, not by feature count:
1. Extract and normalize document text with clear per-format error reporting.
2. Add incremental indexing and a recoverable index rebuild path with observable status.
3. Consider BM25 as an experimentally evaluated ranking option alongside the current ranker, not an unmeasured replacement.
4. Improve query normalization and snippet generation with multilingual, Unicode, punctuation, and long-document tests.
5. Add filter/facet support only where the corpus metadata supports useful filters.

**Acceptance:** indexing and ranking are benchmarked on representative corpora; malformed documents do not corrupt previously indexed content.

### P4 — Production packaging and deployment
- Add a production WSGI entry point and deployment configuration; do not use Flask's development server as a production server.
- Add containerization only after the runtime configuration, persistent storage path, health check, and upload limits are explicit.
- Document backup/restore for SQLite and corpus files, migration policy, environment variables, and rollback steps.
- Keep secrets out of source control and use least-privilege deployment credentials.

**Acceptance:** a clean-machine deployment guide works end-to-end and data survives container restart/redeployment.

### P5 — Android offline mode (separate milestone)
- Compile the Kotlin implementation with a real JDK/Gradle toolchain before claiming parity.
- Add contract tests that replay the same golden query vectors against the Android LocalBackend and compare complete normalized responses.
- Verify SQLite schema, file access, document import, lifecycle/recovery, and offline behavior on an emulator and at least one physical device.
- Build and sign a release APK reproducibly; document signing-key handling without committing private keys.

**Acceptance:** CI compiles the Android module; shared vectors pass; APK installs and searches offline on a real device. Until then, label Android as incomplete.

## Definition of done for portfolio claims

- Claims link to reproducible commands, CI runs, benchmark outputs, or screenshots.
- Performance numbers include corpus size, machine profile, and methodology.
- No invented test counts, speedups, scalability claims, or APK links.
- Main remains protected by review and passing CI where repository settings permit.
- README includes a concise architecture diagram, demo instructions, limitations, and a clear status table.
