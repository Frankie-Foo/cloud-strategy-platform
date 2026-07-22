# API v1

## Authentication scopes

| Scope | Intended principal | Accessible route |
|---|---|---|
| `features:read` | AI investment service | `GET /v1/features/{symbol}` |
| `signals:read` | collaborator | `GET /v1/strategies/{strategy_id}/signals` |

Tokens are random bearer secrets. Only SHA-256 digests are stored. A signal token is
bound to exactly one `strategy_id`; a feature token cannot be converted into a signal
grant. Tokens must be delivered through a secret manager in production.

## Health

```http
GET /health
```

Returns `{"api_version":"v1","status":"ready"}` without disclosing storage,
credentials, strategies, symbols, or market state.

## Point-in-time features

```http
GET /v1/features/AAPL?asof=2026-07-22T15%3A30%3A00Z
Authorization: Bearer <features:read-token>
```

Every value includes `asof_utc`, `definition_version`, and `provenance`. The response
is `null` when no vector exists at or before the requested UTC time. Missing facts are
not estimated.

## Derived signals

```http
GET /v1/strategies/gap_momentum_a/signals?since=2026-07-22T14%3A30%3A00Z
Authorization: Bearer <strategy-specific-signals:read-token>
```

Signals contain only the strategy/version identity, symbol, UTC time, long-only action,
reason, and feature provenance. No raw price/quote payload, provider credential, proxy
URL, Broker state, or order function is returned.

## Deliberately nonexistent routes

`/raw`, `/proxy`, `/alpaca`, `/accounts`, `/positions`, `/tradeplans`, `/orders`, and
all write methods are outside this service contract and return 404/405.
