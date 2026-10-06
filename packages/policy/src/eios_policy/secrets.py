"""Secrets broker: tools get only the secrets their manifest names; DB credentials never.

Secrets come from the process environment (``EIOS_SECRET_<NAME>``); nothing is persisted by
Engineering OS. Names and values that look like database connection material are refused outright
(Root Rule: no external database credentials are ever stored or handed out).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_DB_NAME = re.compile(
    r"(database|\bdb[_-]|[_-]db\b|dsn|connection[_-]?string|conn[_-]?str|jdbc|odbc|postgres|mysql|"
    r"mariadb|mssql|sqlserver|oracle|mongo|redis|cassandra|snowflake|sqlite)",
    re.IGNORECASE,
)
_DB_URL = re.compile(
    r"^\s*(postgres(ql)?(\+\w+)?|mysql(\+\w+)?|mariadb|mssql(\+\w+)?|sqlserver|oracle(\+\w+)?|"
    r"mongodb(\+srv)?|redis(s)?|jdbc:[a-z]+|cassandra|snowflake|clickhouse)://",
    re.IGNORECASE,
)
_DB_KV = re.compile(r"(server|data source|host)\s*=.*(password|pwd)\s*=", re.IGNORECASE | re.DOTALL)


class SecretError(Exception):
    """A secret request that was refused (not granted, unknown, or database material)."""


class SecretValue:
    """Opaque wrapper: repr/str never reveal the value."""

    __slots__ = ("_value", "name")

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"SecretValue(name={self.name!r}, value=[REDACTED])"

    __str__ = __repr__


def is_database_secret(name: str, value: str | None = None) -> bool:
    if _DB_NAME.search(name):
        return True
    return value is not None and bool(_DB_URL.search(value) or _DB_KV.search(value))


class SecretsBroker:
    PREFIX = "EIOS_SECRET_"

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._env = environ if environ is not None else os.environ

    def names(self) -> list[str]:
        """Names of secrets available (never values, never database-looking ones)."""
        return sorted(
            k[len(self.PREFIX) :]
            for k, v in self._env.items()
            if k.startswith(self.PREFIX) and not is_database_secret(k[len(self.PREFIX) :], v)
        )

    def get(self, name: str, *, granted: Iterable[str]) -> SecretValue:
        if not _NAME.match(name):
            raise SecretError("invalid secret name")
        if name not in set(granted):
            raise SecretError(f"secret '{name}' is not granted to this tool")
        if is_database_secret(name):
            raise SecretError("database credentials are never provided to tools")
        value = self._env.get(self.PREFIX + name)
        if value is None:
            raise SecretError(f"secret '{name}' is not configured")
        if is_database_secret(name, value):
            raise SecretError("value looks like database connection material; refused")
        return SecretValue(name, value)
