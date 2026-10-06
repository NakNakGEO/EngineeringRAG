from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from eios_knowledge.blob_store import BlobError, LocalBlobStore
from eios_knowledge.embeddings import HashingEmbedder
from eios_knowledge.text import tokenize


def test_tokenizer_splits_identifiers_and_keeps_originals() -> None:
    assert tokenize("GetUserById") == ["getuserbyid", "get", "user", "by", "id"]
    assert tokenize("parse_http_header") == ["parse", "http", "header"]
    assert tokenize("HTTPServer2") == ["httpserver2", "http", "server2"]
    assert tokenize("") == []


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_embedder_is_deterministic_normalised_and_fixed_dim() -> None:
    e = HashingEmbedder()
    [a1, a2] = await e.embed(["retry the failed payment", "retry the failed payment"])
    assert a1 == a2 and len(a1) == e.dim == 256
    assert math.isclose(math.sqrt(sum(x * x for x in a1)), 1.0, rel_tol=1e-9)
    assert await e.embed(["x"]) == await e.embed(["x"])


async def test_similar_text_is_closer_than_unrelated_text() -> None:
    e = HashingEmbedder()
    q, near, far = await e.embed(
        [
            "how does the payment retry policy work",
            "payment retry policy: failed payments are retried three times",
            "kubernetes ingress tls certificate rotation",
        ]
    )
    assert _cos(q, near) > _cos(q, far) + 0.2


async def test_empty_text_gives_zero_vector_not_nan() -> None:
    [v] = await HashingEmbedder().embed([""])
    assert all(x == 0.0 for x in v)


def test_blob_roundtrip_dedup_and_layout(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    ref = store.put(b"hello")
    assert ref.sha256 == hashlib.sha256(b"hello").hexdigest() and ref.size_bytes == 5
    assert ref.storage_path == f"{ref.sha256[:2]}/{ref.sha256[2:4]}/{ref.sha256}"
    assert store.put(b"hello") == ref  # idempotent / deduplicated
    assert store.get(ref.sha256) == b"hello" and store.exists(ref.sha256)
    assert store.delete(ref.sha256) and not store.delete(ref.sha256)
    assert not list(tmp_path.rglob(".tmp-*"))


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a" * 63, "A" * 64, "", "../" + "a" * 64])
def test_blob_ids_are_validated_against_traversal(tmp_path: Path, bad: str) -> None:
    store = LocalBlobStore(tmp_path)
    for op in (store.get, store.exists, store.delete):
        with pytest.raises(BlobError):
            op(bad)


def test_corrupted_blob_is_detected(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    ref = store.put(b"original")
    (tmp_path / ref.storage_path).write_bytes(b"tampered")
    with pytest.raises(BlobError, match="corrupted"):
        store.get(ref.sha256)


def test_missing_blob(tmp_path: Path) -> None:
    with pytest.raises(BlobError, match="not found"):
        LocalBlobStore(tmp_path).get("0" * 64)
