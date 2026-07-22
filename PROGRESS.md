# Progress

## C1 repository extraction and API isolation - 2026-07-22

Status: implemented and locally verified; production TLS/reverse-proxy provisioning is
not performed by this repository.

- Created an independent package, dependency set, virtual-environment boundary, runtime
  roots, engineering rules, and Git history.
- Removed all imports of the AI investment `kernel`, `execution`, `research`, and
  `data_plane` packages.
- Added the single Alpaca SIP owner, idempotent raw event ledger, and versioned
  point-in-time feature library.
- Added immutable parameterized, safe-expression, and container-isolated Python strategy
  definitions with `strategy_id`-scoped artifacts and derived signals.
- Added separate hashed bearer-token scopes for AI feature access and collaborator
  signal access. Cross-strategy signal reads fail closed.
- Added only `/health`, `/v1/features/{symbol}`, and
  `/v1/strategies/{strategy_id}/signals`; raw-market proxy and order routes do not exist.
- Custom Python requires a digest-pinned, non-root, networkless, read-only container with
  dropped capabilities, no-new-privileges, and CPU/memory/PID/time limits.
- Repository acceptance: 7 tests passed, Ruff clean, and strict mypy success across
  18 source files.
