# Threat model

Scope: Engineering Intelligence OS V1 running locally (Docker Compose), used by one owner with one
or more external reasoning models (MCP/HTTP clients). Primary assets: the owner's knowledge, the
company code that is indexed, and the integrity of the owner's *other* systems (above all external
databases).

## Trust boundaries
1. **LLM / MCP / HTTP client** - untrusted. May be wrong, manipulated by prompt injection from
   indexed code or documents, or malicious.
2. **Indexed repositories and documents** - untrusted data. May contain hostile git config, hooks,
   filters, symlinks, secrets, prompt injections.
3. **Plugins / generated artifacts / downloaded manifests** - untrusted until approved.
4. **Human owner** - trusted; acts through the admin token.
5. **Platform + its PostgreSQL** - trusted computing base.

## Threats and controls
| # | Threat | Controls (structural first) | Evidence |
|---|---|---|---|
| T1 | Model/tool reaches a company, QA or production database | No DB drivers or settings for foreign DBs (`database_policy`, `check_forbidden_deps`); forbidden capabilities rejected at manifest, DB CHECK and router; Policy Engine denies db actions, DB ports, DB client binaries (also indirect); sandbox has no network and refuses DB clients; secrets broker refuses DB credentials; workshop blocks the intent; MCP exposes no SQL | `tests/security/*`, `test_workshop`, `test_mcp` |
| T2 | Policy bypass / Root Policy tampering | Hash-pinned, frozen Root Policy, fail-closed load; compiled-in invariants survive a relocked file; no write API; `root_policy*` capability names forbidden | `test_policy_engine` |
| T3 | Sandbox escape / arbitrary execution | Registered, hash-pinned executables only; no shell; scrubbed env; rlimits; timeout + process-group kill; network namespace (fail closed); static scan of generated code; generated tools need human approval | `test_secrets_and_sandbox`, `test_workshop` |
| T4 | Malicious repository executes code during indexing | Hardened read-only git plumbing (no filters/fsmonitor/textconv/hooks), approved workspace roots, symlink and traversal checks | `test_git_hardening` |
| T5 | Path traversal / secret file reads | realpath-resolved scopes, denied path fragments, `.git` internals unwritable | `test_policy_engine` |
| T6 | Self-promotion of knowledge or capabilities | Trust needs independent evidence or a human; LLM evidence never verifies; generated/downloaded things are not routable until approved; TRUSTED is human-only; evaluation cannot reach TRUSTED | `test_governance`, `test_capability_registry` |
| T7 | Approval forgery / replay | Admin token (>= 12 chars, constant-time compare); approvals single-use, expiring, bound to action+target+actor; engine cannot grant | `test_policy_runtime` |
| T8 | Data exfiltration through export or remote LLM | Export is Default Vault only, encrypted (scrypt + AES-GCM), re-verified on import; project data to a remote model needs approval; no cloud knowledge storage | `test_portability`, `llm.call` rules |
| T9 | Supply chain (plugins, dependencies) | Manifests are data (`safe_load`, strict models); subprocess entrypoints pinned by SHA-256; immutable non-builtin versions; `uv.lock` frozen | `test_manifests` |
| T10 | Tampering with history | Append-only event and audit tables (DB triggers); retention is the only delete path; hash-chained audit export | `test_operations` |
| T11 | Denial of service / runaway agents | Execution budgets, bounded workflow steps and retries, output and size caps, bounded client memory, job leases with fencing | `test_workflow_engine`, `test_job_queue` |
| T12 | Prompt injection via retrieved content | Retrieved text is data in a pack with trust/health labels; it cannot invoke tools - every action still passes routing, policy, approvals | design + tests above |
| T13 | A web page in the owner's browser talks to the loopback services (DNS rebinding, cross-site form posts) | API: `TrustedHostMiddleware` with `api_allowed_hosts` (port ignored); MCP: Host and Origin allow-list (`localhost`, `127.0.0.1`, `[::1]`, `mcp`; any port); web UI: nginx answers 421 to any other Host and sends frame/MIME/referrer headers; CORS is closed by default and non-JSON bodies are rejected before any handler runs | `test_api_host_validation`, `test_mcp` |

## Residual risks (accepted, documented)
- The subprocess sandbox is not a VM; hostile native code needs a container/microVM sandbox
  (the `Sandbox` protocol allows one). In stock containers user namespaces may be unavailable, in
  which case network-isolated tools are refused rather than run.
- The loopback-bound ports and the admin token are the only authentication; do not expose the
  stack to other users without a reverse proxy and real authN/Z.
- The external-database intent detector is heuristic and advisory; the structural controls above
  are what actually prevent access. It covers gap resolution, workshop proposals, `/team/select`
  and workflow start (which are forced to critical risk); the MCP `request_specialist` tool does
  not screen goals yet.
- An approved `HumanApproval` is as strong as the admin token's secrecy.
- The content index (ADR 0018) stores the text of indexed documents, configuration and tests in the
  Engineering OS PostgreSQL. A secret committed to an ordinary tracked file is stored with it; only
  files that routinely carry credentials (`.env`, keys, state files) are skipped and URL
  credentials, and the values of credential-named assignments (`password = "..."`, `"api_key":
  "..."`, `Authorization: Bearer ...`), are masked. Skipped as text: `.env*`, keys, state files,
  `appsettings.<Env>.json`, `local.settings.json`, Spring `application-<profile>.*`, kubeconfig,
  `.htpasswd`, `.pgpass`, `.docker/config.json`. Masking is by key name, so a secret under a
  neutral name is still stored. Uncommitted text expires with the overlay TTL.
- The API and web UI have no authentication of their own. On a computer shared with other people
  any local user can reach them on loopback; set `EIOS_MCP_TOKEN` for MCP and do not use the system
  on a shared machine. The `/docs` (Swagger) page loads its script from a public CDN.
