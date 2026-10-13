# ADR 0019: Readiness follows the question; graph expansion is ranked, not alphabetical

Status: Accepted (dogfood fixes; refines ADR 0009: coverage, escalation and expansion).

Context. Running the system on its own repository (`scripts/dogfood_eval.py`, 48 questions with
ground truth written by engineers who read the code) showed that `ready_to_act` overstated
readiness (23 of 48 packs were "ready" with expected files missing), that escalation work was
discarded on ties, that graph expansion cut by alphabetical key so hubs and `apps/` beat `tests/`,
and that symbol search spent its slots on generic words. Decisions:

1. **Readiness is about what the question asks for.** `intent.detect_intent` is a cheap regex pass
   (it can only add requirements). A tests question needs a test in the pack, a "why" question needs
   a document/decision/memory/history item, a documentation question needs a document: otherwise
   `no_test_evidence`, `no_rationale_evidence`, `no_doc_evidence` (blocking at medium risk and
   above, informational at low). "Test evidence" is a test named outright, found by the dependents-
   only walk, or a test item that mentions what was named - never a symbol that merely has "test"
   in its name and never a test reached by walking out and back through a shared caller.
2. **Only names the caller means count against the pack.** Hints, `backticks` and paths are
   explicit; a prose word that matches nothing is noted (`unresolved_term`), a name the request
   introduces ("add a nullable `correlation_id` column") is `new_name`. Without any grounding the
   entity component is replaced by agreement between independent retrievers plus word overlap, and
   components that cannot apply (dependencies of code nobody named) are left out and the weights
   re-normalised instead of being awarded for free.
3. **One definition of "this name exists".** `find_entity_symbols` serves both the exact retriever
   and the coverage check; names in prose match case-sensitively and acronyms are not entities.
4. **The best pack wins, not the first.** Ranking is (ready, coverage, fewer blocking gaps,
   confidence); a tie keeps the higher level unless the budget cut it down.
5. **Expansion is ranked before it is truncated**: nearer first, edge confidence discounted by
   fan-in, boosted by words shared with the question, round-robin across the named seeds, file
   stubs dropped when symbols of the file are present. Dependents of hubs (> 25 incoming edges)
   are not walked. Only the best 8 direct dependencies of a named seed are protected from the
   budget - and the coverage check requires exactly those, so readiness never demands what the
   pack may not keep.
6. **Symbol search uses rare words.** Question words are stemmed, filler is removed, document
   frequency is measured in one query, generic words (> 40 symbol names) are dropped, the rarest
   are searched first and every hit is confirmed against the symbol's name parts. Two question
   words in a file path surface that file.
7. **More of the repository is addressable**: class fields, enum members, nested functions,
   call/subscript-bound module names and exported TypeScript constants are symbols (`field` is
   skipped by lexical search but still resolves as a named entity; nested functions are callable
   only from their own file).

Consequences. `ready_to_act` is more often false for questions the pack cannot verifiably answer;
the reason is always in `gap.missing_context`. No threshold was tuned to the 48 questions; the
harness records the signals so they can be inspected (`--json`). Known limit: sufficiency for
a prose-only question is an estimate (corroboration + word overlap), not a proof.
