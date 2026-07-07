# v1 acceptance coverage

<!-- requirements-yaml-sha256: f4b2e827c082597de76e13ec467a9086bfccefa99694f620d3d23645f1a33635 -->

The machine-readable acceptance authority is `tests/acceptance/requirements.yml`. The digest above is checked by `scripts/check_requirement_coverage.py` so this public summary cannot silently drift from the matrix.

The matrix maps exactly 60 product/design semantics, 17 operational/publication refinements, and 10 explicit non-goals. A fixed public-safe registry binds each R identifier to one unique authoritative semantic requirement and its valid scenarios. Each entry has one stable behavior key, a complete acceptance statement, an owning stage, mapping status, and assertion-level evidence.

Evidence state and requirement status are intentionally separate:

- `existing` evidence names a tracked assertion that the mapping checker can collect or a fixed registered gate.
- `planned` evidence names an exact future assertion. It completes the mapping but cannot verify a requirement.
- `manual` evidence is limited to operational or publication work and defines a procedure, final artifact, release gate, and completion state.
- `mapped` means the semantic requirement and its proof contract are complete. `verified` is allowed only when no planned or incomplete manual evidence remains.

Run the mapping gate during development:

```console
uv run python scripts/check_requirement_coverage.py --mode mapping
```

The final release gate uses `--mode release`; it additionally rejects every planned assertion and incomplete manual artifact. A successful mapping check therefore does not claim that the release is fully verified.

Non-goals are enforced by an inventory over public OpenAPI names, API and worker identifiers, Web UI claims, and public documentation claims. The inventory covers the absence of broker/live ordering, shared-capital portfolios, realtime/tick/Level-2 feeds, target prices or specific allocations, a second native product UI, accounts/RBAC/subscriptions/billing, dynamic screening, condition-selection/color-K formulas, drawing/multi-stock/multi-period linkage, and AI formula generation/explanation/repair. A minimal installed launcher that opens the browser workstation is permitted.
