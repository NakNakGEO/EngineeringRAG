# Claude-compatible MCP clients

Any MCP client that supports **streamable HTTP** servers works. Client configuration formats
change between versions - check your client's documentation for the exact file and keys; the
server side is always the same URL and (optional) bearer token.

```json
{
  "mcpServers": {
    "engineering-os": {
      "type": "http",
      "url": "http://127.0.0.1:8082/mcp",
      "headers": { "Authorization": "Bearer ${EIOS_MCP_TOKEN}" }
    }
  }
}
```

Omit `headers` when `EIOS_MCP_TOKEN` is unset. Start the stack with `make up` first.

Suggested instruction for the model (project instructions / system prompt):

> Use the `engineering-os` MCP server. Call `bootstrap_project` then `get_context` before
> changing code; if `ready_to_act` is false follow `required_expansions`. Use
> `request_capability` for anything executable and never improvise tools. Report each workflow node
> with `report_result` and real evidence.
