# v1 acceptance coverage

<!-- requirements-yaml-sha256: 9eddc4fa80b7f976eb6e737130006b85654af50a4ec7ec0a5eaa4b64bb5e51dc -->

The machine-readable acceptance authority is `tests/acceptance/requirements.yml`. The digest above is checked by `scripts/check_requirement_coverage.py` so this public summary cannot silently drift from the matrix.

The matrix maps the 77 authoritative requirements in their original stable-ID order and the 10 explicit non-goals in their original exclusion order. A public-safe frozen registry binds every ID to its behavior key, acceptance-text digest, metadata, and exact semantic reference set. Each entry also has an owning stage, mapping status, and assertion-level evidence.

Evidence state and requirement status are intentionally separate:

- `existing` evidence names a tracked assertion that the mapping checker can collect or a fixed registered gate.
- `planned` evidence names an exact future assertion. It completes the mapping but cannot verify a requirement.
- `manual` evidence is limited to operational or publication work and defines a procedure, final artifact, release gate, and completion state.
- `mapped` means the semantic requirement and its proof contract are complete. `verified` is allowed only when no planned or incomplete manual evidence remains.

Run the mapping gate during development:

```console
uv run python scripts/check_requirement_coverage.py --mode mapping
```

The tag-candidate gate uses `--mode pre-publish`. It rejects every planned
assertion, runs existing pytest evidence with xfail semantics disabled, and
rejects incomplete manual artifacts required by `release-acceptance`. Manual
records explicitly assigned to `final-release-audit` are deferred because some
of them bind the signed tag, public release page, and final lineage that do not
exist before publication.

After publication, `--mode release` is the post-release audit. It rejects every
planned assertion and every incomplete manual artifact, including
`final-release-audit`. A successful mapping or pre-publish check therefore does
not claim the post-publication audit is complete.

Non-goals are enforced by an inventory over public OpenAPI names, API and worker identifiers, Web UI claims, and public documentation claims. The inventory covers the absence of broker/live ordering, shared-capital portfolios, realtime/tick/Level-2 feeds, target prices or specific allocations, a second native product UI, accounts/RBAC/subscriptions/billing, dynamic screening, condition-selection/color-K formulas, drawing/multi-stock/multi-period linkage, and AI formula generation/explanation/repair. A minimal installed launcher that opens the browser workstation is permitted.
