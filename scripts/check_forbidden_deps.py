"""Fail the build if an external-database driver (or a V1-excluded technology) appears.

Root Policy: Engineering OS must never connect to an external/company/QA/production database.
The runtime database policy (``eios_core.database_policy``) blocks such connections at startup;
this check is the supply-chain half: the drivers needed to make such connections must not even
be installed. It scans

* every ``pyproject.toml`` (declared dependencies, including dependency groups),
* ``uv.lock`` (the full resolved tree, so transitive additions are caught), and
* Python sources (``import`` / ``from ... import`` of a forbidden module).

``psycopg`` (v3) is the single allowed driver: it talks only to the internal PostgreSQL, enforced
by the runtime policy. ``psycopg2`` and every other driver are forbidden.

Usage: ``python scripts/check_forbidden_deps.py [ROOT]`` (exit code 1 on violations).
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Distribution names, PEP 503-normalised (lowercase, runs of -_. collapsed to "-").
EXTERNAL_DATABASE_DISTRIBUTIONS: frozenset[str] = frozenset(
    {
        # SQL Server / Sybase
        "pyodbc", "aioodbc", "pymssql", "python-tds", "pytds", "sqlalchemy-pytds", "turbodbc",
        # MySQL / MariaDB
        "mysqlclient", "pymysql", "mysql-connector-python", "mysql-connector", "aiomysql",
        "asyncmy", "mariadb",
        # Oracle
        "oracledb", "cx-oracle", "cx_oracle",
        # other PostgreSQL drivers (the internal DB uses psycopg 3 only)
        "psycopg2", "psycopg2-binary", "asyncpg", "pg8000", "pygresql",
        # other relational / warehouse / NoSQL clients
        "ibm-db", "ibm-db-sa", "teradatasql", "snowflake-connector-python",
        "snowflake-sqlalchemy", "clickhouse-driver", "clickhouse-connect",
        "databricks-sql-connector",
        "google-cloud-bigquery", "cassandra-driver", "pymongo", "motor", "couchbase",
        "elasticsearch", "opensearch-py", "sqlalchemy-bigquery",
    }
)  # fmt: skip

# Explicitly excluded from V1 by the master architecture (Non-Goals) unless the owner changes it.
V1_EXCLUDED_DISTRIBUTIONS: frozenset[str] = frozenset(
    {
        "redis", "aioredis", "kafka-python", "aiokafka", "confluent-kafka", "neo4j",
        "qdrant-client", "kubernetes", "nats-py",
    }
)  # fmt: skip

# Top-level import names.
FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        "pyodbc", "aioodbc", "pymssql", "pytds", "turbodbc", "MySQLdb", "pymysql", "mysql",
        "aiomysql", "asyncmy", "mariadb", "oracledb", "cx_Oracle", "psycopg2", "asyncpg",
        "pg8000", "pgdb", "ibm_db", "ibm_db_dbi", "teradatasql", "snowflake", "clickhouse_driver",
        "clickhouse_connect", "databricks", "cassandra", "pymongo", "motor", "couchbase",
        "elasticsearch", "opensearchpy", "redis", "aioredis", "kafka", "aiokafka",
        "confluent_kafka", "neo4j", "qdrant_client", "kubernetes", "nats",
    }
)  # fmt: skip

_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"}
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


@dataclass(frozen=True)
class Violation:
    path: str
    kind: str
    name: str

    def __str__(self) -> str:
        return f"{self.path}: forbidden {self.kind} '{self.name}'"


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


_FORBIDDEN_NORMALISED = {
    normalise(n): "external database driver" for n in EXTERNAL_DATABASE_DISTRIBUTIONS
} | {normalise(n): "V1-excluded dependency" for n in V1_EXCLUDED_DISTRIBUTIONS}


def _check_requirement(spec: object, path: str) -> list[Violation]:
    if not isinstance(spec, str):
        return []
    match = _NAME.match(spec)
    if match is None:
        return []
    kind = _FORBIDDEN_NORMALISED.get(normalise(match.group(1)))
    return [Violation(path, kind, match.group(1))] if kind else []


def _scan_pyproject(path: Path, root: Path) -> list[Violation]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    rel = str(path.relative_to(root))
    specs: list[object] = list(data.get("project", {}).get("dependencies", []))
    for extra in data.get("project", {}).get("optional-dependencies", {}).values():
        specs.extend(extra)
    for group in data.get("dependency-groups", {}).values():
        specs.extend(group)
    specs.extend(data.get("tool", {}).get("uv", {}).get("dev-dependencies", []))
    out: list[Violation] = []
    for spec in specs:
        out.extend(_check_requirement(spec, rel))
    return out


def _scan_lock(path: Path, root: Path) -> list[Violation]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    rel = str(path.relative_to(root))
    out: list[Violation] = []
    for package in data.get("package", []):
        kind = _FORBIDDEN_NORMALISED.get(normalise(str(package.get("name", ""))))
        if kind:
            out.append(Violation(rel, kind, str(package["name"])))
    return out


def _scan_source(path: Path, root: Path) -> list[Violation]:
    rel = str(path.relative_to(root))
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return [Violation(rel, "unparseable source", path.name)]
    out: list[Violation] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names = [node.args[0].value]  # importlib.import_module("pyodbc")
        for name in names:
            if name.split(".")[0] in FORBIDDEN_MODULES:
                out.append(Violation(rel, "import of external database/V1-excluded module", name))
    return out


def _walk(root: Path, pattern: str) -> list[Path]:
    return sorted(
        p
        for p in root.rglob(pattern)
        if p.is_file() and not any(part in _SKIP_DIRS for part in p.relative_to(root).parts)
    )


def scan(root: Path) -> list[Violation]:
    """Return every violation found under ``root``."""
    violations: list[Violation] = []
    for pyproject in _walk(root, "pyproject.toml"):
        violations.extend(_scan_pyproject(pyproject, root))
    lock = root / "uv.lock"
    if lock.is_file():
        violations.extend(_scan_lock(lock, root))
    for source in _walk(root, "*.py"):
        violations.extend(_scan_source(source, root))
    return violations


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent.parent
    violations = scan(root)
    if violations:
        print("Root Policy violation: forbidden dependencies/imports found:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1
    print("forbidden-dependency check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
