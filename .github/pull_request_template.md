## Summary

Describe the problem, the chosen change, and its user-visible effect.

## Verification

List the exact commands run and their results. For behavior changes, include the focused RED and GREEN evidence.

## Checklist

- [ ] The change is focused and contains no secrets, personal data, or licensed market data.
- [ ] New or changed behavior has automated tests written test-first.
- [ ] `make test`, `make lint`, `make typecheck`, `make build`, and `make public-tree` pass.
- [ ] Container-impacting changes were verified with a running Compose stack and `make release-check`.
- [ ] Public docs and `CHANGELOG.md` are updated where needed.
- [ ] I have read and will follow the [Code of Conduct](../CODE_OF_CONDUCT.md).
