# ADR 0016: Web UI (React, TypeScript, Vite)

Status: Accepted (Phase 11).

- Stack: React 18, TypeScript (strict), Vite, TanStack Query, TanStack Virtual, React Flow.
- **Real data only**: every figure comes from the API. Mission Control is derived purely from the
  run's event stream (`deriveMission`, unit-tested); there is no static dashboard.
- Live updates use SSE with automatic reconnect and resume from the last seen `seq`
  (`Last-Event-ID` semantics via `after_seq`). Events are named by type, so the browser subscribes
  to each type; `tests/unit/test_web_contract.py` fails if `web/src/eventTypes.ts` drifts from
  `eios_domain.events.EventType`.
- Scalability: cursor/limit pagination and server-side filters; virtualised lists (constant DOM
  size); bounded client event window (`dropped` counts what the API still holds); the graph folds
  very wide spans and expands on demand.
- Same-origin: nginx serves the build and proxies `/api/` to the API (SSE-safe, no CORS).
  Dev: Vite proxies `/api`. Port bound to 127.0.0.1 only; container is read-only, cap-dropped.
- Views: Mission Control, Execution Graph, Projects, Knowledge, Capabilities, Workshop, Security,
  Observability. The admin token is typed into the page, kept in memory only and sent only as the
  `X-EIOS-Admin-Token` header for human actions (approvals, workshop verify/trust).
- Limits: no auth beyond loopback binding and the admin token; no editing of knowledge from the UI.
