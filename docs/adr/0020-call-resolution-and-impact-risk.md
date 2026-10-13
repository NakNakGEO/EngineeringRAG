# ADR 0020: Call resolution stays inside a language family; impact risk can reach HIGH and CRITICAL

Status: Accepted (dogfood fixes; refines ADR 0008 and ADR 0009).

Context. Running the Impact Analyzer on its own repository showed Python calls resolving to
TypeScript symbols (`str(...)` -> `const str`, so `mission.ts` had 106 Python dependents), real
callers lost behind `__init__.py` re-exports, a risk scale that never produced HIGH or CRITICAL
(340 files: 210 low, 130 medium), migrations rated LOW, and a mistyped path rated LOW with
confidence 0.0.

Decisions.

1. **Call resolution never crosses language families** (`languages.language_family`: TypeScript
   and JavaScript interoperate, Python stays apart). Builtins (`str`, `append`, `.test`) resolve
   only through the caller's own file or imports and leave no `callee:` placeholder. A method that
   delegates to another object's method of the same name is not credited with calling itself.
   A nested function is callable only from its own file; class fields and module variables are
   never call targets.
2. **Imports follow re-exports one hop** (`__init__.py`, `index.ts`), so `from pkg import X`
   resolves at 0.8 to the defining file. Import edges record which names each statement brings
   in (`attrs.names`; `[]` = the whole module), merged across statements.
3. **Impact reports what it knows and flags what it does not.** Name-only callers
   (`callee:<name>`, confidence 0.2) are listed, marked `low_confidence`, counted in
   `low_confidence_edges` and never in the blast radius; schema files are exempt from name-only
   matching (migration entry points are invoked by the tool). Symbol targets get consumers from
   symbol-level edges plus files that import the symbol *by name*; file importers are listed
   separately. A target that resolves to nothing is MEDIUM with `resolved=false`, never LOW.
   Ambiguous suffix and symbol matches are named in `warnings`/`ambiguous`.
4. **The risk scale uses every factor it names.** Blast radius (1/5/15/40 files = 1/2/3/4 points),
   security-critical paths (+2, floor HIGH), schema paths (+2, floor MEDIUM; they count as
   database changes), public contract surfaces (+1), untested code (+2, +1 for route modules that
   are tested over HTTP), name-only callers across five or more files (+1). MEDIUM >= 3,
   HIGH >= 5, CRITICAL >= 7. Documents never have "missing tests". Migration and schema tests
   are recognised by name (nothing imports a migration).
5. **Path knowledge lives in settings**, not in the analyzer: `EIOS_IMPACT_SECURITY_CRITICAL_PATHS`,
   `_SCHEMA_PATHS`, `_SCHEMA_TEST_MARKERS`, `_ROUTE_PATHS`, `_CONTRACT_PATHS` (JSON lists of
   case-insensitive globs). The defaults match this repository's layout; owners override them.

Consequences. HIGH/CRITICAL now occur (security code, schema, wide untested changes), so workflow
team selection and approval gates driven by impact risk see real values. Known limits: calls are
resolved by name without type inference; TypeScript imports do not record imported names, so for
TypeScript targets importing a module counts as an importer, not a consumer; string-literal path
references from tests (`references_path`) are not indexed.
