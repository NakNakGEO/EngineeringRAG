"""Tool onboarding, step one: download a repository to *look* at it. It is never run.

The archive is fetched through the same allowlisted, public-addresses-only path as web research,
read in memory (never extracted to disk), scanned with ``static_scan`` and thrown away; what is
kept is a report plus the archive's hash and size. A human then marks the candidate reviewed or
rejected. Running it (in the Workshop sandbox, with a wrapper and tests) is a separate, human-gated
step that this service does not take.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
import uuid
import zipfile
from typing import Any
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.ids import new_id, utcnow
from eios_runtime.static_scan import MAX_FILE_BYTES, scan_files
from eios_runtime.web_research import WebResearchError, fetch_bytes
from eios_storage.tables.platform import tool_candidate as candidate_t

MAX_ARCHIVE_BYTES = 40_000_000
MAX_UNPACKED_BYTES = 250_000_000
MAX_MEMBERS = 20_000
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_REF = re.compile(r"^[A-Za-z0-9_./-]{1,100}$")
STATES = ("quarantined", "reviewed", "rejected")


class OnboardingError(Exception):
    pass


def parse_repo_url(url: str) -> tuple[str, str]:
    """``https://github.com/owner/name[.git][/...]`` -> (owner, name). Only GitHub is understood."""
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or (parts.hostname or "").lower() != "github.com":
        raise OnboardingError("only https://github.com/<owner>/<repository> addresses are understood")
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) < 2:
        raise OnboardingError("the address must name an owner and a repository")
    owner, name = segs[0], segs[1].removesuffix(".git")
    if not _REPO.match(owner) or not _REPO.match(name):
        raise OnboardingError("owner or repository name has unexpected characters")
    return owner, name


def inspect_archive(data: bytes) -> dict[str, Any]:
    """Scan a zip held in memory. Nothing is extracted; limits stop zip bombs."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise OnboardingError("the download is not a zip archive") from exc
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_MEMBERS:
        raise OnboardingError(f"archive has more than {MAX_MEMBERS} files")
    if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
        raise OnboardingError("archive unpacks to more than 250 MB")
    prefix = infos[0].filename.split("/", 1)[0] + "/" if infos else ""
    files: list[tuple[str, bytes]] = []
    readme = ""
    license_name = ""
    for info in infos:
        path = info.filename[len(prefix):] if info.filename.startswith(prefix) else info.filename
        if not path or ".." in path.split("/"):
            continue
        head = zf.open(info).read(MAX_FILE_BYTES + 1) if info.file_size <= MAX_FILE_BYTES else zf.open(info).read(16)
        files.append((path, head))
        low = path.lower()
        if not readme and low in {"readme.md", "readme", "readme.rst", "readme.txt"}:
            readme = head[:1500].decode("utf-8", errors="replace")
        if not license_name and low.split("/")[-1].startswith(("license", "licence", "copying")):
            license_name = path
    report = scan_files(files)
    report.update({"readme_start": readme, "license_file": license_name or None, "top_level": sorted({p.split("/")[0] for p, _ in files})[:40]})
    return report


class Onboarding:
    def __init__(self, engine: AsyncEngine, allow_host: Any) -> None:
        self._engine = engine
        self._allow = allow_host  # async (host) -> None, raises to refuse (policy + allowlist)

    async def inspect(self, url: str, ref: str = "HEAD", *, requested_by: str = "api") -> dict[str, Any]:
        owner, name = parse_repo_url(url)
        if not _REF.match(ref):
            raise OnboardingError("the branch / tag has unexpected characters")
        archive = f"https://github.com/{owner}/{name}/archive/{ref}.zip" if ref != "HEAD" else f"https://github.com/{owner}/{name}/archive/HEAD.zip"
        try:
            blob = await fetch_bytes(archive, allow_host=self._allow, max_bytes=MAX_ARCHIVE_BYTES, accept=("application/zip", "application/octet-stream", "application/x-zip-compressed"))
        except WebResearchError as exc:
            raise OnboardingError(str(exc)) from exc
        if blob["truncated"]:
            raise OnboardingError("the archive is larger than 40 MB; not inspected")
        report = await asyncio.to_thread(inspect_archive, blob["data"])  # CPU-bound: keep the api responsive
        row = {
            "id": new_id(), "source_url": f"https://github.com/{owner}/{name}", "ref": ref,
            "archive_sha256": hashlib.sha256(blob["data"]).hexdigest(), "archive_bytes": len(blob["data"]),
            "state": "quarantined", "report": report, "requested_by": requested_by[:200],
            "created_at": utcnow(), "decided_by": None, "decided_at": None, "note": "",
        }  # fmt: skip
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(candidate_t).values(**row))
        return _public(row)

    async def list(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sa.select(candidate_t).order_by(candidate_t.c.created_at.desc()).limit(limit))).all()
        return [_public(dict(r._mapping)) for r in rows]

    async def decide(self, candidate_id: uuid.UUID, state: str, by: str, note: str) -> dict[str, Any]:
        if state not in {"reviewed", "rejected"}:
            raise OnboardingError("state must be 'reviewed' or 'rejected'")
        async with self._engine.begin() as conn:
            result = await conn.execute(
                sa.update(candidate_t).where(candidate_t.c.id == candidate_id)
                .values(state=state, decided_by=by[:200], decided_at=utcnow(), note=note[:1000])
            )
            if not result.rowcount:
                raise OnboardingError("candidate not found")
            row = (await conn.execute(sa.select(candidate_t).where(candidate_t.c.id == candidate_id))).one()
        return _public(dict(row._mapping))


def _public(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["id"] = str(row["id"])
    for k in ("created_at", "decided_at"):
        out[k] = row[k].isoformat() if row[k] is not None else None
    return out
