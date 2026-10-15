# ADR 0018: The project content index - searchable file text

Status: Accepted (dogfood fix 1; extends ADR 0008 (index) and ADR 0009 / 0019 (retrieval, readiness)).

Context. The project index held symbols, files and a graph - no *text*. Running the system on its
own repository (`scripts/dogfood_eval.py`, 48 questions with engineer-written ground truth) showed
that whatever has no symbols was unreachable: 31 of 35 expected documents, 6 of 7 migrations and
most YAML/config were missing from the context pack, and a name that only a migration or a
document carries (`uq_event_run_seq`, `ix_job_claim`, `forbid_event_mutation`) was reported as an
unresolved entity, which blocked readiness and sent the governor up to L4 chasing nothing.

## Decision
1. **File text is indexed in bounded chunks.** New table `source.project_chunk` (migration 0010):
   `(file_id, project, branch, scope, path, ordinal, start_line, end_line, heading, text, idents,
   search_vector)`. A chunk belongs to a file row (`ON DELETE CASCADE`), so it is replaced when the
   file changes, goes with the file when it is removed, and - being keyed by `scope` - is rebuilt,
   expired (TTL) and purged with the overlay exactly like symbols and graph rows.
2. **What is chunked** (`eios_project_intelligence.chunking`, pure functions, no I/O):
   markdown / reStructuredText / text, YAML, TOML, JSON, INI, XML, Dockerfile, Makefile, shell,
   PowerShell, SQL and small files of unknown type, **plus Python migration scripts and test
   files** (the SQL, constraint and trigger names of a migration live in string literals the
   symbol parser cannot see; a test's assertions and fixtures say what the code under test is
   *for*). Other source code is covered by its symbols and is deliberately not duplicated here
   (measured, see below). Files are cut along their natural seams - markdown headings (with a
   breadcrumb), YAML top-level keys and second-level keys (`services > worker`), TOML tables,
   Dockerfile stages, Makefile targets, shell functions, SQL statements, migration operations,
   test functions - and a comment block stays with what it documents. Chunks aim at 40 lines /
   1 500 characters and never exceed 60 lines / 2 000 characters (a longer line is split); a file
   has at most 150 chunks.
3. **What is not indexed as text.** Lockfiles (`uv.lock`, `package-lock.json`, `yarn.lock`, ...),
   minified or generated files, text above 400 000 characters and unknown-type files above 64 000
   keep a **stub** (their first 12 lines) so they stay findable by name. Binary files and files that
   routinely carry credentials (`.env` except `.env.example`, `*.pem`, `*.key`, `id_rsa*`,
   `*.tfstate`, `credentials.json`, ...) get no text at all. Credentials embedded in URLs are
   masked (`user:[REDACTED]@host`) before storage - the same rule events use.
4. **Existing projects are backfilled by the next sync.** Each file records `chunk_version`
   in its metadata; a sync re-reads files that should have text at the current version but do not
   (`ProjectStore.paths_needing_chunks`) - once, and without reporting them as changed
   (`SyncResult.reindexed`). Bump `CHUNK_VERSION` to re-chunk everything after changing the rules.
5. **`ContentRetriever`** (new file `eios_retrieval/content.py`, source `content`, weight 1.0, in
   L1 and every level above, best two items reserved in the pack like decisions and memory):
   * Postgres full text search only (`to_tsvector('english', ...)` over heading (weight A), path
     (C) and body; identifiers are split at `_ . / -` and camelCase). Question words are weighted
     by rarity over the project's chunks (BM25-style idf, one query for the document frequencies),
     more when they are in the heading or the path; a word in half of all chunks counts for
     nothing, so a long question made of everyday words still finds the chunk that holds its two
     rare ones. An exact identifier named in the question (backticks, hints, snake_case,
     CamelCase, dotted) adds a bonus. A chunk must hold two of the words (or one that is in at most
     three chunks, or an exact identifier) and reach 30% of the best score - one stray word is not
     a hit.
   * **One candidate per file**: `file:<path>`, kind FILE, `start_line`/`end_line` = the best
     chunk, scope committed|overlay. It carries no text of its own; the governor's snippet loader
     reads exactly that range. (Carrying the chunk text made the reranker's word-overlap bonus
     favour content over symbols and tests: pack recall 0.723 -> 0.740 when it was removed.)
   * **By name**: "ADR 0005", "adr-12", "decision 7" pick `docs/adr/0005-*.md` by number; "the
     threat model", "the runbook" pick a document whose path holds two of the question's words (or
     one that at most three paths share), even when no word of its body matches.
   * Every content hit in a pack is also reported as a `KNOWLEDGE_HIT` event.
