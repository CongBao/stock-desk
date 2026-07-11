# Roadmap

This roadmap communicates direction, not a delivery guarantee. Scope and sequencing may change after technical validation and user feedback.

## Released

| Stage                    | Status          | Intended outcome                                                                                                                                                                                                                   |
| ------------------------ | --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0 — Foundation           | Complete        | Local FastAPI/SQLite service, durable task worker, React shell, encrypted secret storage, reproducible native/container gates, and public project governance.                                                                      |
| 1 — Market data          | Complete        | Provider adapters, normalized A-share instruments and bars, durable manual/daily updates, provenance-pinned pools, local caching, and interactive daily/weekly/60-minute charts.                                                   |
| 2 — Formulas             | Complete        | A constrained TDX-compatible formula language, immutable versions, three-column Formula Studio, and consistent indicator/signal previews documented by tests.                                                                      |
| 3 — Backtests            | Complete        | Reproducible daily, weekly, and 60-minute independent-sample simulations with A-share execution rules, explicit costs, durable pool jobs, conclusion-first reports, pinned replay, and exports.                                    |
| 4 — Intelligent analysis | Complete        | v0.5.0 with user-configured domestic/OpenAI-compatible/Ollama models, immutable nine-stage research runs, category-routed cached evidence, citations, partial and insufficient-evidence reports, and failed-stage retry.           |
| 5 — Integrated v1 desk   | Complete        | v1.0.0 with the complete low-code workstation, task center, responsive icon navigation, cross-platform native installers, performance/security/recovery gates, and bilingual screenshot documentation.                           |

Released means the capability is recorded in the corresponding changelog entry;
it does not expand the research-only or local trust boundaries.

## Planned

| Stage | Status | Intended outcome |
| --- | --- | --- |
| v1.1 Stage 0 — Delivery foundation | In progress | Auditable incremental PR feedback, exact-SHA full `main` proof, content-addressed artifacts, and unsigned alpha prerelease boundary. |
| v1.1 Stages 1–5 — Windows desktop UX | Planned | Tauri desktop shell, first-run real-data onboarding, consumer desktop UX, trusted current-user distribution, and formal Windows `v1.1.0`. |

v1.1 is Windows x64 only. New macOS, Linux, Android, and Windows ARM64
distributions are outside this release scope.

No later-stage capability is considered complete merely because its navigation route or layout preview exists.
