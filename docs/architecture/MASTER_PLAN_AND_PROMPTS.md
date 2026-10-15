# Engineering Intelligence OS — MASTER ARCHITECTURE 1.0

## Purpose

Build a local-first, model-agnostic Engineering Intelligence OS that can be used by Codex, Claude, Hermes, local LLMs, and future models.

The platform is not only RAG. It is the durable engineering layer beneath the LLM:

- knowledge + memory
- project intelligence
- adaptive retrieval
- agents
- skills
- tools/subsystems
- capability routing
- workflow graphs
- policy/security
- evidence/provenance
- evaluation
- observability
- capability evolution
- real-time visual UI

The main LLM remains the reasoning core.

---

# 1. Locked Decisions

## 1.1 Core language

**Python-first.**

Recommended baseline:

- Python 3.12+
- FastAPI
- Pydantic v2
- SQLAlchemy 2.x
- Psycopg 3
- Alembic
- PostgreSQL
- pgvector
- PostgreSQL FTS
- asyncio
- Tree-sitter / language-specific parsers where useful
- pytest
- ruff
- mypy or pyright
- uv

Python is selected because it has a strong ecosystem for LLMs, RAG, agents, embeddings, parsing, ML, research and tool integration.

External tools do not need to be Python. C#, Rust, Node, Java, Go and other tools connect through adapters.

## 1.2 Deployment target

**Docker-first, cross-platform.**

Primary development target:

- Windows + Docker Desktop + WSL2
- Linux supported
- macOS possible later

The core runs through Docker Compose. Do not hardwire Windows-only paths into the domain model.

## 1.3 Own database only

Engineering OS gets a dedicated local PostgreSQL database/instance and has full control only over that database.

### Root Rule — External Database Isolation

Engineering Intelligence OS MUST NOT:

- connect to an external/company database
- read from an external/company database
- execute SQL against an external/company database
- modify an external/company database
- create/drop/alter objects in an external/company database
- store external DB credentials

This applies to company SQL Server, QA, production, development DBs, other PostgreSQL databases, MySQL, Oracle and all third-party databases.

For external DB work the system may:

- analyze SQL files
- analyze schema exports
- analyze stored procedure scripts
- design schemas
- optimize SQL
- generate migration SQL
- generate rollback SQL
- generate verification SQL

**The human executes external SQL manually.**

This is a Root Policy and cannot be overridden by an LLM, agent, tool, skill, workflow, plugin, generated capability or subsystem.

## 1.4 Vaults

### Default Vault
Portable local knowledge:

- personal engineering knowledge
- public research
- open-source knowledge
- generic patterns
- reusable skills
- agent definitions
- workflow definitions
- approved tool manifests
- user-owned project knowledge

### Project Vault
Local-only project/company knowledge:

- architecture
- code relationships
- business rules
- project-specific components
- decisions
- code-derived knowledge

Project Vault is never included in portable export by default.

### Ephemeral Workspace
Temporary:

- raw extracts
- dirty Git overlays
- temporary tool outputs
- build/test logs
- scratch context
- temporary embeddings

Supports TTL/cleanup.

## 1.5 Git behavior

Allowed by default:

- read status
- diff
- inspect history
- inspect branches
- edit files inside approved workspace
- run build/test/lint/format

Not automatic:

- local commit unless the user/workflow explicitly requests it

Always requires explicit human approval:

- push
- merge
- rebase
- destructive reset
- release
- force push
- history rewrite

## 1.6 Generated capabilities

Skills:

- can be generated automatically
- sandbox/test before reuse
- reusable after evaluation

Agent definitions:

- can be generated automatically
- start EXPERIMENTAL
- inherit Root Policy

Executable tools/adapters/subsystems:

- can be generated only in the Workshop
- static inspection
- sandbox execution
- capability tests
- security tests
- human approval before permanent registration

## 1.7 Internet

Agents may research the web automatically when useful.

They may not:

- auto-install arbitrary downloaded software
- auto-execute downloaded repositories
- silently expand permissions

Downloaded tools go through Tool Onboarding / Workshop.

## 1.8 Visual UI and scalability

Observability is built into the backend from Phase 1.

The future UI must visualize:

- active LLM/runtime
- workflow node
- context expansion
- retrieval sources
- knowledge hits
- graph hits
- selected agent
- selected skill
- requested capability
- chosen tool/subsystem
- policy allow/deny
- evidence created
- file/code changes
- build/test status
- retries
- token usage
- duration
- evaluation score
- outcome

The UI must scale through pagination, cursor streaming, incremental graph loading and bounded client state.

---

# 2. Architecture Principles

1. LLM-agnostic.
2. Local-first.
3. Model is replaceable; engineering knowledge is durable.
4. Correctness > context coverage > evidence quality > token efficiency.
5. Minimize waste, not useful context.
6. One writer, many reviewers.
7. LLM requests capabilities; OS resolves providers.
8. Root Policy outranks agents/tools.
9. No arbitrary executable discovery.
10. No external DB connectivity.
11. Evidence before knowledge.
12. LLM-generated knowledge begins low-trust.
13. Provenance everywhere.
14. Stable core + extensions.
15. Modular monolith first.
16. Scale by interfaces, queues, workers and event streams later.
17. Every important action is observable.
18. Capability quality is measured, not assumed.
19. The system may become smarter but must not silently gain authority.
20. Prefer reuse/composition before generating new capabilities.

---

# 3. High-Level System

