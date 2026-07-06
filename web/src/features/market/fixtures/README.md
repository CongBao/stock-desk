# Market contract fixtures

These JSON files are serialized backend contract values, not hand-authored web
mocks. Regenerate them with the corresponding Pydantic DTOs in
`src/stock_desk/api/market.py` and the canonical backend hash helpers:

- routing manifests: `make_routing_manifest`
- manifest record IDs: `manifest_record_id`
- preset snapshot IDs: `stock_desk.market.pools._snapshot_id`

`backend-preset-pool-response.json` intentionally pins an AKShare instrument
catalog while its composition comes from Tushare with different dataset,
route, and observation times. Those two provenance records are independent;
the preset snapshot binds the complete composition to the pinned catalog
manifest and instrument dataset versions.
