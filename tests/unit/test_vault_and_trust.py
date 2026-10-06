from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from pydantic import ValidationError

from eios_domain.ids import utcnow
from eios_domain.knowledge import (
    DecisionCreate,
    EvidenceCreate,
    Health,
    KnowledgeItemCreate,
    MemoryCreate,
    ProvenanceInput,
    SourceKind,
    Trust,
)
from eios_domain.vault import Vault, VaultViolationError, check_vault_invariants

PID = uuid.uuid4()
PROV = [ProvenanceInput(source="tool:x", actor="tool")]


def _item(**kw: object) -> KnowledgeItemCreate:
    base: dict[str, object] = {
        "vault": Vault.PROJECT,
        "project_id": PID,
        "title": "t",
        "content": "c",
        "source_kind": SourceKind.MANUAL,
        "created_by": "me",
    }
    base.update(kw)
    return KnowledgeItemCreate(**base)  # type: ignore[arg-type]


def test_only_the_default_vault_is_exportable() -> None:
    assert [v for v in Vault if v.exportable] == [Vault.DEFAULT]


@pytest.mark.parametrize(
    ("vault", "project", "expires"),
    [
        (Vault.PROJECT, None, None),  # project data without a project
        (Vault.DEFAULT, PID, None),  # company data in the portable vault
        (Vault.EPHEMERAL, None, None),  # ephemeral without expiry
        (Vault.EPHEMERAL, None, utcnow() - timedelta(seconds=1)),  # already expired
        (Vault.DEFAULT, None, utcnow() + timedelta(days=1)),  # default does not expire
    ],
)
def test_vault_invariants_reject_inconsistent_classification(vault, project, expires) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(VaultViolationError):
        check_vault_invariants(vault, project_id=project, expires_at=expires)


def test_valid_classifications_pass() -> None:
    check_vault_invariants(Vault.PROJECT, project_id=PID, expires_at=None)
    check_vault_invariants(Vault.DEFAULT, project_id=None, expires_at=None)
    check_vault_invariants(
        Vault.EPHEMERAL, project_id=None, expires_at=utcnow() + timedelta(hours=1)
    )


def test_trust_ordering() -> None:
    assert Trust.RAW < Trust.OBSERVED < Trust.DERIVED < Trust.VERIFIED < Trust.APPROVED
    assert Trust.DERIVED <= Trust.DERIVED and not Trust.VERIFIED <= Trust.DERIVED


@pytest.mark.parametrize("source", [SourceKind.TOOL, SourceKind.LLM, SourceKind.RESEARCH])
@pytest.mark.parametrize("trust", [Trust.DERIVED, Trust.VERIFIED, Trust.APPROVED])
def test_raw_tool_and_llm_output_cannot_be_created_above_observed(source, trust) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError, match="cannot be created with trust"):
        _item(source_kind=source, trust=trust, provenance=PROV)


@pytest.mark.parametrize("source", [SourceKind.TOOL, SourceKind.LLM, SourceKind.CODE])
def test_verified_is_never_creatable_for_machine_sources(source) -> None:  # type: ignore[no-untyped-def]
    for from_pipeline in (False, True):
        with pytest.raises(ValidationError):
            _item(
                source_kind=source,
                trust=Trust.VERIFIED,
                provenance=PROV,
                from_pipeline=from_pipeline,
            )


def test_pipeline_raises_the_ceiling_to_derived_only() -> None:
    item = _item(
        source_kind=SourceKind.TOOL, trust=Trust.DERIVED, provenance=PROV, from_pipeline=True
    )
    assert item.trust is Trust.DERIVED


def test_non_manual_knowledge_requires_provenance() -> None:
    for source in (SourceKind.TOOL, SourceKind.LLM, SourceKind.CODE, SourceKind.RESEARCH):
        with pytest.raises(ValidationError, match="provenance"):
            _item(source_kind=source, trust=Trust.RAW)
    assert _item(source_kind=SourceKind.MANUAL).provenance == []


def test_raw_knowledge_cannot_be_current() -> None:
    with pytest.raises(ValidationError, match="CURRENT"):
        _item(source_kind=SourceKind.LLM, provenance=PROV, health=Health.CURRENT)


def test_other_models_enforce_vault_rules() -> None:
    with pytest.raises(ValidationError):
        EvidenceCreate(vault=Vault.PROJECT)  # no project_id
    with pytest.raises(ValidationError):
        EvidenceCreate()  # ephemeral default needs expiry
    with pytest.raises(ValidationError):
        MemoryCreate(vault=Vault.DEFAULT, project_id=PID, content="x", created_by="a")
    with pytest.raises(ValidationError, match="ephemeral"):
        DecisionCreate(vault=Vault.EPHEMERAL, question="q", decider="d")
