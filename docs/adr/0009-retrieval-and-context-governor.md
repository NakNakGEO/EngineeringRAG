# ADR 0009: Adaptive retrieval, the Context Governor and the Impact Analyzer

Status: Accepted (Phase 4). Resolves assessment question Q5 (Impact Analyzer lives here).

## Pipeline
`scope -> resolve entities -> retrieve (parallel) -> fuse -> graph-expand -> dedupe -> rerank ->
select within budget -> coverage check -> expand -> context pack`.

- **Retrievers** (ports): exact (named files/symbols, qualified names), symbol (distinctive words vs
  names), FTS (OR-style query so natural-language questions need not match every word), vector,
  memory, decisions, Git history (headers only), reusable components, research. A failing retriever
  degrades the pack and is reported in `coverage.notes`; it never fails the request.
- **Fusion**: weighted reciprocal-rank fusion (exact 1.6, symbol 1.1, FTS 1.0, vector 0.9, graph 0.9,
  decisions 0.9, memory 0.7, history 0.6).
- **Graph expansion** pulls in code connected to the seeds even when semantically dissimilar:
  dependencies (out), dependents/callers (in), tests and directory siblings at deeper levels, with
  edge-confidence thresholds so guesses do not drag in unrelated code.
- **Dedupe**: byte-equal text and >=0.85 shingle similarity within the same subject; a file is
  redundant when its symbols are already present (unless it was named).
- **Rerank**: trust, health, freshness; current working-tree (overlay) source over summaries;
  named entities and *their direct dependencies* first. STALE/CONTRADICTED knowledge is down-ranked
  and flagged, never silently dropped.
- **Budget**: critical evidence (named entities + their direct dependencies) is never trimmed, even
  over budget. Each low-volume source keeps its best two items so high-volume sources cannot crowd
  them out. Anything the budget removes is listed and lowers confidence.

## Levels
L0 immediate -> L1 focused -> L2 expanded (callers, memory, decisions, history) -> L3 deep
(transitive deps, tests, reusable components, research) -> L4 archaeology (siblings, long history).
Start level follows risk (high/critical start at L2). The governor raises the level automatically
while the gap can be closed by retrieval (`raise_level`, `fetch_dependencies`); it stops - and says
so - when only a human/index action can help (`resolve_entity`, `sync_project`) or the budget is the
limit.

## Honest coverage (`InformationGap`)
`coverage = 0.4 entities + 0.3 dependencies + 0.2 evidence + 0.1 index health`; confidence is
reduced by contradictions, budget truncation and a stale index. `ready_to_act` also requires:
no blocking gap, coverage >= the risk threshold (0.55/0.70/0.85/0.90), and direct-dependency
coverage >= 0.5/0.8/0.95/1.0 by risk - **a pack missing most of a function's dependencies is never
"ready", however good the rest looks** (found and fixed through the evaluation below). A non-trivial
task on a STALE/BRANCH_CHANGED/MAJOR_DIVERGENCE index is blocked; high-risk work needs verified
knowledge or current source evidence. Unknown named entities are reported as `unresolved_entity`
(blocking when the caller named them explicitly) - the system never papers over them.

## Impact Analyzer
For file/symbol targets: callers, callees, transitive dependents (depth-limited), consumers, tests
(structural + naming conventions, and `tests_missing`), business rules, SQL scripts, related
modules, history (flagging fix/incident commits), decisions, similar patterns, plus a risk score with
explicit factors. It is conservative (includes weaker edges and counts them as low-confidence).

## Evaluation (exit criterion: beat the vector-only baseline)
`tests/evals` compares five strategies on a synthetic payments codebase with 11 labelled questions
(dependency chains, stale and near-duplicate knowledge, SQL, tests, an unknown entity). Baselines see
code as text chunks. See [`docs/evals/retrieval.md`](../evals/retrieval.md). Headline: recall@10 0.86
vs 0.52 (vector-only) and 0.73 (FTS-only); graph coverage 1.00 vs 0.43. The test also asserts that
each added capability does not regress and that the unknown entity is *not* reported ready.

Honest caveats: the corpus is small and synthetic; the local hashing embedder is weak, which is why
plain hybrid (0.67) scored below FTS-only (0.73) here - a neural embedder behind
`EmbeddingProvider` is the obvious next improvement.

## Known limits
Call resolution is name-based with explicit confidence (no type inference); impact analysis is only
as good as the index; the stored project state is as fresh as the last bootstrap/sync (clients should
bootstrap first - the MCP `bootstrap_project` tool does).
