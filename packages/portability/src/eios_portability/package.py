"""Encrypted portable package of the Default Vault (export, verify, import).

Format (``.eiospkg``)::

    MAGIC(8) | header_len(4, big endian) | header(JSON) | ciphertext

* key: scrypt(passphrase, salt); cipher: AES-256-GCM with the magic and header as AAD, so any change
  to the header or ciphertext fails authentication;
* plaintext: gzip(JSON) with a SHA-256 recorded in the header (checked after decryption);
* content: Default Vault knowledge/memory/decisions/evidence (+ blob bytes) and non-builtin skills
  and agents. **Never** Project Vault, Ephemeral, audit, runs, jobs, secrets or executables.
* import re-checks everything: any row outside the Default Vault, or any project reference, rejects
  the whole package; imported knowledge is capped at DERIVED/UNVERIFIED (it re-earns trust), skills
  and agents re-register as plugin-origin EXPERIMENTAL, tools are informational only.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import struct
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_capability import LoadResult, RegistryService, parse_document
from eios_capability.store import RegistryStore
from eios_domain.knowledge import Health, SourceKind, Trust
from eios_domain.registry import ROUTABLE_STATES, Origin, RegistryState
from eios_knowledge import HashingEmbedder
from eios_knowledge.blob_store import BlobStore
from eios_portability.selector import ExportBundle, assert_clean, collect_default_vault
from eios_storage.tables.evidence import blob as blob_t
from eios_storage.tables.evidence import record as record_t
from eios_storage.tables.knowledge import decision as decision_t
from eios_storage.tables.knowledge import item as item_t
from eios_storage.tables.knowledge import provenance as provenance_t
from eios_storage.tables.memory import item as memory_t

MAGIC = b"EIOSPKG1"
FORMAT = "eios-default-vault"
VERSION = 1
MIN_PASSPHRASE = 12
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_PLAINTEXT_BYTES = 1024 * 1024 * 1024
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1


class PackageError(Exception):
    """The package is malformed, tampered with, or the passphrase is wrong."""


class ImportRejectedError(PackageError):
    """The package contained data that must never be imported (project/company data)."""


def _key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode())


def _check_passphrase(passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE:
        raise PackageError(f"the passphrase must be at least {MIN_PASSPHRASE} characters")


# ---- export -------------------------------------------------------------------------------------
async def _registry_payload(store: RegistryStore) -> dict[str, Any]:
    def portable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"manifest": r["manifest"], "state": r["state"], "origin": r["origin"]}
            for r in rows
            if r["origin"] != Origin.BUILTIN.value and RegistryState(r["state"]) in ROUTABLE_STATES
        ]

    tools = [
        {
            "id": p.id,
            "version": p.version,
            "description": p.manifest.get("description", ""),
            "capabilities": p.capabilities,
            "state": p.state.value,
            "executable": False,
        }
        for p in await store.list_providers()
        if p.origin is not Origin.BUILTIN
    ]
    return {
        "skills": portable(await store.list_skills()),
        "agents": portable(await store.list_agents()),
        "tools_info": tools,  # informational: executables never travel
    }


async def build_plaintext(
    engine: AsyncEngine, blobs: BlobStore | None = None, registry: RegistryStore | None = None
) -> tuple[dict[str, Any], ExportBundle]:
    bundle = await collect_default_vault(engine)
    blob_data: dict[str, str] = {}
    if blobs is not None:
        for b in bundle.blobs:
            sha = b["sha256"]
            if sha not in blob_data and blobs.exists(sha):
                blob_data[sha] = base64.b64encode(blobs.get(sha)).decode()
    payload = {
        "format": FORMAT,
        "version": VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "bundle": bundle.to_dict(),
        "blob_data": blob_data,
        "registry": await _registry_payload(registry) if registry else {},
    }
    return payload, bundle


def seal(payload: dict[str, Any], passphrase: str, counts: dict[str, int]) -> bytes:
    _check_passphrase(passphrase)
    plaintext = gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), mtime=0)
    salt, nonce = os.urandom(16), os.urandom(12)
    header = json.dumps(
        {
            "v": VERSION,
            "format": FORMAT,
            "kdf": "scrypt",
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "counts": counts,
            "plaintext_sha256": hashlib.sha256(plaintext).hexdigest(),
            "created_at": payload["created_at"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    aad = MAGIC + header
    cipher = AESGCM(_key(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)).encrypt(
        nonce, plaintext, aad
    )
    return MAGIC + struct.pack(">I", len(header)) + header + cipher


async def export_default_vault(
    engine: AsyncEngine,
    passphrase: str,
    *,
    blobs: BlobStore | None = None,
    registry: RegistryStore | None = None,
) -> bytes:
    _check_passphrase(passphrase)
    payload, bundle = await build_plaintext(engine, blobs, registry)
    assert_clean(bundle)
    return seal(payload, passphrase, bundle.counts())


# ---- open / verify ------------------------------------------------------------------------------
def read_header(data: bytes) -> dict[str, Any]:
    """Header only (no passphrase needed): counts and creation time are not secret."""
    if len(data) > MAX_PACKAGE_BYTES:
        raise PackageError("package is too large")
    if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 4:
        raise PackageError("not an Engineering OS package")
    (hlen,) = struct.unpack(">I", data[len(MAGIC) : len(MAGIC) + 4])
    start = len(MAGIC) + 4
    if hlen > 65536 or len(data) < start + hlen + 16:
        raise PackageError("corrupt package header")
    try:
        header = json.loads(data[start : start + hlen])
    except ValueError as exc:
        raise PackageError("corrupt package header") from exc
    if (
        header.get("format") != FORMAT
        or header.get("v") != VERSION
        or header.get("kdf") != "scrypt"
    ):
        raise PackageError("unsupported package version")
    result: dict[str, Any] = dict(header)
    result["_start"] = start + hlen
    result["_raw"] = data[start : start + hlen]
    return result


def open_package(data: bytes, passphrase: str) -> dict[str, Any]:
    header = read_header(data)
    n, r, p = int(header["n"]), int(header["r"]), int(header["p"])
    if n > 2**20 or r > 16 or p > 4:  # a hostile header must not make us burn memory
        raise PackageError("unreasonable key-derivation parameters")
    try:
        key = _key(passphrase, base64.b64decode(header["salt"]), n, r, p)
        plaintext = AESGCM(key).decrypt(
            base64.b64decode(header["nonce"]), data[header["_start"] :], MAGIC + header["_raw"]
        )
    except InvalidTag as exc:
        raise PackageError("wrong passphrase, or the package was modified") from exc
    if hashlib.sha256(plaintext).hexdigest() != header.get("plaintext_sha256"):
        raise PackageError("package content hash mismatch")
    decompressor = zlib.decompressobj(wbits=31)
    try:
        raw = decompressor.decompress(plaintext, MAX_PLAINTEXT_BYTES)
        if decompressor.unconsumed_tail:
            raise PackageError("package expands beyond the allowed size")
        payload = json.loads(raw)
    except (zlib.error, ValueError) as exc:
        raise PackageError("package content is corrupt") from exc
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise PackageError("unexpected package content")
    return payload


# ---- import --------------------------------------------------------------------------
@dataclass
class ImportReport:
    inserted: dict[str, int] = field(default_factory=dict)
    skipped_existing: dict[str, int] = field(default_factory=dict)
    capped_trust: int = 0
    registered: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    dry_run: bool = False


def verify_no_local_data(payload: dict[str, Any]) -> None:
    """Defence at the door: the package must be pure Default Vault."""
    bundle = payload.get("bundle", {})
    for name in ("knowledge_items", "memory_items", "decisions", "evidence_records"):
        for row in bundle.get(name, []):
            if row.get("vault") != "default" or row.get("project_id") is not None:
                raise ImportRejectedError(
                    f"{name} contains non-default-vault data; package rejected"
                )
            if row.get("run_id") is not None:
                raise ImportRejectedError(f"{name} references a local run; package rejected")


def _revive(table: sa.Table, row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in table.c:
        if col.name not in row:
            continue
        v = row[col.name]
        t = col.type
        if v is not None:
            if isinstance(t, sa.DateTime):
                v = datetime.fromisoformat(v)
            elif isinstance(t, sa.UUID):
                v = uuid.UUID(v)
            elif isinstance(t, sa.ARRAY) and isinstance(t.item_type, sa.UUID):
                v = [uuid.UUID(x) for x in v]
        out[col.name] = v
    return out


async def import_package(
    data: bytes,
    passphrase: str,
    engine: AsyncEngine,
    *,
    blobs: BlobStore,
    embedder: HashingEmbedder | None = None,
    registry: RegistryService | None = None,
    dry_run: bool = False,
) -> ImportReport:
    payload = open_package(data, passphrase)
    verify_no_local_data(payload)
    embedder = embedder or HashingEmbedder()
    bundle = payload["bundle"]
    report = ImportReport(dry_run=dry_run)
    if dry_run:
        report.inserted = {k: len(v) for k, v in bundle.items()}
        return report

    items = []
    for raw in bundle.get("knowledge_items", []):
        row = _revive(item_t, raw)
        original = Trust(row["trust"])
        row["trust"] = min(original, Trust.DERIVED, key=lambda t: t.rank).value
        report.capped_trust += int(
            original is not row["trust"] and original.rank > Trust.DERIVED.rank
        )
        row["source_kind"] = SourceKind.IMPORT.value
        row["project_id"], row["expires_at"] = None, None
        if row["health"] in {Health.CURRENT.value, Health.STALE.value, Health.CONTRADICTED.value}:
            row["health"] = Health.UNVERIFIED.value
        [row["embedding"]] = await embedder.embed([f"{row['title']}\n{row['content']}"])
        row["embedding_model"] = embedder.model_id
        items.append(row)
    memory = []
    for raw in bundle.get("memory_items", []):
        row = _revive(memory_t, raw)
        [row["embedding"]] = await embedder.embed([row["content"]])
        row["embedding_model"] = embedder.model_id
        memory.append(row)
    decisions = [_revive(decision_t, r) for r in bundle.get("decisions", [])]
    for d in decisions:
        d["superseded_by"] = None  # re-linked below once every row exists
    supersessions = {
        uuid.UUID(r["id"]): uuid.UUID(r["superseded_by"])
        for r in bundle.get("decisions", [])
        if r.get("superseded_by")
    }
    blob_rows = [_revive(blob_t, r) for r in bundle.get("blobs", [])]
    records = [_revive(record_t, r) for r in bundle.get("evidence_records", [])]
    prov = [_revive(provenance_t, r) for r in bundle.get("provenance", [])]

    for b in blob_rows:  # content-addressed, verified: write bytes before the rows that name them
        encoded = payload.get("blob_data", {}).get(b["sha256"])
        if encoded is None:
            raise PackageError(f"blob {b['sha256'][:12]} is missing from the package")
        content = base64.b64decode(encoded)
        if hashlib.sha256(content).hexdigest() != b["sha256"]:
            raise PackageError("a blob does not match its hash")
        ref = blobs.put(content)
        b["storage_path"] = ref.storage_path

    async def put(conn: Any, table: sa.Table, rows: list[dict[str, Any]], name: str) -> None:
        inserted = 0
        for row in rows:
            pk = next(iter(table.primary_key.columns))
            stmt = pg_insert(table).values(**row).on_conflict_do_nothing().returning(pk)
            inserted += 1 if (await conn.execute(stmt)).first() is not None else 0
        report.inserted[name] = inserted
        report.skipped_existing[name] = len(rows) - inserted

    async with engine.begin() as conn:  # all-or-nothing
        await put(conn, blob_t, blob_rows, "blobs")
        await put(conn, record_t, records, "evidence_records")
        await put(conn, item_t, items, "knowledge_items")
        await put(conn, provenance_t, prov, "provenance")
        await put(conn, memory_t, memory, "memory_items")
        await put(conn, decision_t, decisions, "decisions")
        for old, new in supersessions.items():
            await conn.execute(
                sa.update(decision_t).where(decision_t.c.id == old).values(superseded_by=new)
            )
    if registry is not None:
        load = LoadResult()
        reg = payload.get("registry", {})
        for entry in [*reg.get("skills", []), *reg.get("agents", [])]:
            try:
                load.manifests.append(("package", parse_document(entry["manifest"], "package")))
            except Exception as exc:  # a bad entry must not block the rest
                report.rejected.append(str(exc)[:200])
        sync = await registry.sync(load, origin=Origin.PLUGIN)
        report.registered = [*sync.added, *sync.updated]
        report.rejected += [f"{i}: {r}" for i, r in sync.rejected]
    return report
