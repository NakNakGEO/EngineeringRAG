# Hermes MCP client

Register Engineering OS as an MCP server in your Hermes client using its **streamable HTTP**
transport and the URL `http://127.0.0.1:8082/mcp` (add the bearer token header when
`EIOS_MCP_TOKEN` is set). The exact configuration keys depend on your Hermes client version; keep
that detail in your client's own configuration, not in Engineering OS.

For local models that cannot call MCP tools natively, use the HTTP API ([codex-http.md](codex-http.md))
from a thin wrapper; the contract in [README.md](README.md) is identical.
