# Cloud Strategy Platform

Independent, signal-only cloud platform for shared Alpaca SIP ingestion, versioned
point-in-time features, isolated multi-strategy research, and least-privilege APIs.

This is not a Broker or execution service. The repository contains no order adapter,
account access, position access, TradePlan submission, or short-selling action.

## Repository boundary

- This repository owns the one cloud Alpaca SIP connection and its credentials.
- Strategy configuration, selection, backtest, Paper/shadow, and review artifacts are
  isolated by `strategy_id`.
- AI investment services use an HTTPS `features:read` token.
- Collaborators use a strategy-specific `signals:read` token.
- Raw SIP events and proxy capabilities are never exposed by HTTP.
- Custom Python runs only in the locked-down container under
  `deploy/strategy-sandbox`.

The API contract is documented in [docs/API.md](docs/API.md).

## Verification

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\ruff check .
.\.venv\Scripts\mypy cloud_strategy_platform scripts tests
```

## Local administration

Register a strategy, issue a service token, and start the loopback API:

```powershell
.\.venv\Scripts\python -m scripts.register_strategy strategy.json --activate
.\.venv\Scripts\python -m scripts.issue_token `
  --principal ai-quant --scope features:read
.\.venv\Scripts\python -m scripts.serve_api --host 127.0.0.1 --port 8765
```

For production, keep the Python server on a private interface behind an authenticated
TLS reverse proxy. Never bind it directly to the public internet.

The SIP owner reads `ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY` only in its process:

```powershell
.\.venv\Scripts\python -m scripts.run_sip_owner
```

No other service identity should receive those two variables.
