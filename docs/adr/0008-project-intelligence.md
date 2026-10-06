# ADR 0008: Project Intelligence - identity, safe git access, incremental index, overlays

Status: Accepted (Phase 3)

## Identity
A project is identified by a fingerprint of the normalised git remote (credentials, scheme, `.git` and
case removed) plus the repository's root commit - never by path alone. Moving or re-cloning a
project keeps its identity; the new location is recorded (`project.location`). Without a remote the
root commit is used; without git, the path (weakest, and flagged by construction).

## Approved workspaces
Only directories under configured roots (`EIOS_WORKSPACE_ROOTS`) can be read; with none configured,
nothing can. Symlinks are resolved *before* the check, and the git top-level is checked too. In Docker
the workspace is mounted read-only at `/workspace`. The Policy Engine (Phase 6) reuses this guard.

## Safe git access (untrusted repositories)
Git can execute repository-defined programs (`core.fsmonitor`, `filter.*.clean/smudge`,
`diff.*.textconv`, hooks, pagers) - a control test shows plain `git status` runs a hostile
`core.fsmonitor` here. Therefore:
- only an allowlist of read-only plumbing commands runs; `status`, `ls-files -m`, `diff-files` are
  never used because they can run clean filters;
- dangerous config is overridden on the command line, system/global config is ignored, prompts and
  optional locks are off, there is a timeout and an output cap;
- working-tree changes are detected by hashing file content ourselves (tolerating CRLF conversion);
- object ids are validated before being passed to git; symlinks and submodules are never read.
Adversarial tests index a repository armed with fsmonitor/filter/textconv/pager/hook programs and prove
nothing executes.

## Incremental index
A file is re-read and re-parsed only if its git blob id differs from the stored hash. Removed files
are deleted with their symbols and graph rows. The graph stores edges **by node key**
(`file:`, `symbol:`, `dir:`, `external:`), so adding/removing a file never leaves dangling ids.
Imports and calls of *unchanged* files that depended on added/removed/renamed definitions are
re-resolved from their stored edges - without re-parsing them (tested).

## Overlays and branches
Rows are keyed `(project, branch, scope, path)`. Committed rows describe a branch's commit; the
`overlay` scope holds uncommitted work (modified, deleted-as-tombstone, untracked), is rebuilt on each
sync, expires (TTL) and is never permanent knowledge. Branches never mix; switching back to a branch
reuses its rows.

## Parsers
`SourceParser` is a port. Shipped: Python (`ast`), and heuristic comment/string-aware scanners for
C#, Java, TypeScript/JavaScript, Go, SQL (text only, never connects to a database) and Markdown. A
Tree-sitter adapter can replace any of them later (deviation from the plan's "where useful").
Call resolution is best-effort with explicit confidence (same file 0.95, imported 0.8, unique
project-wide 0.3, unresolved 0.2); consumers must threshold on confidence.

## Bootstrap states
NEW, CURRENT, STALE, DIRTY, BRANCH_CHANGED, MAJOR_DIVERGENCE (non-ancestor history, missing commit or a
very large change set), ERROR. Sync converges regardless of state because it is hash-based.

## Jobs
Indexing runs as `project.sync` jobs on the PostgreSQL queue (leases, heartbeats, retry, dead-letter,
stale-worker fencing), each recorded as an observable run. Active syncs are deduplicated per project.

## Known limits
Full-tree hashing on every sync is O(size of the working tree); a stat cache is a later
optimisation. A newly checked-out branch is indexed from scratch (no cross-branch row reuse yet).
Symbol search is substring-based; richer retrieval arrives in Phase 4.