6. **Entity fallback** (`retrievers.find_entity_matches`). An identifier that matches **no
   symbol** (snake_case, CamelCase, dotted, or any name the caller gave explicitly) is looked up
   as a whole word, case-sensitively, in the content index and resolves to `file:<path>` of at
   most three files, migrations first, then documents, then configuration, then the busiest file.
   The exact retriever (as EXACT file candidates, beyond its limit) and `resolve_entities` both
   call this one function, so they cannot disagree (a test asserts it). Guards: a name the request
   *introduces* ("add a `x_y` column") is not looked up; an ordinary word in prose is never a name;
   a name the caller did not mark explicitly that is in more than 12 files is common vocabulary
   and stays unresolved. A name that exists nowhere is still `unresolved_entity` (blocking).
   *Index choice*: each chunk stores `idents text[]` - its distinct words and dotted names with
   case preserved - behind a GIN index, and the lookup is `idents @> ARRAY[name]`. Considered:
   `ILIKE '%name%'` (a scan of all text), `pg_trgm` (an extension, new infrastructure) and the
   English tsvector (stems and case-folds, and its tokenizer fuses `eios.retention` into one
   lexeme). The array is exact, indexed and bounded (300 chunk rows per name).
7. **Coverage** (`/projects/{id}/coverage`, `ProjectService.semantic_coverage`) additionally
   reports `content_files` (files that should have text), `files_with_content`, `content_chunks`
   and `content_coverage`. The knowledge-based numbers are unchanged.

## Evaluation (`scripts/dogfood_eval.py`, same index state for both builds)
Index of the main checkout (committed + working tree), base = a643cfe retrieval re-measured today
(the maintainer's reference numbers were 0.671 / 0.338 / 0.437):

| | base | content index |
|---|---|---|
| pack recall, hints | 0.684 | **0.758** |
| pack recall, no hints | 0.472 | **0.541** |
| search@10 recall | 0.385 | **0.459** |
| expected docs missing from the pack (hints) | 31 / 35 | **14 / 35** |
| migrations missing | 6 / 7 | **1 / 7** |
| YAML / config / lock missing (12 files) | 10 / 12 | **6 / 12** |
| tests missing (52) | 15 | 21 (worse) |
| other code missing (125) | 11 | 14 (worse) |
| packs "ready" / of those with expected files missing | 33 / 27 | 47 / 34 |
| mean items per pack | 32.8 | 27.0 |
| full re-index of this repo (forced, loaded machine) | 12.7-12.8 s | 14.3-14.7 s (+1.6-1.9 s) |

`tests/evals/dogfood/questions.json` is part of the repository, so the content index finds every
question verbatim in it and offers it as the top hit of every pack. On a copy of the repository
without that file the same comparison is: pack recall 0.693 -> **0.771** (hints), 0.489 -> 0.563
(no hints), search@10 0.385 -> 0.455; docs missing 31 -> 11, migrations 6 -> 1, config 10 -> 6,
tests 13 -> 21, code 11 -> 14.

What the numbers say, honestly:
* Documents, migrations and configuration became reachable (docs 31 -> 11-14 missing).
* Tests and code are **worse in the pack** (+6 and +3 files). The cause is not displacement
  alone: with `uq_...`-style names resolved and a rationale source present, 14 more packs are
  judged ready at L1/L2 and stop, where before an unresolvable entity (`forbid_event_mutation`) forced
  an escalation to L4 that dragged in tests by brute force (mean pack 33 -> 27 items, precision
  0.17 -> 0.21). Without hints the same effect costs 14 code files (62 -> 76 missing) while
  docs, config and migrations gain 31.
* `ready_to_act` is attained far more often (no hints: 3 -> 34 of 48) because content agreeing
  with symbols is "corroboration" (ADR 0019). The share of ready packs that still lack an
  expected file fell from 82% to 72% with hints, not to zero; readiness remains an estimate.
* Chunking also the other source code was measured (all `.py`: search@10 0.455 -> 0.498, docs
  pack recall -3, three times the stored text) and left out; chunking the tests alone gave
  pack recall 0.745 -> 0.771 and search@10 0.411 -> 0.455.
* The harness is deterministic (repeated runs agree to the file). The design choices above - no
  candidate text, reserved slots, indexing tests, the relevance floor - were each compared on
  these 48 questions, so the results are not independent of them; the thresholds themselves (two
  words, 30% of the best score, "rare" = at most three chunks, "generic" = half of all chunks)
  were set once from the score distributions, not swept.

## Consequences and known limits
* The text of indexed documents, configuration and tests is now also stored in the Engineering OS
  PostgreSQL (the same data `read_source` serves from git), under the same TTL for uncommitted
  work. A secret committed to an ordinary tracked file is therefore stored too; sensitive file
  names are skipped and URL credentials masked, nothing more (threat model, residual risks).
* Changing which files are chunked requires bumping `CHUNK_VERSION`; chunks of files that are no
  longer chunked are removed only when the file next changes.
* A GUC or constraint name that appears only in non-chunked source (`hnsw.ef_search` in
  `pgvector.py`) is still unresolved.
* The symbol retriever's path hits do not honour working-tree deletions (a deleted file stays a
  `file:` candidate from that source); content hits do.
* English stemming applies to prose and code alike; there are no embeddings for chunks (the
  hashing embedder is weak, ADR 0009) - a neural embedder over chunks is the obvious next step.