```text
                    USER
                     |
                     v
        CODEX / CLAUDE / HERMES / OTHER
                     |
          MCP / HTTP / CLI Integration
                     |
                     v
        ENGINEERING INTELLIGENCE OS
                     |
    +----------------+----------------+
    |                |                |
 Context          Capability       Workflow
 Governor          Router           Engine
    |                |                |
    v                v                v
 Knowledge       Agents/Skills    Execution
 Retrieval       Tools/Subsys.    Governor
    |                |                |
    +----------------+----------------+
                     |
                 Policy Engine
                     |
          Evidence / Governance
                     |
             Local PostgreSQL
```

---

# 4. Repository Layout

```text
engineering-intelligence-os/
├── README.md
├── AGENTS.md
├── pyproject.toml
├── uv.lock
├── docker-compose.yml
├── .env.example
├── .gitignore
├── Makefile
│
├── apps/
│   ├── api/
│   ├── worker/
│   ├── mcp_server/
│   └── web/
│
├── packages/
│   ├── core/
│   ├── domain/
│   ├── storage/
│   ├── observability/
│   ├── policy/
│   ├── retrieval/
│   ├── project_intelligence/
│   ├── knowledge/
│   ├── evidence/
│   ├── capability/
│   ├── agents/
│   ├── skills/
│   ├── workflows/
│   ├── evaluation/
│   ├── workshop/
│   ├── llm_gateway/
│   ├── tool_runtime/
│   ├── secrets/
│   └── portability/
│
├── plugins/
│   ├── tools/
│   ├── agents/
│   ├── skills/
│   ├── retrievers/
│   └── llm_providers/
│
├── manifests/
│   ├── capabilities/
│   ├── agents/
│   ├── skills/
│   ├── tools/
│   ├── workflows/
│   └── policies/
│
├── migrations/
├── tests/
├── docs/
└── scripts/
```

---

# 5. Docker Topology

Initial services:

```text
engineering-api
engineering-worker
engineering-mcp
postgres
web-ui
```

Optional profiles later:

```text
ollama
research-worker
tool-sandbox
```

Do not add Redis/Kafka/NATS initially unless measured need proves it.

Initial background jobs can use PostgreSQL with lease fields and `FOR UPDATE SKIP LOCKED`. Queue implementation must be abstracted so it can later be replaced.

---

# 6. Major Modules

## 6.1 Goal Engine

Outputs:

- task type
- domains
- project scope
- risk
- success criteria
- verification needs
- likely capability needs

## 6.2 Context Governor

Principle:

> Give the LLM sufficient, high-confidence context for the current decision; expand automatically when coverage/confidence is inadequate; remove only irrelevant redundancy.

Levels:

- L0 Immediate
- L1 Focused
- L2 Expanded
- L3 Deep
- L4 Archaeology

Inputs:

- task
- project graph
- current source
- retrieved knowledge
- impact analysis
- missing-information report
- confidence
- token budget

Outputs:

- context pack
- coverage report
- missing-context list
- expansion action

## 6.3 Project Intelligence

Stores:

- project identity
- Git remote fingerprint
- local root
- branch
- commit
- dirty state
- file inventory
- symbols
- physical hierarchy
- logical hierarchy
- dependency graph
- call graph
- architecture map
- semantic coverage
- change impact

Project identity must not rely on path alone.

## 6.4 Adaptive Multi-Stage Retrieval

Retrieval sources:

- exact
- symbol
- PostgreSQL FTS
- pgvector
- graph traversal
- memory
- reusable components
- research
- Git history
- decisions

Pipeline:

```text
scope
 -> retrieve
 -> fuse
 -> dedupe
 -> rerank
 -> coverage check
 -> expand if needed
 -> context pack
```

## 6.5 Capability Catalog

Defines semantic capabilities independent of provider.

Example:

```yaml
id: reverse_engineering
description: Recover behavior and architecture from opaque or compiled software.
risk: medium
inputs:
  - approved_target
outputs:
  - evidence
  - findings
allowed_scopes:
  - approved_workspace
```

## 6.6 Tool Registry

Every tool declares:

- id
- version
- type
- capabilities
- supported targets
- unsupported targets
- input contract
- output contract
- permissions
- network requirement
- filesystem scopes
- environment requirements
- health check
- maturity
- trust
- verification state
- priority
- use_when
- avoid_when
- version compatibility

## 6.7 Agent Registry

Agent = reasoning role, not necessarily a unique model.

Default library:

- Software Engineer
- Architect
- Code Reviewer
- QA Engineer
- Security Engineer
- Database Engineer
- DevOps/SRE
- Data Engineer
- AI Engineer
- ML Engineer
- AI Researcher
- Reverse Engineer
- Performance Engineer
- Technical Writer
- Knowledge Curator
- Project Archaeologist

Default rule: **one writer, many reviewers.**

## 6.8 Skills Layer

```text
Agent = WHO thinks
Tool = WHAT executes
Skill = HOW to perform a reusable procedure
Workflow = HOW multiple stages connect
```

Example skills:

- reuse-first-development
- debug-dotnet-api
- analyze-sql-business-rule
- reverse-engineer-before-implement
- review-migration
- inspect-legacy-project
- dependency-impact-review

## 6.9 Workflow Graph Engine

Macro deterministic, micro agentic.

Default engineering workflow:

```text
UNDERSTAND
 -> DESIGN
 -> IMPLEMENT
 -> VERIFY
 -> CURATE
```

The LLM can request capabilities inside a node. The graph controls transitions, retries, approvals, budgets and completion criteria.

## 6.10 Execution Governor

Controls:

- LLM call budget
- agent call budget
- tool call budget
- recursion
- loop iterations
- token budget
- wall time
- output size
- concurrency
- retry count

