"""Static scan of generated tool source: defence in depth *before* the sandbox.

This does not make untrusted code safe - the sandbox and the Policy Engine do that. It rejects
code that is plainly reaching for what Root Policy forbids (network, processes, databases,
dynamic execution, the policy files themselves) so such code never even reaches the sandbox.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

MAX_SOURCE_BYTES = 200_000
FORBIDDEN_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "urllib",
        "urllib3",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "asyncio.subprocess",
        "subprocess",
        "multiprocessing",
        "ctypes",
        "cffi",
        "pty",
        "pexpect",
        "paramiko",
        "fabric",
        "psycopg",
        "psycopg2",
        "sqlalchemy",
        "sqlite3",
        "pymysql",
        "mysql",
        "pyodbc",
        "pymssql",
        "cx_Oracle",
        "oracledb",
        "pymongo",
        "motor",
        "redis",
        "cassandra",
        "snowflake",
        "clickhouse_driver",
        "asyncpg",
        "aiomysql",
        "importlib",
        "runpy",
        "pickle",
        "marshal",
        "shelve",
    }
)
FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "__import__", "breakpoint", "input"})
FORBIDDEN_OS_ATTRS = frozenset(
    {
        "system",
        "popen",
        "fork",
        "forkpty",
        "kill",
        "killpg",
        "setuid",
        "setgid",
        "chroot",
        "execv",
        "execve",
        "execl",
        "execlp",
        "execvp",
        "spawnl",
        "spawnv",
        "putenv",
        "environ",
    }
)
FORBIDDEN_DUNDERS = frozenset(
    {
        "__subclasses__",
        "__globals__",
        "__builtins__",
        "__code__",
        "__bases__",
        "__mro__",
        "__loader__",
        "__spec__",
    }
)
FORBIDDEN_STRINGS = ("root_policy", "/etc/", "/proc/", ".ssh", "eios_", "EIOS_", "postgresql://")


@dataclass
class ScanReport:
    ok: bool = True
    findings: list[str] = field(default_factory=list)

    def add(self, line: int, message: str) -> None:
        self.ok = False
        self.findings.append(f"line {line}: {message}")


def scan_python(source: str) -> ScanReport:
    report = ScanReport()
    if len(source.encode()) > MAX_SOURCE_BYTES:
        report.add(0, f"source exceeds {MAX_SOURCE_BYTES} bytes")
        return report
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        report.add(exc.lineno or 0, f"syntax error: {exc.msg}")
        return report
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES or alias.name in FORBIDDEN_MODULES:
                    report.add(line, f"import of '{alias.name}' is forbidden")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.split(".")[0] in FORBIDDEN_MODULES or mod in FORBIDDEN_MODULES:
                report.add(line, f"import from '{mod}' is forbidden")
            if mod == "os" and any(a.name in FORBIDDEN_OS_ATTRS for a in node.names):
                report.add(line, "importing process/environment helpers from os is forbidden")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in FORBIDDEN_CALLS:
                report.add(line, f"call to '{fn.id}' is forbidden")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_DUNDERS:
                report.add(line, f"access to '{node.attr}' is forbidden")
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr in FORBIDDEN_OS_ATTRS
            ):
                report.add(line, f"os.{node.attr} is forbidden")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for needle in FORBIDDEN_STRINGS:
                if needle in node.value:
                    report.add(line, f"string literal references '{needle}'")
    return report
