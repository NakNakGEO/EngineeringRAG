"""The text analyzers: document_parse, log_analyze, api_contract_analyze, performance_profile."""

from __future__ import annotations

import json

import pytest

from eios_runtime.analyzers import (
    AnalysisError,
    api_contract_analyze,
    document_parse,
    log_analyze,
    performance_profile,
)


def test_documents_of_each_supported_kind() -> None:
    assert (
        document_parse(
            {"document": "# Title\n\n## Part\nsee [x](https://example.com)", "format": "md"}
        )["headings"][1]["title"]
        == "Part"
    )
    html = document_parse(
        {
            "document": "<html><head><title>T</title></head>"
            "<body><h1>Hi</h1><p>text</p></body></html>"
        }
    )
    assert html["format"] == "html" and html["title"] == "T" and "text" in html["text"]
    assert document_parse({"document": '{"a": 1, "b": [1, 2]}'})["top_level_keys"] == ["a", "b"]
    assert document_parse({"document": "name,qty\napple,3\npear,5", "format": "csv"})["rows"] == 2
    assert document_parse({"document": "plain words here"})["words"] == 3


def test_xml_is_summarised_and_entities_are_refused() -> None:
    xml = (
        '<?xml version="1.0"?><catalog xmlns:x="urn:x">'
        '<book id="b1"><title>One</title></book><book id="b2"><title>Two</title></book></catalog>'
    )
    r = document_parse({"document": xml})
    assert (r["format"], r["root"], r["elements"], r["max_depth"]) == ("xml", "catalog", 5, 3)
    assert r["element_counts"]["book"] == 2 and r["namespaces"] == {"xmlns:x": "urn:x"}
    assert r["top_level"] == ['book id="b1"', 'book id="b2"'] and r["text"] == "One Two"
    with pytest.raises(AnalysisError, match="entity"):
        document_parse({"document": '<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>', "format": "xml"})
    with pytest.raises(AnalysisError, match="well-formed"):
        document_parse({"document": "<a><b></a>", "format": "xml"})


def test_documents_that_need_a_parser_library_are_refused_not_guessed() -> None:
    with pytest.raises(AnalysisError, match="not installed"):
        document_parse({"document": "%PDF-1.7", "format": "pdf"})
    with pytest.raises(AnalysisError):
        document_parse({"document": "   "})


def test_log_levels_repeats_errors_and_span() -> None:
    log = "\n".join(
        [
            "2026-10-14 10:00:00 INFO started job 17",
            "2026-10-14 10:00:01 INFO started job 18",
            "2026-10-14 10:00:02 ERROR failed to read file 0x1f",
            "2026-10-14 10:05:00 WARNING slow query 1200 ms",
            "Traceback (most recent call last):",
        ]
    )
    r = log_analyze({"logs": log})
    assert r["levels"] == {"INFO": 2, "ERROR": 1, "WARN": 1}
    assert (
        r["first_timestamp"] == "2026-10-14 10:00:00"
        and r["last_timestamp"] == "2026-10-14 10:05:00"
    )
    assert r["top_messages"][0] == {"message": "started job #", "count": 2}
    assert r["errors"][0]["line"] == 3 and r["stack_trace_blocks"] == 1


OLD = {
    "openapi": "3.0.0",
    "info": {"title": "T", "version": "1"},
    "paths": {
        "/a": {
            "get": {
                "parameters": [{"name": "q", "required": False}],
                "responses": {"200": {}, "404": {}},
            }
        },
        "/b": {"post": {"responses": {"201": {}}}},
    },
}
NEW = {
    "openapi": "3.0.0",
    "info": {"title": "T", "version": "2"},
    "paths": {
        "/a": {"get": {"parameters": [{"name": "q", "required": True}], "responses": {"200": {}}}},
        "/c": {"get": {"responses": {"200": {}}}},
    },
    "components": {"securitySchemes": {"bearer": {}}, "schemas": {"X": {}}},
}


def test_api_contract_summary_and_breaking_changes() -> None:
    r = api_contract_analyze({"contract": json.dumps(NEW), "previous": json.dumps(OLD)})
    assert (r["operations"], r["schemas"], r["auth_schemes"]) == (2, 1, ["bearer"])
    cmp = r["compared_with_previous"]
    assert (
        cmp["added"] == ["GET /c"] and cmp["removed"] == ["POST /b"] and cmp["compatible"] is False
    )
    assert "removed operation POST /b" in cmp["breaking"]
    assert "GET /a: parameter 'q' is now required" in cmp["breaking"]
    assert "GET /a: response 404 was removed" in cmp["breaking"]


def test_api_contract_rejects_other_documents() -> None:
    with pytest.raises(AnalysisError):
        api_contract_analyze({"contract": json.dumps({"hello": "world"})})


def test_performance_from_samples_and_from_text() -> None:
    r = performance_profile(
        {
            "samples": [
                {"name": "db", "ms": 10},
                {"name": "db", "ms": 30},
                {"name": "render", "ms": 5},
            ]
        }
    )
    assert r["samples"] == 3 and r["slowest_by_total"][0]["name"] == "db" and r["max_ms"] == 30
    t = performance_profile({"trace": "load config: 2ms\nquery users = 1.5 s\nnot a timing line"})
    assert t["samples"] == 2 and t["slowest_by_total"][0]["name"] == "query users"
    with pytest.raises(AnalysisError):
        performance_profile({"trace": "nothing here"})