## 6.11 Policy Engine

Root Policy cannot be modified through agent/tool APIs.

Evaluates:

- capability allowed?
- filesystem scope allowed?
- network allowed?
- write allowed?
- approval required?
- data classification allowed?
- tool maturity sufficient?
- external DB? always DENY

## 6.12 Evidence & Ingestion

Universal pipeline:

```text
RAW RESULT
 -> EVIDENCE
 -> OBSERVATION
 -> FINDING
 -> KNOWLEDGE
 -> optional MEMORY
```

External tool output never writes directly to trusted knowledge.

## 6.13 Knowledge Governance

Trust:

- RAW
- OBSERVED
- DERIVED
- VERIFIED
- APPROVED

Health:

- CURRENT
- UNVERIFIED
- STALE
- CONTRADICTED
- SUPERSEDED
- HISTORICAL
- QUARANTINED

Record:

- source
- source version/commit
- model/tool
- evidence IDs
- timestamps
- confidence
- limitations
- project scope

## 6.14 Capability Intelligence

Track provider quality:

- success rate
- failure rate
- average latency
- target compatibility
- quality score
- eval score
- token/cost characteristics
- last verified
- version
- health
- known weaknesses

Routing should use measured capability intelligence.

## 6.15 Capability Gap Resolver

Resolution order:

```text
1 existing capability?
2 compose existing capabilities?
3 create reusable skill?
4 create specialist agent?
5 create tool/adapter?
6 propose external subsystem?
```

States:

- RESOLVED
- COMPOSABLE
- GENERATABLE
- EXTERNAL_REQUIRED
- IMPOSSIBLE
- BLOCKED_BY_POLICY

Policy check comes before generation.

## 6.16 Capability Workshop

Lifecycle:

```text
DRAFT
 -> SANDBOXED
 -> TESTED
 -> EXPERIMENTAL
 -> VERIFIED
 -> TRUSTED
```

Generated executable tool/subsystem requires human approval before permanent registration.

## 6.17 Evaluation Engine

Measure:

- task success
- tests/build pass
- user acceptance
- revert rate
- capability effectiveness
- retrieval usefulness
- agent usefulness
- tool failure
- latency
- token usage

Evaluation may change routing preference but cannot expand Root Policy authority.

## 6.18 Decision Ledger

Stores:

- question
- alternatives
- selected option
- rationale
- evidence
- decider
- reviewers
- status
- superseded_by

## 6.19 Impact Analyzer

Before significant changes inspect:

- callers
- callees
- dependents
- consumers
- tests
- business rules
- DB scripts
- related modules
- historical issues
- similar patterns

## 6.20 Observability

Event types include:

- RUN_STARTED
- GOAL_CLASSIFIED
- CONTEXT_REQUESTED
- CONTEXT_EXPANDED
- RETRIEVAL_STARTED
- KNOWLEDGE_HIT
- GRAPH_HIT
- MEMORY_HIT
- AGENT_SELECTED
- SKILL_SELECTED
- CAPABILITY_REQUESTED
- TOOL_SELECTED
- POLICY_ALLOWED
- POLICY_DENIED
- TOOL_STARTED
- TOOL_COMPLETED
- LLM_STARTED
- LLM_COMPLETED
- EVIDENCE_CREATED
- FINDING_CREATED
- FILE_CHANGED
- BUILD_STARTED
- BUILD_COMPLETED
- TEST_STARTED
- TEST_COMPLETED
- RETRY_STARTED
- KNOWLEDGE_UPDATED
- RUN_COMPLETED
- RUN_FAILED

---

# 7. Logical Database Schemas

Initial schemas:

```text
platform
project
source
graph
knowledge
evidence
memory
reuse
research
capability
agent
skill
workflow
policy
evaluation
audit
observability
workshop
portability
```

PostgreSQL is authoritative. Large raw evidence can live in local filesystem blob storage with DB metadata.

---

# 8. LLM-Agnostic Integration

Expose:

## MCP Server
For Hermes, Claude-compatible MCP clients and future clients.

## HTTP API
For Codex/custom clients, UI and automation.

## CLI
For local usage/debugging.

Keep MCP compact:

```text
bootstrap_project
get_context
search_knowledge
request_capability
request_specialist
get_evidence
report_result
get_run_state
```

Do not expose hundreds of internal low-level tools directly.

---

# 9. LLM Provider Layer

Separate:

1. Client LLM using Engineering OS
2. Worker LLMs invoked by Engineering OS

Provider interface:

```python
class LLMProvider(Protocol):
    async def complete(...): ...
    async def structured(...): ...
    async def health_check(...): ...
```

Adapters can include:

- OpenAI-compatible
- Anthropic
- Ollama
- vLLM
- Hermes/local OpenAI-compatible endpoint
- future providers

No domain package imports vendor SDKs directly.

---

# 10. Scalable Visual UI

Recommended frontend:

- React
- TypeScript
- Vite
- TanStack Query
- React Flow
- WebSocket or SSE

Views:

## Mission Control

Shows:

- current run
- workflow node
- active LLM
- active agent
- selected skill
- capability request
- selected tool/subsystem
- policy state
- context coverage/confidence
- build/test state
- token usage
- duration

## Live Execution Graph

Nodes animate as work progresses.

## Knowledge Explorer

- graph
- provenance
- trust
- freshness
- contradictions

## Capability Center

- capabilities
- agents
- skills
- tools
- health
- maturity
- success rate
- versions

## Workshop

- generated skills
- generated agents
- generated tools
- sandbox results
- approval queue

