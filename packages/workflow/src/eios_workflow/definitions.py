"""Workflow definitions are YAML data, validated by the domain models."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from eios_domain.workflow import WorkflowDefinition

MAX_BYTES = 200_000


class DefinitionError(Exception):
    pass


def load_definitions(directory: Path) -> tuple[dict[str, WorkflowDefinition], list[str]]:
    """Load every ``*.yaml`` file; returns (definitions by id, error messages)."""
    found: dict[str, WorkflowDefinition] = {}
    errors: list[str] = []
    if not directory.is_dir():
        return found, errors
    for path in sorted(directory.glob("*.y*ml")):
        if path.is_symlink() or path.stat().st_size > MAX_BYTES:
            errors.append(f"{path.name}: skipped (symlink or too large)")
            continue
        try:
            definition = WorkflowDefinition.model_validate(
                yaml.safe_load(path.read_text(encoding="utf-8"))
            )
        except (yaml.YAMLError, ValidationError, UnicodeDecodeError) as exc:
            errors.append(f"{path.name}: {str(exc)[:300]}")
            continue
        if definition.id in found:
            errors.append(f"{path.name}: duplicate workflow id {definition.id}")
            continue
        found[definition.id] = definition
    return found, errors
