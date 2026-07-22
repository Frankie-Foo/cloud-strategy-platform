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

## C2 scoped market-data and Paper service - 2026-07-22

Status: implemented, credential migration completed, and the local loopback deployment
is running; public internet deployment still requires an HTTPS reverse proxy.

- Added separate `market-data:read`, `paper:read`, and `paper:write` scopes. Collaborator
  `signals:read` tokens cannot read raw events, call provider adapters, inspect Paper
  state, or submit/cancel Paper orders.
- Added normalized raw SIP event polling plus historical bars, quotes, and news routes
  backed by cloud-owned Alpaca credentials; no generic upstream proxy is exposed.
- Added long-only Paper account, positions, open-order, idempotent lookup, submit, close,
  and cancel routes. Cloud Broker writes remain disabled by default.
- Added an atomic credential migration tool that removes Alpaca credentials from the AI
  environment, stores them only in this repository's ignored environment, and issues
  hashed least-privilege service tokens without displaying secrets.
- Installed a current-user loopback API startup entry after Windows denied non-elevated
  scheduled-task registration. The service listens only on `127.0.0.1:8765`.
- End-to-end read-only verification reported an active Paper account, authenticated SIP
  data for AAPL, and zero submitted orders.
- Repository acceptance: 10 tests passed, Ruff clean, and strict mypy success across
  23 source files.
