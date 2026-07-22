# Security policy

## Reporting a vulnerability

Do not open a public issue for vulnerabilities, exposed credentials, authorization
bypasses, order-safety defects, or market-data leakage. Use the repository's private
security-advisory reporting channel. If that channel is unavailable, contact the
repository owner privately before sharing technical details.

Never include real Alpaca credentials, bearer tokens, account identifiers, runtime
databases, positions, orders, or proprietary market-data samples in a report.

## Supported version

Security fixes are applied to the current `main` branch. This research platform does not
provide a Live Broker endpoint. Paper writes are disabled by default and require both a
`paper:write` token and the server-side write switch.