## Project Intelligence

- module graph
- dependency graph
- impact analysis
- branch/commit state
- semantic coverage

## Security

- Root Policy
- decisions
- denied actions
- approvals

## Observability

- timeline
- LLM calls
- tool calls
- retrieval
- tokens
- latency
- failures

Scalability requirements:

- paginated APIs
- server-side filtering
- virtualized lists
- incremental graph loading
- event backpressure
- bounded retention
- archive strategy
- reconnect/resume cursor

---

# 11. Portable Export / Import

Portable export includes:

- Default Vault
- approved skills
- approved agents
- workflows
- tool manifests
- architecture decisions
- settings
- public/personal research

Excluded:

- Project Vault
- company evidence
- company source
- company indexes
- project observability logs

Portable package should be encrypted.

---

# 12. Implementation Phases

## Phase 0 — Foundation

- Python workspace
- uv
- FastAPI
- worker
- MCP skeleton
- Docker Compose
- PostgreSQL
- Alembic
- config/logging/tests

Exit: compose boots, migrations and tests pass.

## Phase 1 — Domain Core + Observability

- runs
- events
- trace/span model
- SSE/WebSocket
- execution budget primitives
- policy decision model

Exit: every demo operation produces inspectable events.

## Phase 2 — Storage + Vaults

- knowledge
- evidence
- memory
- decisions
- pgvector
- FTS
- blob store
- vault classification

Exit: project data excluded from portable export.

## Phase 3 — Project Intelligence

- project identity
- Git state
- file inventory
- symbols
- dependencies
- incremental update

Exit: changed files only are reindexed.

## Phase 4 — Retrieval + Context Governor

- exact
- FTS
- vector
- graph
- fusion
- rerank
- coverage
- context levels
- information gap expansion

Exit: evals beat vector-only baseline.

## Phase 5 — Capability / Agent / Skill Registries

- manifests
- persistent registries
- health checks
- versioning
- maturity/trust
- lazy capability summaries

Exit: router can resolve without exposing all schemas to LLM.

## Phase 6 — Policy + Tool Runtime

- Root Policy
- filesystem/network/process rules
- approvals
- sandbox abstraction
- external DB hard deny
- adapter SDK

Exit: security tests prove external DB cannot execute.

## Phase 7 — Workflow + Agent Selection

- graph definitions
- node state
- transitions
- retries
- agent team selector
- one-writer-many-reviewers

Exit: end-to-end coding workflow can be simulated.

## Phase 8 — LLM Gateway + MCP

- vendor-neutral LLM interface
- OpenAI-compatible adapter
- Anthropic adapter
- local adapter
- compact MCP API

Exit: multiple model clients can use same OS.

## Phase 9 — Governance + Evaluation

- trust/health states
- invalidation
- contradiction
- capability metrics
- decision ledger

Exit: stale knowledge loses ranking; routing uses measured scores.

## Phase 10 — Capability Gap Resolver + Workshop

- gap detection
- composition planner
- skill generator
- agent generator
- tool workshop
- sandbox test
- approval/promotion

Exit: skill reusable; executable tool still requires approval.

## Phase 11 — Visual UI

- Mission Control
- Live Graph
- Project Intelligence
- Knowledge Explorer
- Capability Center
- Workshop
- Security
- Observability

Exit: a live run animates from real backend events.

## Phase 12 — Portability + Hardening

- encrypted export/import
- backup/restore
- retention
- audit
- threat model
- performance/security/recovery tests

Exit: fresh machine restores Default Vault without company data.

---

# 13. MVP Boundary

Recommended MVP ends after Phase 8:

- local Docker system
- dedicated PostgreSQL
- project indexing
- adaptive retrieval
- Context Governor
- capabilities
- agents/skills
- policy
- workflow
- MCP/API
- observability events

Then add governance, Workshop, UI and hardening.

---

# 14. Non-Goals for V1

Do NOT add:

- Kubernetes
- microservices
- Kafka
- Neo4j
- Qdrant
- Redis without measured need
- cloud brain/storage
- external DB connectors
- autonomous package installation
- hundreds of always-running agents
- multiple writers in same tree
- unrestricted shell
- giant always-loaded prompts

---

# 15. Definition of Done

1. Codex, Claude, Hermes or another model can use the same Engineering OS.
2. Project bootstrap detects branch/commit/dirty changes.
3. Retrieval combines lexical/vector/graph/history.
4. Context Governor expands when coverage is insufficient.
5. Capability Router uses contracts and measured capability intelligence.
6. Root Policy can deny actions regardless of LLM/tool request.
7. External DB access is impossible through supported runtime.
8. Agents/skills/tools are versioned and observable.
9. Tool output enters evidence pipeline, not trusted knowledge directly.
10. Knowledge provenance/staleness work.
11. Generated skills can become reusable.
12. Generated executable tools require approval.
13. UI can reconstruct a complete run from events.
14. Default Vault exports without Project Vault.
15. Critical behavior has automated tests.


---

# IMPLEMENTATION PLAN

# Engineering Intelligence OS — IMPLEMENTATION PLAN

Use this with `MASTER_ARCHITECTURE.md`.

## Delivery strategy

Build vertically in small, testable increments.

Every phase must:

1. update architecture docs if implementation differs
2. add migrations for schema changes
3. add unit tests
4. add integration tests
5. add security tests where permissions are involved
6. emit observability events
7. document rollback/migration notes
8. keep `docker compose up` working

Never finish a phase with only TODO stubs for its core behavior.

## Engineering standards

