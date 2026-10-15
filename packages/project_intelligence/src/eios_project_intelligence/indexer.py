"""Bootstrap detection and incremental indexing of a project.

Sync algorithm (per branch):

1. ``git ls-tree`` gives every tracked blob with its git object id. A file is (re)indexed only if
   its blob id differs from the stored ``content_hash``: unchanged files are never read or parsed.
2. Changed/new files are parsed; removed files are deleted with their symbols and graph rows.
3. Imports/calls of *unchanged* files that depended on added, removed or renamed definitions are
   re-resolved from their stored edges (no re-parsing).
4. Uncommitted work (modified, deleted and untracked files, detected by hashing working-tree
   content ourselves) goes into a temporary ``overlay`` scope with a TTL. The overlay is rebuilt
   on every sync, is keyed by branch, and is never promoted to committed/permanent knowledge.

Documents, configuration, SQL, migrations and tests are also cut into searchable text chunks
(:mod:`eios_project_intelligence.chunking`) that live and die with their file row, overlay
included. Files indexed before the content index existed, or by an older chunker, are re-read once
(``SyncResult.reindexed``) without being reported as changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from eios_domain.errors import NotFoundError
from eios_domain.events import EventType
from eios_domain.ids import utcnow
from eios_domain.project import (
    BootstrapState,
    FileScope,
    normalize_remote_url,
    project_fingerprint,
)
from eios_observability import RunContext
from eios_project_intelligence.chunking import CHUNK_VERSION, ChunkSpec, chunk_text, wants_chunks
from eios_project_intelligence.content_cache import ContentFingerprints
from eios_project_intelligence.git import (
    CommitInfo,
    GitError,
    SubprocessGit,
    TreeEntry,
    git_blob_sha,
    matches_blob,
)
from eios_project_intelligence.graph_builder import (
    build_file_change,
    file_key,
    resolve_imports,
)
from eios_project_intelligence.index_version import index_version
from eios_project_intelligence.languages import detect_language, looks_binary
from eios_project_intelligence.parsers import ParsedFile, ParsedImport, ParserRegistry
from eios_project_intelligence.resolver import CallRequest, resolve_calls
from eios_project_intelligence.store import (
    FileChange,
    GraphEdgeSpec,
    OverlayRow,
    Project,
    ProjectStore,
)
from eios_project_intelligence.workspace import ApprovedWorkspaces

_BATCH = 200
_PARSE_CACHE_MAX = 30_000  # parse results (no text) of unchanged uncommitted files
_MAX_HASH_BYTES = 50_000_000
_EVENT_FILE_CAP = 50
MAJOR_DIVERGENCE_MIN_FILES = 200
MAJOR_DIVERGENCE_RATIO = 0.3

GitFactory = Callable[[Path], SubprocessGit]


@dataclass
class BootstrapResult:
    state: BootstrapState
    project: Project | None
    branch: str | None = None
    head: str | None = None
    dirty_paths: int = 0
    previous_branch: str | None = None
    previous_commit: str | None = None
    detail: str | None = None

    @property
    def needs_sync(self) -> bool:
        return self.state is not BootstrapState.CURRENT and self.state is not BootstrapState.ERROR


@dataclass
class SyncResult:
    project_id: uuid.UUID
    branch: str
    commit: str
    state: BootstrapState
    files_total: int
    changed: list[str]
    removed: list[str]
    overlay: list[str]
    symbols_total: int
    duration_ms: int
    snapshot_id: uuid.UUID | None = None
    overlay_truncated: bool = False
    chunks_total: int = 0
    reindexed: int = 0  # unchanged files re-read only to (re)build their searchable text
    # (path, change) of uncommitted work that differs from the previous sync's overlay;
    # change is added | modified | removed | reverted. Drives the live activity feed.
    overlay_changes: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class _Parsed:
    path: str
    language: str
    size: int
    sha: str
    parsed: ParsedFile | None
    metadata: dict[str, object] = field(default_factory=dict)
    chunks: list[ChunkSpec] = field(default_factory=list)
    keep: bool = False  # the stored row, symbols and text are still right (uncommitted view)


@dataclass
class WorktreeChanges:
    modified: list[str]
    deleted: list[str]
    untracked: list[str]
    untracked_truncated: bool

    @property
    def overlay_paths(self) -> list[str]:
        return sorted(set(self.modified) | set(self.untracked))

    @property
    def count(self) -> int:
        return len(self.modified) + len(self.deleted) + len(self.untracked)


def _safe_relative(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return bool(parts) and not PurePosixPath(rel).is_absolute() and ".." not in parts


def _read_regular(root: Path, rel: str, max_bytes: int) -> tuple[bytes | None, int]:
    """Read a regular, non-symlink file inside ``root``. Returns (data, size); data is None if
    the file is missing, not a regular file, or larger than ``max_bytes``."""
    if not _safe_relative(rel):
        return None, 0
    path = root / rel
    try:
        st = os.lstat(path)
    except OSError:
        return None, 0
    if not stat.S_ISREG(st.st_mode):
        return None, 0
    if st.st_size > max_bytes:
        return None, st.st_size
    try:
        return path.read_bytes(), st.st_size
    except OSError:
        return None, 0


def fingerprint_file(
    root: Path, rel: str, max_bytes: int, cache: ContentFingerprints
) -> tuple[str, int, bytes | None]:
    """``(blob sha, size, data)`` of a working-tree file for the uncommitted view.

    ``data`` is None when the hash came from the cache (the file was not read). The sha is ""
    when the file is missing, not a regular file, unreadable or larger than ``max_bytes``."""
    if not _safe_relative(rel):
        return "", 0, None
    path = root / rel
    try:
        st = os.lstat(path)
    except OSError:
        return "", 0, None
    if not stat.S_ISREG(st.st_mode):
        return "", 0, None
    if st.st_size > max_bytes:
        return "", st.st_size, None
    base = str(root)
    known = cache.get(base, rel, st)
    if known is not None:
        return known.blob_sha, st.st_size, None
    try:
        data = path.read_bytes()
    except OSError:
        return "", 0, None
    sha = git_blob_sha(data)
    cache.put(base, rel, st, sha)
    return sha, st.st_size, data


def detect_worktree_changes(
    root: Path,
    tree: dict[str, TreeEntry],
    untracked: list[str],
    truncated: bool,
    cache: ContentFingerprints | None = None,
) -> WorktreeChanges:
    """Compare the working tree with HEAD by hashing content (never runs git filters).

    With ``cache``, a file whose size and mtime are unchanged since it was found equal to its
    blob is not read again."""
    modified: list[str] = []
    deleted: list[str] = []
    base = str(root)
    for rel, entry in tree.items():
        if not _safe_relative(rel):
            continue
        path = root / rel
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            deleted.append(rel)
            continue
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            modified.append(rel)  # replaced by a symlink/directory
            continue
        if st.st_size > _MAX_HASH_BYTES:
            if st.st_size != entry.size:
                modified.append(rel)
            continue
        if cache is not None:
            known = cache.get(base, rel, st)
            if known is not None and known.clean_sha == entry.sha:
                continue
        data, _ = _read_regular(root, rel, _MAX_HASH_BYTES)
        if data is None:
            continue
        matched = matches_blob(data, entry.sha)
        if cache is not None:
            cache.put(base, rel, st, git_blob_sha(data), entry.sha if matched else None)
        if not matched:
            modified.append(rel)
    return WorktreeChanges(sorted(modified), sorted(deleted), sorted(untracked), truncated)


class ProjectIndexer:
    def __init__(
        self,
        store: ProjectStore,
        workspaces: ApprovedWorkspaces,
        parsers: ParserRegistry | None = None,
        *,
        max_file_bytes: int = 1_000_000,
        overlay_ttl: timedelta = timedelta(hours=24),
        git_factory: GitFactory | None = None,
    ) -> None:
        self._store = store
        self._workspaces = workspaces
        self._parsers = parsers or ParserRegistry()
        self._max = max_file_bytes
        self._ttl = overlay_ttl
        self._git: GitFactory = git_factory or (lambda root: SubprocessGit(root))
        # what an unchanged uncommitted file already cost us: its hash (by size and mtime) and its
        # parse result. In process only; a restart starts cold, which is slower but never wrong.
        self._fingerprints = ContentFingerprints()
        self._parse_cache: OrderedDict[tuple[str, str], _Parsed] = OrderedDict()
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------ bootstrap
    async def bootstrap(self, path: str | Path) -> BootstrapResult:
        """Identify the project at ``path`` (registering it if new) and report how current its
        index is. Never indexes anything itself."""
        start = self._workspaces.resolve(path)  # raises WorkspaceViolationError if not approved
        try:
            top = self._workspaces.resolve(await self._git(start).toplevel())
            git = self._git(top)
            head = await git.head()
            branch = await git.branch()
            remote = await git.remote_url()
            root_commit = await git.root_commit()
        except GitError as exc:
            return BootstrapResult(BootstrapState.ERROR, None, detail=str(exc)[:300])

        fingerprint = project_fingerprint(remote_url=remote, root_commit=root_commit, path=str(top))
        project = await self._store.find_by_fingerprint(fingerprint)
        if project is None:
            project = await self._store.create_project(
                name=top.name,
                fingerprint=fingerprint,
                remote=normalize_remote_url(remote) if remote else None,
                root_commit=root_commit,
                local_root=str(top),
                default_branch=branch,
            )
        elif project.local_root != str(top):
            await self._store.relocate(project.id, str(top))
            project = await self._store.get_project(project.id) or project

        try:
            tree = await git.tree()
            untracked, truncated = await git.untracked()
            changes = await asyncio.to_thread(
                detect_worktree_changes, top, tree, untracked, truncated, self._fingerprints
            )
            state, detail = await self._classify(project, git, head, branch, changes, len(tree))
        except GitError as exc:
            return BootstrapResult(
                BootstrapState.ERROR, project, branch, head, detail=str(exc)[:300]
            )
        await self._store.update_state(project.id, state)
        return BootstrapResult(
            state=state,
            project=project,
            branch=branch,
            head=head,
            dirty_paths=changes.count,
            previous_branch=project.last_branch,
            previous_commit=project.last_commit,
            detail=detail,
        )

    async def worktree_signature(self, project: Project) -> str:
        """Digest of HEAD and the size+mtime of every tracked and untracked file. It changes when
        something is committed, edited, added or removed, and costs one ``stat`` per file (no
        reads, no hashing), so it is cheap enough to poll."""
        root = self._workspaces.resolve(project.local_root)
        git = self._git(root)
        head = await git.head()
        tree = await git.tree()
        untracked, _ = await git.untracked()

        def digest() -> str:
            h = hashlib.sha1(head.encode(), usedforsecurity=False)
            for rel in sorted({*tree, *untracked}):
                if not _safe_relative(rel):
                    continue
                try:
                    st = os.lstat(root / rel)
                    h.update(
                        f"{rel}\0{st.st_mtime_ns}\0{st.st_size}\n".encode(
                            "utf-8", "surrogateescape"
                        )
                    )
                except OSError:
                    h.update(f"{rel}\0-\n".encode("utf-8", "surrogateescape"))
            return h.hexdigest()

        return await asyncio.to_thread(digest)

    async def _classify(
        self,
        project: Project,
        git: SubprocessGit,
        head: str,
        branch: str,
        changes: WorktreeChanges,
        tracked_files: int,
    ) -> tuple[BootstrapState, str | None]:
        if project.last_commit is None or project.last_branch is None:
            return BootstrapState.NEW, "never indexed"
        if branch != project.last_branch:
            return BootstrapState.BRANCH_CHANGED, f"{project.last_branch} -> {branch}"
        if head != project.last_commit:
            if not await git.has_object(project.last_commit):
                return BootstrapState.MAJOR_DIVERGENCE, "previously indexed commit no longer exists"
            if not await git.is_ancestor(project.last_commit, head):
                return BootstrapState.MAJOR_DIVERGENCE, "history was rewritten (not a fast-forward)"
            changed = len(await git.changed_paths(project.last_commit, head))
            if changed >= max(
                MAJOR_DIVERGENCE_MIN_FILES, int(tracked_files * MAJOR_DIVERGENCE_RATIO)
            ):
                return BootstrapState.MAJOR_DIVERGENCE, f"{changed} files changed since last index"
            return BootstrapState.STALE, f"{changed} files changed since last index"
        if changes.count:
            return BootstrapState.DIRTY, f"{changes.count} uncommitted paths"
        return BootstrapState.CURRENT, None

    # ------------------------------------------------------------------ sync
    async def sync(
        self,
        project_id: uuid.UUID,
        *,
        ctx: RunContext | None = None,
        force_full: bool = False,
    ) -> SyncResult:
        started = time.monotonic()
        project = await self._store.get_project(project_id)
        if project is None:
            raise NotFoundError(f"project {project_id} not found")
        root = self._workspaces.resolve(project.local_root)
        git = self._git(root)
        head, branch, tree = await git.head(), await git.branch(), await git.tree()

        stored = await self._store.committed_files(project_id, branch)
        changed = sorted(
            p for p, e in tree.items() if force_full or stored.get(p, (None, None))[1] != e.sha
        )
        removed = sorted(set(stored) - set(tree))
        added = [p for p in changed if p not in stored]
        # Unchanged files whose searchable text is missing (indexed before the content index
        # existed, or by an older chunker) are re-read like changed ones, but not reported as such.
        stale_text = sorted(
            (await self._store.paths_needing_chunks(project_id, branch)) & set(tree)
        )
        reindexed = sorted(set(stale_text) - set(changed))
        to_index = sorted({*changed, *reindexed})
        old_names = await self._store.symbol_names_for_files(
            [stored[p][0] for p in [*to_index, *removed] if p in stored]
        )
        old_index = await self._store.provides_index(project_id, branch)

        # 1. parse changed files (in batches, off the event loop)
        parsed_changes: list[_Parsed] = []
        for i in range(0, len(to_index), _BATCH):
            batch = to_index[i : i + _BATCH]
            blobs = await git.read_blobs([tree[p].sha for p in batch], max_bytes=self._max)
            parsed_changes += await asyncio.to_thread(
                self._parse_batch,
                [(p, tree[p].sha, tree[p].size, blobs[tree[p].sha]) for p in batch],
            )

        # 2. module-key index after the change
        touched = set(to_index) | set(removed)
        index: dict[str, list[str]] = {}
        for key, paths in old_index.items():
            kept = [p for p in paths if p not in touched]
            if kept:
                index[key] = kept
        for pc in parsed_changes:
            for key in pc.parsed.provides if pc.parsed else []:
                index.setdefault(key, []).append(pc.path)

        # 3. store files, symbols, containment + import edges
        changes: list[FileChange] = []
        call_requests: list[CallRequest] = []
        imported: dict[str, set[str]] = {}
        for pc in parsed_changes:
            change = build_file_change(
                pc.path,
                language=pc.language,
                size_bytes=pc.size,
                content_hash=pc.sha,
                parsed=pc.parsed,
                parser=self._parsers.get(pc.language),
                key_index=index,
                extra_metadata=pc.metadata,
                chunks=pc.chunks,
            )
            changes.append(change)
            imported[pc.path] = {
                e.dst_key.removeprefix("file:")
                for e in change.edges
                if e.kind == "imports" and e.dst_key.startswith("file:")
            }
            if pc.parsed:
                call_requests.extend(
                    CallRequest(pc.path, c.caller, c.callee, c.line, c.kind)
                    for c in pc.parsed.calls
                )
        first = True
        for i in range(0, max(len(changes), 1), _BATCH):
            await self._store.apply_changes(
                project_id,
                branch,
                FileScope.COMMITTED,
                changes[i : i + _BATCH],
                deleted_paths=removed if first else (),
            )
            first = False

        # 4. calls of changed files + stale resolutions of unchanged files
        new_names = {s.name.lower() for ch in changes for s in ch.symbols}
        affected_names = sorted(old_names | new_names)
        stale_calls = await self._store.unresolved_and_affected_edges(
            project_id,
            branch,
            kinds=["calls", "references"],
            callee_names=affected_names,
            exclude_owner_paths=to_index,
        )
        removed_keys = [file_key(p) for p in removed]
        new_provided = {
            k
            for pc in parsed_changes
            if pc.path in set(added)
            for k in (pc.parsed.provides if pc.parsed else [])
        }
        stale_imports = (
            await self._store.unresolved_and_affected_edges(
                project_id,
                branch,
                kinds=["imports"],
                dst_keys=removed_keys,
                exclude_owner_paths=to_index,
            )
            if removed_keys
            else []
        )
        if new_provided:
            stale_imports += await self._store.edges_with_external_import_keys(
                project_id, branch, sorted(new_provided), exclude_owner_paths=to_index
            )
        reimports = self._reresolve_imports(stale_imports, index)
        owners = {e["owner_path"] for e in stale_calls if e["owner_path"]}
        if owners:
            for e in await self._store.edges_from(
                project_id, branch, [file_key(p) for p in owners], kinds=["imports"], limit=100000
            ):
                if e["dst_key"].startswith("file:"):
                    imported.setdefault(e["owner_path"], set()).add(
                        e["dst_key"].removeprefix("file:")
                    )
        for re_edge in reimports:
            if re_edge.owner_path and re_edge.dst_key.startswith("file:"):
                imported.setdefault(re_edge.owner_path, set()).add(
                    re_edge.dst_key.removeprefix("file:")
                )
        call_requests.extend(
            CallRequest(
                e["owner_path"] or "",
                self._caller_of(e["src_key"], e["owner_path"] or ""),
                str(e["attrs"].get("callee", "")),
                int(e["attrs"].get("line", 0)),
                e["kind"],
            )
            for e in stale_calls
            if e["attrs"].get("callee")
        )
        call_edges = await resolve_calls(
            self._store, project_id, branch, call_requests, imported_paths=imported
        )
        await self._store.replace_edges(
            project_id,
            branch,
            FileScope.COMMITTED,
            [e["id"] for e in stale_calls] + [e["id"] for e in stale_imports],
            [*call_edges, *reimports],
        )

        # 5. uncommitted work -> overlay
        overlay_paths, overlay_truncated, overlay_changes = await self._sync_overlay(
            project_id, branch, root, git, tree, index
        )

        files_total = await self._store.count_files(project_id, branch, FileScope.COMMITTED)
        symbols_total = await self._store.count_symbols(project_id, branch, FileScope.COMMITTED)
        chunks_total = await self._store.count_chunks(project_id, branch, FileScope.COMMITTED)
        state = BootstrapState.DIRTY if overlay_paths else BootstrapState.CURRENT
        duration_ms = int((time.monotonic() - started) * 1000)
        snapshot_id = await self._store.add_snapshot(
            project_id,
            branch=branch,
            commit_sha=head,
            dirty_paths=len(overlay_paths),
            state=state,
            files_total=files_total,
            files_changed=len(changed) + len(removed),
            symbols_total=symbols_total,
            duration_ms=duration_ms,
        )
        await self._store.update_state(project_id, state, branch=branch, commit=head, synced=True)

        result = SyncResult(
            project_id=project_id,
            branch=branch,
            commit=head,
            state=state,
            files_total=files_total,
            changed=changed,
            removed=removed,
            overlay=overlay_paths,
            symbols_total=symbols_total,
            duration_ms=duration_ms,
            snapshot_id=snapshot_id,
            overlay_truncated=overlay_truncated,
            chunks_total=chunks_total,
            reindexed=len(reindexed),
            overlay_changes=overlay_changes,
        )
        await self._emit(ctx, result, set(added))
        return result

    async def read_source(
        self,
        project: Project,
        branch: str,
        path: str,
        *,
        content_hash: str | None = None,
        overlay: bool = False,
    ) -> str | None:
        """Text of an indexed file: the committed blob (by object id) or the working-tree file
        for overlay entries. Returns None if unavailable, binary or too large."""
        root = self._workspaces.resolve(project.local_root)
        data: bytes | None
        if overlay:
            data, _ = await asyncio.to_thread(_read_regular, root, path, self._max)
        else:
            if content_hash is None:
                return None
            blobs = await self._git(root).read_blobs([content_hash], max_bytes=self._max)
            data = blobs.get(content_hash)
        if data is None or looks_binary(data):
            return None
        return data.decode("utf-8", errors="replace")

    async def history(
        self,
        project: Project,
        *,
        paths: list[str] | None = None,
        grep: str | None = None,
        limit: int = 20,
    ) -> list[CommitInfo]:
        """Recent commits touching ``paths`` and/or matching ``grep`` (headers only)."""
        root = self._workspaces.resolve(project.local_root)
        return await self._git(root).log(paths=paths, grep=grep, limit=limit)

    # ------------------------------------------------------------------ helpers
    def _parse_batch(
        self, items: Sequence[tuple[str, str, int, bytes | None]], *, with_chunks: bool = True
    ) -> list[_Parsed]:
        out: list[_Parsed] = []
        for path, sha, size, data in items:
            language = detect_language(path)
            metadata: dict[str, object] = {}
            parsed: ParsedFile | None = None
            chunks: list[ChunkSpec] = []
            if data is None:
                metadata["skipped"] = "too_large" if size > self._max else "unreadable"
                if wants_chunks(path, language):
                    metadata["chunk_version"] = CHUNK_VERSION  # nothing to read: do not retry
            elif looks_binary(data):
                language = "binary"
                metadata["skipped"] = "binary"
            else:
                text = data.decode("utf-8", errors="replace")
                parser = self._parsers.get(language)
                if parser is not None:
                    parsed = parser.parse(path, text)
                else:
                    parsed = ParsedFile(language=language, line_count=text.count("\n") + 1)
                if with_chunks and wants_chunks(path, language):
                    result = chunk_text(path, language, text)
                    chunks = result.chunks
                    metadata["chunk_version"] = CHUNK_VERSION
                    metadata["chunks"] = len(chunks)
                    if result.reason:
                        metadata["chunk_note"] = result.reason
                    if result.truncated:
                        metadata["chunks_truncated"] = True
            out.append(_Parsed(path, language, size, sha, parsed, metadata, chunks))
        return out

    @staticmethod
    def _caller_of(src_key: str, owner_path: str) -> str | None:
        prefix = f"symbol:{owner_path}::"
        return src_key[len(prefix) :] if src_key.startswith(prefix) else None

    def _reresolve_imports(
        self, edges: list[dict[str, Any]], index: dict[str, list[str]]
    ) -> list[GraphEdgeSpec]:
        """Recompute import edges from their stored candidate keys against the new index."""
        out: list[GraphEdgeSpec] = []
        seen: set[tuple[str, str]] = set()
        for e in edges:
            owner = str(e["owner_path"] or "")
            attrs = e["attrs"] if isinstance(e["attrs"], dict) else {}
            keys = list(attrs.get("keys", []))
            spec = str(attrs.get("spec", ""))
            if (owner, spec) in seen:
                continue
            seen.add((owner, spec))
            language = detect_language(owner)
            parser = self._parsers.get(language)
            if parser is None:
                continue
            fake = ParsedFile(language=language, line_count=0)
            fake.imports.append(ParsedImport(spec, int(attrs.get("line", 0))))
            # reuse the stored candidate keys exactly (relative imports are already resolved)
            wrapper = _FixedKeysParser(parser, {spec: keys})
            out.extend(resolve_imports(owner, fake, wrapper, index))
        return out

    async def _sync_overlay(
        self,
        project_id: uuid.UUID,
        branch: str,
        root: Path,
        git: SubprocessGit,
        tree: dict[str, TreeEntry],
        committed_index: dict[str, list[str]],
    ) -> tuple[list[str], bool, list[tuple[str, str]]]:
        untracked, truncated = await git.untracked()
        wt = await asyncio.to_thread(
            detect_worktree_changes, root, tree, untracked, truncated, self._fingerprints
        )
        state = await self._store.overlay_state(project_id, branch)
        previous = {p: (s.content_hash, s.status) for p, s in state.items()}
        paths = wt.overlay_paths
        if not paths and not wt.deleted:
            await self._store.clear_overlay(project_id, branch)
            return (
                [],
                False,
                [(p, "reverted" if p in tree else "removed") for p in sorted(previous)][
                    :_EVENT_FILE_CAP
                ],
            )

        # Files whose content did not change keep their stored row, symbols and text; the graph
        # (nodes and edges) is rebuilt for all of them, so resolution sees the whole view.
        await self._store.clear_overlay_graph(project_id, branch)
        parsed: list[_Parsed] = []
        for i in range(0, len(paths), _BATCH):
            parsed += await asyncio.to_thread(
                self._overlay_batch, root, paths[i : i + _BATCH], tree, state
            )
        overlay_set = set(paths) | set(wt.deleted)
        left = [p for p in state if p not in overlay_set]  # no longer part of the uncommitted view

        # What moved since the last sync. A first look at a project with a large pile of
        # uncommitted files is a baseline, not fifty "new file" events.
        touched: list[tuple[str, str]] = []
        if previous or len(paths) + len(wt.deleted) <= _EVENT_FILE_CAP:
            for pc in parsed:
                before = previous.get(pc.path)
                if before is None:
                    touched.append((pc.path, "modified" if pc.path in tree else "added"))
                elif before[0] != pc.sha:
                    touched.append((pc.path, "modified"))
            for rel in wt.deleted:
                if previous.get(rel, ("", ""))[1] != "deleted":
                    touched.append((rel, "removed"))
            touched.extend(
                (p, "reverted" if p in tree else "removed")
                for p in sorted(set(previous) - overlay_set)
            )
        index: dict[str, list[str]] = {}
        for key, ps in committed_index.items():
            kept = [p for p in ps if p not in overlay_set]
            if kept:
                index[key] = kept
        for pc in parsed:
            for key in pc.parsed.provides if pc.parsed else []:
                index.setdefault(key, []).append(pc.path)

        changes: list[FileChange] = []
        requests: list[CallRequest] = []
        imported: dict[str, set[str]] = {}
        for pc in parsed:
            ch = build_file_change(
                pc.path,
                language=pc.language,
                size_bytes=pc.size,
                content_hash=pc.sha,
                parsed=pc.parsed,
                parser=self._parsers.get(pc.language),
                key_index=index,
                extra_metadata={
                    **pc.metadata,
                    "tracked": pc.path in tree,
                    "index_version": index_version(),
                },
                chunks=pc.chunks,
            )
            if pc.keep:
                ch.keep_rows, ch.symbols, ch.chunks = True, [], []
            changes.append(ch)
            imported[pc.path] = {
                e.dst_key.removeprefix("file:")
                for e in ch.edges
                if e.kind == "imports" and e.dst_key.startswith("file:")
            }
            if pc.parsed:
                requests.extend(
                    CallRequest(pc.path, c.caller, c.callee, c.line, c.kind)
                    for c in pc.parsed.calls
                )
        for rel in wt.deleted:  # tombstones hide the committed file from the working view
            changes.append(
                FileChange(
                    rel,
                    detect_language(rel),
                    0,
                    0,
                    "",
                    status="deleted",
                    metadata={"tracked": True},
                )
            )
        expires = utcnow() + self._ttl
        for i in range(0, max(len(changes), 1), _BATCH):
            await self._store.apply_changes(
                project_id,
                branch,
                FileScope.OVERLAY,
                changes[i : i + _BATCH],
                deleted_paths=left if i == 0 else (),
                expires_at=expires,
            )
        if requests:
            edges = await resolve_calls(
                self._store,
                project_id,
                branch,
                requests,
                imported_paths=imported,
                scopes=(FileScope.COMMITTED, FileScope.OVERLAY),
                hidden_committed_paths=frozenset(overlay_set),
            )
            await self._store.replace_edges(project_id, branch, FileScope.OVERLAY, [], edges)
        return paths, wt.untracked_truncated, touched[:_EVENT_FILE_CAP]

    def _can_keep(
        self, rel: str, sha: str, size: int, before: OverlayRow, *, tracked: bool
    ) -> bool:
        """Whether the stored rows of an uncommitted file are exactly what indexing it now would
        write: same content, written by the same parsers, and still tracked (or not) the same."""
        language = detect_language(rel)
        return (
            sha != ""
            and before.content_hash == sha
            and before.status == "active"
            and before.size_bytes == size
            and before.language in (language, "binary")
            and before.metadata.get("index_version") == index_version()
            and before.metadata.get("tracked") == tracked
            and (
                not wants_chunks(rel, language)
                or before.metadata.get("chunk_version") == CHUNK_VERSION
            )
        )

    def _remember(self, pc: _Parsed) -> None:
        slim = replace(pc, chunks=[], keep=False)  # parse results only: text is never reused
        key = (pc.path, pc.sha)
        with self._cache_lock:
            self._parse_cache[key] = slim
            self._parse_cache.move_to_end(key)
            while len(self._parse_cache) > _PARSE_CACHE_MAX:
                self._parse_cache.popitem(last=False)

    def _overlay_batch(
        self,
        root: Path,
        paths: Sequence[str],
        tree: dict[str, TreeEntry],
        state: dict[str, OverlayRow],
    ) -> list[_Parsed]:
        """Hash and parse uncommitted files, reading only those that changed (runs in a thread).

        A file whose stored rows are still right is marked ``keep``: its parse result comes from
        the cache when possible and its text is never chunked again."""
        out: list[_Parsed] = []
        for rel in paths:
            sha, size, data = fingerprint_file(root, rel, self._max, self._fingerprints)
            before = state.get(rel)
            if before is not None and self._can_keep(rel, sha, size, before, tracked=rel in tree):
                with self._cache_lock:
                    hit = self._parse_cache.get((rel, sha))
                    if hit is not None:
                        self._parse_cache.move_to_end((rel, sha))
                if hit is not None:
                    out.append(replace(hit, metadata=dict(before.metadata), keep=True))
                    continue
                if data is None:  # the hash came from the cache: read the bytes to parse them
                    data, size = _read_regular(root, rel, self._max)
                if data is not None and git_blob_sha(data) == sha:
                    pc = self._parse_batch([(rel, sha, size, data)], with_chunks=False)[0]
                    pc.metadata, pc.keep = dict(before.metadata), True
                    self._remember(pc)
                    out.append(pc)
                    continue
                sha = git_blob_sha(data) if data is not None else ""  # changed under us
            if data is None and sha:  # hash from the cache, but the stored rows are not reusable
                data, size = _read_regular(root, rel, self._max)
                sha = git_blob_sha(data) if data is not None else ""
            pc = self._parse_batch([(rel, sha, size, data)])[0]
            if sha:
                self._remember(pc)
            out.append(pc)
        return out

    async def _emit(self, ctx: RunContext | None, result: SyncResult, added: set[str]) -> None:
        if ctx is None:
            return
        for path in result.changed[:_EVENT_FILE_CAP]:
            await ctx.emit(
                EventType.FILE_CHANGED,
                path,
                data={"path": path, "change": "added" if path in added else "modified"},
            )
        for path in result.removed[: max(0, _EVENT_FILE_CAP - len(result.changed))]:
            await ctx.emit(EventType.FILE_CHANGED, path, data={"path": path, "change": "removed"})
        room = max(0, _EVENT_FILE_CAP - len(result.changed) - len(result.removed))
        for path, change in result.overlay_changes[:room]:
            await ctx.emit(
                EventType.FILE_CHANGED,
                path,
                data={"path": path, "change": change, "uncommitted": True},
            )
        await ctx.emit(
            EventType.PROJECT_SYNCED,
            f"{result.branch}@{result.commit[:8]}: {len(result.changed)} changed, "
            f"{len(result.removed)} removed, {len(result.overlay)} uncommitted",
            data={
                "project_id": str(result.project_id),
                "branch": result.branch,
                "commit": result.commit,
                "state": result.state.value,
                "files_total": result.files_total,
                "changed": len(result.changed),
                "removed": len(result.removed),
                "overlay": len(result.overlay),
                "symbols_total": result.symbols_total,
                "chunks_total": result.chunks_total,
                "duration_ms": result.duration_ms,
            },
        )


class _FixedKeysParser:
    """Adapter that replays previously computed import candidate keys."""

    def __init__(self, inner: object, keys_by_spec: dict[str, list[str]]) -> None:
        self.language = getattr(inner, "language", "")
        self._keys = keys_by_spec

    def parse(self, path: str, text: str) -> ParsedFile:  # pragma: no cover - never called
        raise NotImplementedError

    def import_keys(self, path: str, imp: object) -> list[str]:
        return self._keys.get(getattr(imp, "spec", ""), [])


__all__ = [
    "BootstrapResult",
    "ProjectIndexer",
    "SyncResult",
    "WorktreeChanges",
    "detect_worktree_changes",
]
