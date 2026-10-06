# ADR 0007: Embeddings, vault enforcement and trust ceilings

Status: Accepted (Phase 2). Resolves assessment question Q6.

## Embeddings (Q6)
- `EmbeddingProvider` is a port (`model_id`, `dim`, `embed`). The default adapter is a deterministic,
  dependency-free **feature-hashing embedder** (signed hashing of unigrams, bigrams and identifier
  parts, 256 dimensions, L2-normalised). It is local-first (no model download, no network),
  reproducible in tests, and captures lexical similarity. It does **not** capture deep semantics.
- Phase 4 evaluations compare it against FTS and graph retrieval honestly. A neural embedder (local
  Ollama / sentence-transformers / OpenAI-compatible endpoint) is added as another adapter without
  changing callers.
- The vector column dimension is fixed (`EMBEDDING_DIM = 256`, `vector(256)`) with an HNSW cosine
  index. `embedding_model` is stored per row. Changing model or dimension means adding a new
  column (or table) and re-embedding via a migration + worker job; mixed-model similarity is never
  computed.

## Vault enforcement
Vault rules live in three layers: the pure domain (`check_vault_invariants`), the repositories
(search scope is applied *in SQL*), and the database (`CHECK` constraints: project vault needs a
project, default vault has no project, ephemeral needs an expiry; evidence trust is always RAW).
Raw SQL cannot misclassify data. Search isolation: another project's vault is never visible, and
without a project in scope no project data is visible.

Moving data from the Project Vault to the Default Vault (the only portable vault) is never
automatic: `move_to_default_vault` and promoting project-derived findings into the default vault both
require a `HumanApproval`, which only the admin-authenticated surface (Phase 6) can create.

## Trust ceilings
Records are created with trust no higher than their source allows (manual: APPROVED; code/import:
DERIVED; tool/LLM/research: OBSERVED). Knowledge built from an evidence-backed finding may reach
DERIVED, never more. Evidence is always RAW. VERIFIED/APPROVED for machine-originated knowledge
requires the verification rules of Phase 9. The public API treats every caller as an LLM: content it
submits is recorded as `llm` source and can never claim human origin.

## Consequences
- The API cannot be used to launder a model's assertion into trusted knowledge.
- Project knowledge cannot leak into another project's retrieval or the portable export.