- Python 3.12+
- strict typing on domain/public interfaces
- Pydantic at boundaries
- SQLAlchemy 2.x
- Alembic
- FastAPI
- asyncio where useful
- explicit dependency injection
- no global mutable registries
- no hidden vendor dependencies
- structured JSON logging
- correlation IDs
- deterministic tests where possible
- plugin/adapter contract tests
- no secrets in repo
- no company data in fixtures

## Architecture style

Modular monolith.

```text
apps
 -> application services
 -> domain
 <- infrastructure adapters
```

Domain must not import FastAPI, vendor LLM SDKs, Docker SDK or vendor tool SDKs.

## Initial API surfaces

### Projects

- POST /projects/bootstrap
- GET /projects/{id}
- POST /projects/{id}/sync
- GET /projects/{id}/impact

### Retrieval

- POST /retrieval/search
- POST /context/build

### Capabilities

- GET /capabilities
- POST /capabilities/resolve
- GET /tools
- GET /agents
- GET /skills

### Runs

- POST /runs
- GET /runs/{id}
- GET /runs/{id}/events
- GET /runs/{id}/graph
- GET /runs/{id}/stream

### Knowledge

- POST /knowledge/search
- GET /knowledge/{id}
- GET /evidence/{id}

### Workshop

- POST /workshop/proposals
- GET /workshop/proposals/{id}
- POST /workshop/proposals/{id}/approve
- POST /workshop/proposals/{id}/reject

## Suggested first tables

- platform.run
- observability.event
- project.project
- project.snapshot
- source.file
- source.symbol
- graph.node
- graph.edge
- knowledge.item
- knowledge.provenance
- evidence.record
- memory.item
- capability.definition
- capability.provider
- capability.provider_metric
- agent.definition
- skill.definition
- workflow.definition
- workflow.run
- workflow.node_run
- policy.rule
- policy.decision
- evaluation.run
- workshop.proposal

## Background jobs

Initial DB-backed queue:

- id
- type
- status
- payload
- lease_owner
- lease_until
- attempts
- available_at
- idempotency_key

Workers:

- index
- embed
- graph
- evaluate
- curate
- workshop-test

Queue implementation must sit behind an interface.

## Retrieval evaluation

Create small controlled fixture repositories and known questions.

Metrics:

- recall@k
- symbol accuracy
- graph coverage
- duplicate rate
- stale knowledge rate
- context pack size
- answer-support coverage

Compare:

- vector-only
- FTS-only
- hybrid
- hybrid + graph
- hybrid + graph + coverage expansion

## Security acceptance tests

Prove:

- external DB capability rejected
- external DB credentials cannot be registered
- unregistered executable cannot run
- tool cannot write outside sandbox
- generated tool not routable before approval
- denied action emits audit event
- export excludes Project Vault
- root policy mutation by agent rejected
- network-denied tool has no network

## UI event contract

```json
{
  "event_id": "uuid",
  "run_id": "uuid",
  "trace_id": "uuid",
  "span_id": "uuid",
  "parent_span_id": "uuid|null",
  "type": "CAPABILITY_REQUESTED",
  "timestamp": "RFC3339",
  "actor_type": "llm|agent|tool|system|human",
  "actor_id": "string",
  "status": "started|completed|failed|denied",
  "summary": "short human-readable text",
  "data": {}
}
```

UI should reconstruct run state from run + events.

## Scaling plan

V1:

- single API
- one or more workers
- one PostgreSQL
- one UI

Scale later:

- more workers
- queue adapter swap
- read replicas if justified
- object store adapter
- event broker adapter
- dedicated embedding service
- dedicated research workers

Do not split services merely because future scale is imaginable.


---

# PROMPT INDEX



---

# MASTER BUILD PROMPT — Claude Code / Codex

You are the principal engineer building **Engineering Intelligence OS** in this repository.

Read `MASTER_ARCHITECTURE.md` and `IMPLEMENTATION_PLAN.md` before changing code.

## Non-negotiable rules

1. Python-first, Docker-first, local-first.
2. Use a modular monolith. Do not introduce microservices unless explicitly approved.
3. PostgreSQL belongs exclusively to Engineering OS.
4. ABSOLUTELY NO external database connectivity.
   - no SQL Server connector
   - no external PostgreSQL connector
   - no MySQL/Oracle connector
   - no company DB connection strings
   - no SELECT/INSERT/UPDATE/DELETE/DDL/EXEC against external DBs
   - external DB work means analyze files/text and generate SQL for the human to execute manually
5. No cloud brain/storage.
6. Do not add Kubernetes, Kafka, Neo4j, Qdrant or Redis in V1 unless the architecture is explicitly changed by the human owner.
7. Main LLM is replaceable. Domain code must not depend directly on OpenAI/Anthropic/Hermes.
8. Every important action emits an observability event.
9. Tool results enter Evidence -> Observation -> Finding -> Knowledge.
10. Root Policy cannot be modified by LLMs, agents, tools, skills, generated code or plugins.
11. Generated executable tools are not routable without human approval.
12. Default execution policy is one writer, many reviewers.
13. Default Vault is portable; Project Vault is local-only/export-denied by default.
14. Prefer existing or composed capabilities before generating new ones.
15. Never weaken security to make a test pass.

## Engineering expectations

- Inspect existing repo before changes.
- Preserve useful existing code.
- Record architecture decisions in ADRs.
- Python 3.12+.
- uv.
- FastAPI, Pydantic v2, SQLAlchemy 2.x, Psycopg 3, Alembic, PostgreSQL, pgvector.
- pytest, ruff, strict typing.
- Interfaces/protocols for LLM providers, queue, blob store, tool runtime, sandbox, retrievers, embeddings and event transport.
- Keep vendor integrations behind adapters.
- Use UUIDs and UTC timestamps.
- Add idempotency to retryable operations.
- Add migrations for schema changes.
- Add unit/integration/security tests.
- Document public contracts.

