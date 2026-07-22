# Cloud strategy platform engineering rules

1. This repository owns the single Alpaca SIP connection, raw event ledger, shared
   point-in-time feature library, strategy registry, and derived-signal service.
2. It must never import the AI investment or Broker execution repository.
3. Every strategy-owned record is keyed by `strategy_id`; cross-strategy reads fail
   closed.
4. Collaborator credentials may read authorized derived signals only.
5. AI-investment service credentials may read versioned point-in-time features but
   never raw SIP frames, Alpaca credentials, proxy endpoints, accounts, or orders.
6. There is no Broker adapter or order endpoint in this repository.
7. The platform is permanently long-only: no short signal action exists.
8. Custom Python executes only in a digest-pinned, networkless, read-only, non-root
   container with resource and time limits.
9. Stored timestamps are timezone-aware UTC. Missing features remain unavailable.
10. Changes are test-first and update `PROGRESS.md` with exact evidence.
