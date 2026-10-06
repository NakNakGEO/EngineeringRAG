# Codex / custom HTTP client

If a client has no MCP support, call the HTTP API directly. The calls map one-to-one:

| MCP tool | HTTP |
|---|---|
| `bootstrap_project` | `POST /projects/bootstrap {"path": "...", "sync": "if_needed"}` |
| `get_context` | `POST /context/build {"text": "...", "project_id": "..."}` |
| `search_knowledge` | `POST /retrieval/search` |
| `request_capability` | `POST /capabilities/invoke` (route + policy + approvals + execute); `POST /capabilities/resolve` previews routing only |
| `request_specialist` | `POST /team/select` |
| `report_result` | `POST /workflows/{id}/report` |
| `get_run_state` | `GET /workflows/{id}/brief`, `GET /runs/{id}`, `GET /runs/{id}/events` |

```bash
curl -s localhost:8000/projects/bootstrap -H 'content-type: application/json' \
  -d '{"path": "/workspace/myrepo", "sync": "if_needed"}'
curl -s localhost:8000/context/build -H 'content-type: application/json' \
  -d '{"text": "What does PaymentGateway.charge rely on?", "project_id": "<id>"}'
```

Human-only endpoints (`POST /approvals/{id}/decision`) require `X-EIOS-Admin-Token`; never give
that token to a model.