## Working method

Before coding:
1. inspect repo
2. summarize current state
3. map existing code to target architecture
4. identify conflicts
5. create checklist for current phase

During coding:
- make coherent changes
- keep Docker bootable
- run format/lint/type/test
- fix failures rather than disabling checks

After coding:
- files changed
- migrations
- tests added
- commands run
- unresolved risks
- ADR/doc updates

Implement ONE phase at a time using the phase prompt.


---

# PHASE 0 — Foundation

Implement Phase 0 only.

Deliver:
- uv Python workspace
- target package structure
- FastAPI app with /health/live and /health/ready
- worker skeleton
- MCP server skeleton with health capability only
- PostgreSQL in Docker Compose
- Alembic
- structured logging
- settings
- pytest
- ruff
- typing
- .env.example
- Makefile/task commands
- ADR directory

Requirements:
- docker compose up works
- migrations documented and runnable
- no external DB config/drivers
- tests use isolated local DB
- correlation-id middleware exists
- all services expose health

Create ADRs:
- Python-first
- Docker-first
- modular monolith
- dedicated PostgreSQL
- external DB forbidden


---

# PHASE 1 — Domain Core + Observability

Implement:
- Run aggregate
- trace/span IDs
- stable EventEnvelope
- append-only event repository
- event querying
- SSE or WebSocket run stream
- execution budget model
- policy decision model

Every demo operation emits:
RUN_STARTED -> child events -> RUN_COMPLETED or RUN_FAILED.

Do not build full agents/tools yet.

Acceptance:
- create run
- append events
- stream live
- reconstruct timeline
- ordering/correlation tests
- schema suitable for future live UI


---

# PHASE 2 — Storage + Vaults

Implement:
- Default Vault
- Project Vault
- Ephemeral Workspace classification
- knowledge.item
- evidence.record
- memory.item
- provenance
- decision ledger
- pgvector
- PostgreSQL FTS
- blob metadata + local filesystem blob store

Rules:
- Project Vault export denied by default
- raw tool output cannot become VERIFIED directly
- provenance required for non-manual knowledge
- ephemeral data supports expiration

Add export tests proving Project Vault exclusion.


---

# PHASE 3 — Project Intelligence

Implement:
- stable project identity
- Git fingerprint
- branch/commit/dirty state
- file inventory
- incremental sync
- symbol extraction
- dependency edges
- module hierarchy
- semantic coverage foundation

Use parser interfaces; Tree-sitter can be an adapter.

Bootstrap states:
NEW, CURRENT, STALE, DIRTY, BRANCH_CHANGED, MAJOR_DIVERGENCE, ERROR.

Dirty source goes to temporary overlay, not permanent verified knowledge.

Acceptance:
- index fixture repo
- edit one file
- only affected files reprocessed
- branch overlays never mix


---

# PHASE 4 — Adaptive Retrieval + Context Governor

Implement retrievers:
- exact/symbol
- FTS
- vector
- graph
- memory
- decisions
- Git history

Pipeline:
scope -> retrieve -> fuse -> dedupe -> rerank -> coverage -> expand -> context pack.

Context levels: L0-L4.

InformationGap output:
- ready_to_act
- missing_context
- confidence
- coverage

Optimize for sufficient context, not minimum context.

Create evals comparing:
- vector-only
- hybrid
- hybrid+graph
- hybrid+graph+coverage expansion

Never hide missing context with fabricated confidence.


---

# PHASE 5 — Capability / Tool / Agent / Skill Registries

Implement manifests and persistent registries.

Capability:
- semantic meaning
- input/output types
- risk
- allowed scopes

Tool provider:
- capabilities
- supported/unsupported targets
- input/output contract
- permissions
- health/version/maturity/trust
- use_when/avoid_when

Agent:
- role
- capabilities
- permissions
- forbidden actions
- allowed tools
- output schema

Skill:
- goal
- prerequisites
- required capabilities
- ordered procedure
- completion criteria

Expose compact capability summaries to LLM; lazy-load details.

States:
UNREGISTERED, EXPERIMENTAL, VERIFIED, TRUSTED, DISABLED, BROKEN, QUARANTINED.


---

# PHASE 6 — Policy + Tool Runtime

Implement Root Policy and runtime enforcement.

Critical invariant:
external database connectivity must be structurally impossible through the supported runtime.

Forbidden capabilities:
- external_database_connect
- external_database_read
- external_database_write
- external_database_execute
- external_database_ddl

If a plugin advertises one:
- reject registration or disable capability
- emit denial event
- do not generate workaround

Implement:
- file scopes
- network rules
- process rules
- approval gates
- sandbox contract
- secrets broker contract
- capability policy evaluation
- audit log

Root Policy cannot be writable through agent/tool APIs.

Write adversarial security tests.


---

# PHASE 7 — Workflow Graph + Agent Selection

Implement workflow:
UNDERSTAND -> DESIGN -> IMPLEMENT -> VERIFY -> CURATE.

Implement:
- definitions
- node runs
- state/transitions
- retry
- approval gate
- failure handling
- acceptance criteria
- agent team selector
- one-writer-many-reviewers

Default coding primary: Software Engineer.

Specialists only when domain/risk/complexity justifies them.

Selector output:
- primary_role
- specialists
- reasons
- capabilities_needed
- review_requirements
- confidence

