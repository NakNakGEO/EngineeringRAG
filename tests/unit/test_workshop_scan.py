from __future__ import annotations

import pytest

from eios_domain.registry import detects_external_database_intent
from eios_workshop import scan_python

GOOD = """
import json
import sys

data = json.load(sys.stdin)
print(json.dumps({"echo": data, "n": len(data)}))
"""


def test_clean_stdlib_tool_passes() -> None:
    report = scan_python(GOOD)
    assert report.ok and report.findings == []


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        ("import socket\n", "socket"),
        ("import subprocess\nsubprocess.run(['ls'])\n", "subprocess"),
        ("from psycopg import connect\n", "psycopg"),
        ("import sqlalchemy\n", "sqlalchemy"),
        ("import pymysql\n", "pymysql"),
        ("import redis\n", "redis"),
        ("import requests\n", "requests"),
        ("import ctypes\n", "ctypes"),
        ("import os\nos.system('id')\n", "os.system"),
        ("from os import popen\n", "os"),
        ("eval('1+1')\n", "eval"),
        ("exec('x=1')\n", "exec"),
        ("__import__('socket')\n", "__import__"),
        ("x = ().__class__.__bases__\n", "__bases__"),
        ("open('/etc/passwd').read()\n", "/etc/"),
        ("open('policy/root_policy.yaml', 'w')\n", "root_policy"),
        ("print('postgresql://u:p@db/x')\n", "postgresql://"),
        ("import os\nprint(os.environ)\n", "os.environ"),
        ("def f(:\n", "syntax"),
    ],
)
def test_dangerous_sources_are_rejected(source: str, needle: str) -> None:
    report = scan_python(source)
    assert not report.ok
    assert any(needle in f for f in report.findings), report.findings


def test_oversized_source_is_rejected() -> None:
    assert not scan_python("x = 1\n" * 100_000).ok


@pytest.mark.parametrize(
    "text",
    [
        "connect to the production database and read orders",
        "Run this query against the QA database",
        "execute sql on the company postgres",
        "dump the customer database",
        "access the corporate Oracle server",
        "log into the sql server and update rows",
    ],
)
def test_external_database_intent_is_detected(text: str) -> None:
    assert detects_external_database_intent(text)


@pytest.mark.parametrize(
    "text",
    [
        "analyze SQL text and generate a migration script as text for a human to run",
        "parse SQL files for table references",
        "Generate rollback SQL from this DDL file",
        "Explain what a database index is",
        "summarize the retry policy",
    ],
)
def test_text_only_sql_work_is_not_blocked(text: str) -> None:
    assert not detects_external_database_intent(text)
