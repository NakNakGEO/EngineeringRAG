from __future__ import annotations

import pytest

from eios_domain.registry import (
    detects_external_database_intent,
    find_external_database_intent,
    is_forbidden_capability,
)
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


# ---- widened detector: structure, scoping and the "never loosen" guarantee ----
@pytest.mark.parametrize(
    ("text", "excerpt"),
    [
        ("read orders directly from the company production Oracle database", "read orders"),
        ("Please connect to prod", "connect to prod"),
        ("external_db_connector", "external db connector"),
    ],
)
def test_the_matched_excerpt_is_reported(text: str, excerpt: str) -> None:
    found = find_external_database_intent(text)
    assert found is not None and found.startswith(excerpt)
    assert len(found) <= 160


def test_clean_text_has_no_excerpt() -> None:
    assert find_external_database_intent("parse DDL files and explain the indexes") is None


def test_text_only_wording_scopes_to_its_own_clause() -> None:
    allowed = "generate SQL as text for a human to run against the production database"
    assert not detects_external_database_intent(allowed)
    # the same wording does not excuse a separate, live action in the same request
    assert detects_external_database_intent(f"{allowed} and connect to the production database")
    assert detects_external_database_intent(f"{allowed}, then query the QA database for rows")
    assert detects_external_database_intent(f"{allowed}. Also pull orders from the prod Oracle db")


@pytest.mark.parametrize(
    "marker",
    ["directly", "automatically", "on its own", "without a human", "straight"],
)
def test_unattended_wording_voids_the_text_only_exemption(marker: str) -> None:
    assert detects_external_database_intent(
        f"as text for a human, but run the sql {marker} against the prod database"
    )


def test_the_gap_between_verb_and_target_stops_at_a_new_purpose() -> None:
    # "to understand" starts a new clause: the Oracle docs are not a live target
    assert not detects_external_database_intent("read the docs from the repo to understand oracle")
    assert not detects_external_database_intent("pull the notes from the wiki for the oracle team")


@pytest.mark.parametrize("verb", ["connect to", "log into", "run sql against", "query", "dump"])
@pytest.mark.parametrize(
    "noun", ["database", "db", "sql server", "oracle", "postgres", "mysql", "mongodb", "redis"]
)
def test_original_verb_and_noun_pairs_are_never_unblocked(verb: str, noun: str) -> None:
    assert detects_external_database_intent(f"{verb} the company {noun}")
    assert detects_external_database_intent(f"We should {verb} the {noun} tonight")


def test_vendor_words_alone_do_not_trigger() -> None:
    assert not detects_external_database_intent("Oracle")
    assert not detects_external_database_intent("compare redis and postgres for the cache layer")


@pytest.mark.parametrize(
    "capability",
    ["external_db_connector", "oracle_reader", "SqlServerClient", "run_sql", "modify_root_policy"],
)
def test_forbidden_capability_names_ignore_casing_separators_and_order(capability: str) -> None:
    assert is_forbidden_capability(capability)
    assert is_forbidden_capability(capability.lower())
    assert is_forbidden_capability(capability.replace("_", "-"))
    assert is_forbidden_capability(capability.replace("_", "."))


@pytest.mark.parametrize(
    "capability", ["externaldbconnector", "oraclereader", "sqlserverclient", "databaseconnector"]
)
def test_run_together_forbidden_names_are_caught_too(capability: str) -> None:
    assert is_forbidden_capability(capability)


@pytest.mark.parametrize(
    "capability", ["sql_analyze", "sql_generate", "parse_ddl", "database_docs", "policy_explain"]
)
def test_text_oriented_capability_names_are_allowed(capability: str) -> None:
    assert not is_forbidden_capability(capability)