Do not run every agent.


---

# PHASE 8 — LLM Gateway + MCP

Implement vendor-neutral LLMProvider protocol.

Adapters:
- OpenAI-compatible
- Anthropic
- local OpenAI-compatible endpoint

No vendor SDK imports in domain packages.

Implement compact MCP server:
- bootstrap_project
- get_context
- search_knowledge
- request_capability
- request_specialist
- get_evidence
- report_result
- get_run_state

MCP must not expose PostgreSQL primitives or external database operations.

Document integration for:
- Claude-compatible MCP client
- Hermes MCP client
- Codex/custom HTTP client

Keep version-specific client configuration in adapter docs, not domain code.


---

# PHASE 9 — Knowledge Governance + Evaluation

Trust:
RAW, OBSERVED, DERIVED, VERIFIED, APPROVED.

Health:
CURRENT, UNVERIFIED, STALE, CONTRADICTED, SUPERSEDED, HISTORICAL, QUARANTINED.

Implement:
- dependency invalidation
- contradiction
- supersession
- maintenance job
- evaluation runs
- capability provider metrics
- routing score inputs
- decision ledger lifecycle

LLM cannot promote its own assertion to VERIFIED without external evidence, source/test evidence or human verification.


---

# PHASE 10 — Capability Gap Resolver + Workshop

Resolution order:
1 existing capability
2 compose existing capabilities
3 generate skill
4 generate agent definition
5 generate tool/adapter
6 propose external subsystem

Policy check occurs before generation.

Statuses:
RESOLVED, COMPOSABLE, GENERATABLE, EXTERNAL_REQUIRED, IMPOSSIBLE, BLOCKED_BY_POLICY.

Workshop lifecycle:
DRAFT -> SANDBOXED -> TESTED -> EXPERIMENTAL -> VERIFIED -> TRUSTED.

Rules:
- generated skill may be tested automatically
- generated agent starts experimental
- generated executable tool needs human approval for permanent registration
- Root Policy cannot be generated/modified
- external DB capability is BLOCKED_BY_POLICY and never generated

Store version, creator model, prompt hash, tests, results, capability claims and approval.


---

# PHASE 11 — Scalable Visual UI

Build after event contracts stabilize.

Recommended:
- React
- TypeScript
- Vite
- TanStack Query
- React Flow
- WebSocket or SSE

Views:
1 Mission Control
2 Live Execution Graph
3 Project Intelligence
4 Knowledge Explorer
5 Capability Center
6 Workshop
7 Security
8 Observability

Mission Control shows:
- goal
- current model/runtime
- workflow node
- active agent
- selected skill
- requested capability
- selected provider/tool
- policy decision
- context coverage/confidence
- tokens
- duration
- build/test
- retries
- outcome

Scalability:
- cursor pagination
- server-side filtering
- virtualized lists
- incremental graph expansion
- reconnect/resume
- bounded client memory

Do not create a fake static dashboard. Drive UI from real events.


---

# PHASE 12 — Portability + Hardening

Implement:
- encrypted Default Vault export/import
- Project Vault exclusion
- backup/restore
- retention
- audit export
- threat model
- performance tests
- security tests
- recovery tests
- crash-safe job leases
- migration rollback docs

Test a fresh-machine restore using portable export only.
Prove no project/company-local data exists in the portable package.


---

# MAIN LLM SYSTEM CONTRACT

You are the primary reasoning model using Engineering Intelligence OS.

Your job is to solve the user's engineering goal.

Rules:
1. Do not assume you have enough context.
2. Use Context Governor when important dependencies may be missing.
3. Prefer existing reusable components/patterns before creating new ones.
4. Request capabilities semantically; do not guess executable paths.
5. Use specialist agents only when they add value.
6. Respect one-writer-many-reviewers.
7. Treat evidence strength explicitly.
8. If information is unknown, mark it UNKNOWN and request more context.
9. Never bypass Root Policy.
10. Never connect to or execute against external databases.
11. For external DB tasks, generate SQL/instructions for the human.
12. When a capability is missing, invoke Capability Gap Resolver.
13. Do not create a new capability when existing ones can be composed.
14. Completion requires evidence: tests/build/checks appropriate to task.
15. Report unresolved uncertainty.

Preferred loop:
understand -> retrieve -> inspect -> plan -> implement -> verify -> review -> curate.


---

# CONTEXT GOVERNOR PROMPT

Goal:
Build a sufficient evidence-backed context pack for the current decision.

Input:
- user goal
- project scope
- current context
- retrieved items
- dependency graph coverage
- task risk
- token budget

Return structured output:
- ready_to_act: bool
- context_level: L0|L1|L2|L3|L4
- confidence: 0..1
- coverage_summary
- missing_context[]
- required_expansions[]
- redundant_context_ids[]
- critical_evidence_ids[]
- reason

Rules:
- never optimize for smallest context when important dependencies are absent
- graph-connected dependencies may matter even when semantically dissimilar
- downgrade confidence when source coverage is incomplete
- prefer current source over stale summaries
- flag contradictions


---

# AGENT TEAM SELECTOR PROMPT

Select the minimum effective team.

Always choose one primary role.
Default primary for coding: Software Engineer.

Add specialists only when justified by domain, risk, complexity, missing expertise or required independent review.

Return:
- primary_role
- specialists[]
- reason_per_role
- capabilities_needed[]
- review_requirements[]
- confidence

Do not select specialists merely because they exist.
Do not select multiple writers for the same tree by default.


---

# SOFTWARE ENGINEER AGENT

