# Cloud Strategy Platform

Independent cloud platform for shared Alpaca SIP ingestion, versioned point-in-time
features, isolated multi-strategy research, and least-privilege APIs.

This service owns Alpaca credentials and exposes a narrowly scoped Paper execution API.
It contains no Live endpoint, generic Alpaca proxy, TradePlan promotion, or short action.

## Repository boundary

- This repository owns the one cloud Alpaca SIP connection and its credentials.
- Strategy configuration, selection, backtest, Paper/shadow, and review artifacts are
  isolated by `strategy_id`.
- AI investment services use an HTTPS `features:read` token.
- AI market ingestion uses a separate `market-data:read` token.
- AI Paper execution uses a separate `paper:write` token; writes are disabled by default.
- Collaborators use a strategy-specific `signals:read` token.
- Normalized market events are exposed only to separately scoped AI service identities;
  collaborator tokens can read derived signals only.
- Alpaca credentials, arbitrary upstream requests, and generic proxy capabilities are
  never exposed by HTTP.
- Custom Python runs only in the locked-down container under
  `deploy/strategy-sandbox`.

API 文档分为三层：

- [当前 v1 完整接口手册](docs/API.md)：逐接口参数、响应、错误、权限和调用示例；
- [当前 v1 OpenAPI 3.1](docs/openapi-v1.json)：可导入 API 工具的机器可读合同；
- [完整多策略目标 API](docs/PLATFORM_API_TARGET.md)：尚未实现的 `/v2` 设计和验收边界，
  不与当前可调用接口混写。

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
.\.venv\Scripts\python -m scripts.issue_token `
  --principal ai-quant-market --scope market-data:read
.\.venv\Scripts\python -m scripts.issue_token `
  --principal ai-quant-execution --scope paper:write
.\.venv\Scripts\python -m scripts.serve_api --host 127.0.0.1 --port 8765
```

For the local AI-investment client, install the independent API as a per-user Windows
task and start it immediately:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_local_api_task.ps1
```

The installer prefers a restartable scheduled task. If Windows denies task registration,
it installs a current-user Startup shortcut instead. Both paths listen only on
`127.0.0.1:8765` and write logs under the ignored `runs/` directory. Internet-facing
deployment still requires HTTPS through a reverse proxy; never expose this plain-HTTP
listener beyond localhost.

For production, keep the Python server on a private interface behind an authenticated
TLS reverse proxy. Never bind it directly to the public internet.

The SIP owner reads `ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY` only in its process:

```powershell
.\.venv\Scripts\python -m scripts.run_sip_owner
```

No other service identity should receive those two variables.

## Market-data rights

Publishing this source code does not grant a right to redistribute Alpaca or exchange
market data. The `market-data:read` routes are intended for separately authorized,
owner-controlled services. Do not issue that scope to collaborators or expose those
routes as a public data feed. Alpaca states that its API data may not be redistributed;
obtain written permission or an appropriate enterprise agreement before any internal or
external redistribution. See Alpaca's
[redistribution notice](https://alpaca.markets/support/redistribute-alpaca-api) and your
current customer agreement.
