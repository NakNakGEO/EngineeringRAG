# ADR 0014: Knowledge governance, decision ledger and evaluation

Status: Accepted (Phase 9).

## Trust: an assertion cannot vouch for itself
Pure rules in `eios_domain.governance`, enforced by `GovernanceService`:
- VERIFIED needs *independent* evidence (tool or code output, same project/vault scope, with a
  content hash) or a named human. LLM-sourced evidence never counts; evidence from another project
  never counts; unknown evidence ids are rejected.
- APPROVED is human-only. Demotions are always allowed (demoting to RAW also clears CURRENT).
- Every change is a compare-and-set update plus an append in `knowledge.lifecycle`
  (actor, reason, evidence ids, approval) in one transaction.
- A `HumanApproval` is only constructed by the admin-authenticated API surface
  (`X-EIOS-Admin-Token` + a named approver); a model-level request cannot create one.

## Health, supersession, contradictions
- Health transitions are rule-checked (quarantine needs a reason; leaving QUARANTINED, reinstating
  historical/superseded knowledge and resolving a contradiction as CURRENT need a human).
- Supersession stays inside one vault/project scope, rejects self/cycles/quarantined successors,
  and superseding APPROVED knowledge needs a human.
- Contradictions are first-class rows. Reporting one marks the *less trusted* side CONTRADICTED
  (both when equal); an item with an open contradiction cannot be CURRENT; only a human resolves
  it (the loser is superseded by the winner). `detect_contradictions` finds same-`subject_key`,
  different-content pairs.

## Dependency invalidation
`knowledge.dependency` links items to files/symbols/other items. Project sync notifies listeners;
the governance listener marks items depending on changed/removed files STALE and propagates
through item dependencies (bounded to 5 hops). The `knowledge.maintain` job (worker) re-checks
recorded file hashes against the current index (catches missed events), detects contradictions
and reports counts. STALE is never silently dropped from retrieval (it is down-ranked and flagged).

## Decision ledger
Anyone may *propose* (the API always records PROPOSED); only a named human accepts or rejects.
ACCEPTED -> SUPERSEDED requires an ACCEPTED successor in the same scope, no cycles. REJECTED and
SUPERSEDED are immutable; every transition is recorded with approver and reason.

## Evaluation
YAML golden-case suites (`evals/`) run providers through the same policy/sandbox path
(`ToolRuntime.execute_provider`). Results are stored (`evaluation.run`) and update the provider's
`eval_score`/`known_weaknesses`, which are *routing inputs only*. A passing suite (>= 3 cases and
the required pass rate, healthy provider) supplies `verification_passed` for EXPERIMENTAL ->
VERIFIED. Evaluation can never reach TRUSTED, cannot un-quarantine, cannot approve generated or
downloaded artifacts, and cannot make a forbidden provider routable - those gates are in the
registry and Policy Engine, which evaluation does not touch (tests prove each).
