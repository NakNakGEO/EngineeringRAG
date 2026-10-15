"""Root Policy: external database isolation (supply-chain half)."""

from __future__ import annotations

from pathlib import Path

import pytest
from check_forbidden_deps import scan

pytestmark = pytest.mark.security

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_repository_is_clean() -> None:
    assert scan(REPO_ROOT) == []


def test_allowed_driver_psycopg3_is_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path, "pyproject.toml", '[project]\nname="x"\ndependencies=["psycopg[binary]>=3.2"]\n'
    )
    _write(tmp_path, "app.py", "import psycopg\n")
    assert scan(tmp_path) == []


@pytest.mark.parametrize(
    "requirement",
    [
        "pyodbc>=5",
        "PyMSSQL",
        "mysqlclient==2.2",
        "oracledb",
        "psycopg2-binary>=2.9",
        "psycopg2_binary",
        "asyncpg",
        "pymongo>=4",
        "redis>=5",
        "qdrant-client",
        "Cx_Oracle",
    ],
)
def test_forbidden_dependency_declared_in_pyproject(tmp_path: Path, requirement: str) -> None:
    _write(tmp_path, "pyproject.toml", f'[project]\nname="x"\ndependencies=["{requirement}"]\n')
    assert scan(tmp_path), requirement


def test_forbidden_dependency_in_dependency_group(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", '[project]\nname="x"\n[dependency-groups]\ndev=["pyodbc"]\n')
    assert scan(tmp_path)


def test_forbidden_dependency_in_nested_workspace_member(tmp_path: Path) -> None:
    _write(tmp_path, "packages/p/pyproject.toml", '[project]\nname="p"\ndependencies=["pymysql"]\n')
    assert scan(tmp_path)


def test_transitive_forbidden_dependency_found_in_lockfile(tmp_path: Path) -> None:
    _write(tmp_path, "uv.lock", 'version = 1\n[[package]]\nname = "pyodbc"\nversion = "5.0"\n')
    violations = scan(tmp_path)
    assert [v.name for v in violations] == ["pyodbc"]


@pytest.mark.parametrize(
    "source",
    [
        "import pyodbc",
        "import pymssql as m",
        "from MySQLdb import connect",
        "from oracledb import connect",
        "import psycopg2.extras",
        "import asyncpg",
        'import importlib\nimportlib.import_module("pyodbc")',
    ],
)
def test_forbidden_import_in_source(tmp_path: Path, source: str) -> None:
    _write(tmp_path, "src/mod.py", source + "\n")
    assert scan(tmp_path), source


def test_ignored_directories_are_not_scanned(tmp_path: Path) -> None:
    _write(tmp_path, ".venv/lib/x.py", "import pyodbc\n")
    assert scan(tmp_path) == []