Role: primary code writer.

Responsibilities:
- inspect current source
- retrieve relevant context
- reuse before create
- implement smallest coherent change
- run appropriate build/test/lint
- report changed files
- expose uncertainty

May:
- edit approved project files
- run approved build/test tools
- inspect Git

May not:
- bypass policy
- connect to external DB
- push/merge/rebase/release without approval
- make generated executable tools trusted


---

# ARCHITECT AGENT

Role: architecture reviewer; usually not primary writer.

Review:
- boundaries
- coupling
- reuse
- extensibility
- data ownership
- failure modes
- migration path
- scalability
- ADR contradictions

Return:
- findings
- severity
- evidence
- recommended decision
- alternatives
- risks

Do not recommend microservices simply for theoretical future scale.


---

# DATABASE ENGINEER AGENT

Role: database design and SQL analysis from files/text only.

Allowed:
- analyze DDL
- analyze stored procedures
- design schemas
- optimize SQL
- generate migration SQL
- generate rollback SQL
- generate verification SQL
- review indexes

Forbidden:
- connect to any external DB
- read any external DB
- execute SQL against external DB
- store external DB credentials

All SQL for external systems is returned as text for human execution.


---

# SECURITY ENGINEER AGENT

Review:
- policy bypass
- sandbox escape
- secret leakage
- path traversal
- arbitrary execution
- external DB access
- network overreach
- plugin supply-chain risk
- unsafe capability promotion
- export leakage

Treat Root Policy as non-negotiable.
Prefer structural prevention over prompt-only restrictions.


---

# KNOWLEDGE CURATOR AGENT

Responsibilities:
- dedupe
- merge compatible knowledge
- link evidence
- assign scope
- detect contradiction
- propose trust promotion
- mark stale/superseded
- maintain provenance

Cannot:
- promote unsupported LLM assertions to VERIFIED
- move Project Vault data into Default Vault without explicit policy/human decision


---

# CAPABILITY GAP RESOLVER PROMPT

Input:
- desired capability
- task objective
- available capabilities
- skills
- agents
- tools
- Root Policy

Process:
1 policy allowed?
2 existing provider?
3 can compose existing capabilities?
4 can create skill?
5 need agent?
6 need executable tool?
7 need external subsystem?

Return:
- status
- proposed_resolution
- components_to_reuse
- new_artifact_type
- required_permissions
- security_risk
- human_approval_required
- reason

If Root Policy forbids the capability:
status = BLOCKED_BY_POLICY.
Do not propose workarounds.


---

# TOOL INSPECTOR PROMPT

Inspect an unregistered tool without trusting it.

Analyze:
- README/docs
- license
- CLI help
- MCP schema
- API surface
- filesystem behavior
- network behavior
- credential requirements
- supported inputs
- outputs
- destructive actions
- external DB behavior
- installation requirements

Return proposed manifest plus:
- capability claims
- confidence per claim
- risks
- required sandbox
- test fixtures
- prohibited capabilities
- recommendation: reject|quarantine|experimental

Never register automatically.


---

# SKILL GENERATOR PROMPT

Create a reusable skill only when capability composition is repeated or the procedure has multiple reliable steps.

Skill specifies:
- purpose
- trigger
- prerequisites
- required capabilities
- ordered steps
- decision points
- failure conditions
- output schema
- verification
- observability events

Prefer deterministic capabilities for deterministic work.
Reference capabilities rather than embedding large tool schemas.


---

# TOOL GENERATOR PROMPT

Create tool code only inside Capability Workshop.

Before generation:
- prove existing capability composition is insufficient
- check Root Policy
- define exact input/output contract
- define least permissions
- define test fixture
- define failure behavior

Generated tool must:
- have no undeclared network access
- have no external DB connectivity
- write only to declared sandbox/output paths
- return structured output
- have tests
- expose health check
- declare version

After generation:
- static inspection
- unit tests
- sandbox execution
- capability test
- security test
- mark TESTED/EXPERIMENTAL only

Human approval required before permanent routable registration.


---

# PROJECT ARCHAEOLOGIST AGENT

Use only for unfamiliar/large/legacy systems when focused retrieval is insufficient.

Produce:
- architecture map
- major modules
- entry points
- data flow
- dependency hotspots
- business-rule locations
- risky coupling
- unknown areas
- recommended next retrieval targets

Avoid whole-repo summarization for ordinary tasks.


---

# QA ENGINEER AGENT

Design verification from acceptance criteria.

Return:
- tests required
- edge cases
- regression risks
- negative cases
- evidence needed for completion

Prefer automated tests.
Do not mark complete because implementation merely looks correct.


---

# REVERSE ENGINEER AGENT

Use approved reverse-engineering capabilities for approved targets only.

Return:
- observed evidence
- derived findings
- confidence
- limitations
- provenance
- feature traces
- relationships

Do not convert inferred behavior directly into VERIFIED knowledge.


---

# UI IMPLEMENTATION MASTER PROMPT

Build a real-time Engineering OS control center, not a static mockup.

Use backend events as source of truth.

Required interaction:
- select run
- watch workflow nodes activate
- see agent/tool/capability selections
- inspect why selected
- inspect context coverage
- inspect knowledge/evidence
- inspect policy allow/deny
- inspect tokens/time
- inspect build/test result
- browse capability health
- approve/reject Workshop executable proposals

UI must remain usable with:
- 100k+ historical events
- thousands of knowledge nodes
- hundreds of capabilities
- long-running runs

Use pagination, cursors, virtualization and incremental graph loading.
Do not load whole graphs/event history into browser memory.
