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

## C3 complete API contract and target platform design - 2026-07-22

Status: current v1 contract documented and automatically verified; target `/v2` remains
an explicitly labeled design and is not presented as implemented.

- Added an importable OpenAPI 3.1 contract for exactly the 13 currently implemented HTTP
  operations, including scopes, parameters, request/response schemas, examples, errors,
  idempotency rules, `Cache-Control: no-store`, and the Paper write gate.
- Replaced the short API summary with a complete Chinese reference covering security
  boundaries, token issuance, every route, field examples, error handling, retries,
  cursor behavior, deployment, and known limitations.
- Explicitly documented that current v1 Paper routes are account-global compatibility
  routes and are not yet isolated by `strategy_id`.
- Added a separate target-platform contract for strategy/version management, selection,
  backtests, strategy Paper subledgers, reviews, Python sandbox jobs, artifacts, audit,
  token lifecycle, quotas, migration, and staged acceptance.
- Added executable documentation tests that verify the exact route/scope inventory,
  write-gate errors, local OpenAPI references, forbidden route/secret exclusions, and
  the implemented-versus-target labeling.
- Repository acceptance: 15 tests passed, Ruff clean, and strict mypy success across
  24 source files.

## C4 public repository release hygiene - 2026-07-22

Status: implemented; GitHub CI execution is pending the branch push.

- Corrected the repository boundary description so the documented normalized
  `market-data:read` routes are not confused with collaborator signal access.
- Added an explicit upstream market-data redistribution notice; publishing the software
  does not publish or sublicense market data.
- Added least-privilege GitHub Actions verification for pytest, Ruff, and strict mypy.
- Added a security reporting policy that prohibits public disclosure of credentials,
  account data, runtime databases, orders, and proprietary market-data samples.
