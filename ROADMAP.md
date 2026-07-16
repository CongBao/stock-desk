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
| v1.1 — Windows desktop experience | Complete | Windows x64 only: standalone Tauri desktop shell, per-user NSIS install, first-run setup, searchable/default stock entry, responsive desktop UX, recovery/uninstall controls, and an explicitly unsigned exact-main release. |

Released means the capability is recorded in the corresponding changelog entry;
it does not expand the research-only or local trust boundaries.

## Planned

| Stage | Status | Intended outcome |
| --- | --- | --- |
| v1.2 — Microsoft Store distribution | Planned | Package the Windows desktop app as MSIX for a personal Microsoft Store account. The Store edition is a fresh install and does not read or migrate v1.1 data; onboarding runs again. |

v1.1 is Windows x64 only. New macOS, Linux, Android, and Windows ARM64
distributions remain outside its release scope. v1.2 Store signing does not
retroactively change v1.1's unsigned status.

No later-stage capability is considered complete merely because its navigation route or layout preview exists.
