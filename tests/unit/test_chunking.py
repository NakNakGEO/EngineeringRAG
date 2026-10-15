"""Chunking of documents, configuration, SQL and migrations (ADR 0018)."""

from __future__ import annotations

from itertools import pairwise

from eios_project_intelligence.chunking import (
    MAX_CHARS,
    MAX_CHUNKS_PER_FILE,
    MAX_LINES,
    ChunkResult,
    chunk_text,
    identifiers,
    is_identifier_like,
    is_lockfile,
    is_sensitive_path,
    wants_chunks,
)
from eios_project_intelligence.languages import detect_language


def _chunks(path: str, text: str) -> ChunkResult:
    return chunk_text(path, detect_language(path), text)


def test_xml_chunks_carry_the_element_path_like_markdown_headings() -> None:
    items = "\n".join(f'    <PackageReference Include="Pkg{n}" Version="1.0" />' for n in range(120))
    text = f"<Project>\n  <ItemGroup>\n{items}\n  </ItemGroup>\n</Project>\n"
    result = _chunks("App.csproj", text)
    assert len(result.chunks) > 1
    assert result.chunks[0].heading == "Project"
    assert any(
        c.heading.startswith('Project > ItemGroup > PackageReference Include="Pkg') for c in result.chunks[1:]
    )
    assert any("Pkg77" in c.idents for c in result.chunks)
    _bounded(result)


def _bounded(result: ChunkResult) -> None:
    for c in result.chunks:
        assert len(c.text) <= MAX_CHARS
        assert c.end_line - c.start_line + 1 <= MAX_LINES
        assert 1 <= c.start_line <= c.end_line


def _doc(sections: int, lines: int) -> str:
    out = ["# Title", ""]
    for n in range(sections):
        out += (
            [f"## Section {n}", ""] + [f"line {n}.{i} about topic{n}" for i in range(lines)] + [""]
        )
    return "\n".join(out)


def test_which_files_have_their_text_indexed() -> None:
    for path in (
        "docs/adr/0005-x.md",
        "config/app.yaml",
        "pyproject.toml",
        "Dockerfile",
        "Makefile",
        "db/schema.sql",
        "scripts/run.sh",
        "package.json",
        "policy/root_policy.lock",  # unknown type
        "migrations/versions/0002_runs.py",
        "services/api/alembic/versions/abc.py",
        "tests/unit/test_x.py",
        "web/src/test/format.test.ts",
    ):
        assert wants_chunks(path, detect_language(path)), path
    for path in ("src/app/service.py", "web/src/App.tsx", "src/main/Foo.java"):
        assert not wants_chunks(path, detect_language(path)), path
    assert not wants_chunks("tests/fixtures/logo.png", "binary")


def test_markdown_is_cut_at_headings_and_remembers_the_breadcrumb() -> None:
    text = (
        "# ADR 0008\n\nIntro line.\n\n"
        "## Identity\n\n" + "\n".join(f"identity fact {i}" for i in range(12)) + "\n\n"
        "### Fingerprint\n\nthe fingerprint is a hash\n\n"
        "## Overlays\n\n" + "\n".join(f"overlay fact {i}" for i in range(12)) + "\n"
    )
    result = _chunks("docs/adr/0008.md", text)
    headings = [c.heading for c in result.chunks]
    assert headings[0] == "ADR 0008" or headings[0].startswith("ADR 0008")
    assert any(h.endswith("Overlays") for h in headings)
    overlay = next(c for c in result.chunks if c.heading.endswith("Overlays"))
    assert "overlay fact 3" in overlay.text and "identity fact 3" not in overlay.text
    assert text.split("\n")[overlay.start_line - 1].startswith("## Overlays")
    _bounded(result)


def test_headings_inside_code_fences_are_not_headings() -> None:
    text = "# Real\n\n```bash\n# not a heading\necho hi\n```\n\n## Next\n\nbody\n"
    result = _chunks("README.md", text)
    assert all("not a heading" not in c.heading for c in result.chunks)


def test_long_sections_are_split_at_blank_lines_within_the_caps() -> None:
    result = _chunks("docs/long.md", _doc(sections=1, lines=400))
    assert len(result.chunks) > 5
    _bounded(result)
    # no line is lost or duplicated, and ranges are ordered and disjoint
    spans = [(c.start_line, c.end_line) for c in result.chunks]
    assert spans == sorted(spans)
    assert all(a[1] < b[0] for a, b in pairwise(spans))
    assert [c.ordinal for c in result.chunks] == list(range(len(result.chunks)))


def test_one_enormous_line_is_split_not_truncated() -> None:
    sentence = "word " * 700  # one 3 500 char line: longer than a chunk, shorter than "minified"
    result = _chunks("docs/prose.md", f"# T\n\n{sentence}\n\nafter\n")
    assert result.status == "chunked" and len(result.chunks) >= 2
    _bounded(result)
    assert sum(c.text.count("word") for c in result.chunks) == 700


def test_tiny_files_are_a_single_chunk_and_empty_files_none() -> None:
    assert len(_chunks("notes.md", "just one line\n").chunks) == 1
    assert _chunks("empty.md", "").chunks == []
    assert _chunks("blank.yaml", "\n\n\n").chunks == []


def test_yaml_is_cut_at_top_level_keys_and_compose_services() -> None:
    text = (
        "name: demo\n\nservices:\n"
        + "".join(
            f"  svc{n}:\n    image: img{n}\n" + "".join(f"    opt{i}: {i}\n" for i in range(12))
            for n in range(6)
        )
        + "\nvolumes:\n  data: {}\n"
    )
    result = _chunks("docker-compose.yml", text)
    headings = {c.heading for c in result.chunks}
    assert "name" in headings and "volumes" in headings
    assert any(h.startswith("services > svc") for h in headings)
    _bounded(result)


def test_yaml_comment_block_stays_with_the_key_below_it() -> None:
    text = "a: 1\n" * 8 + "# the retention policy\n# applies to events\nretention:\n  days: 30\n"
    result = _chunks("cfg.yaml", text)
    chunk = next(c for c in result.chunks if "retention:" in c.text)
    assert chunk.text.startswith("# the retention policy")
    assert chunk.heading == "retention"


def test_toml_dockerfile_makefile_shell_and_sql_have_their_own_seams() -> None:
    toml = "[project]\nname='x'\n" + "a=1\n" * 10 + "[tool.ruff]\nline-length=100\n" + "b=2\n" * 10
    assert [c.heading for c in _chunks("pyproject.toml", toml).chunks] == ["project", "tool.ruff"]

    docker = (
        "FROM python:3.12 AS builder\n" + "RUN echo build\n" * 10 + "\n"
        "FROM python:3.12-slim AS runtime\n" + "RUN echo run\n" * 10
    )
    assert [c.heading for c in _chunks("Dockerfile", docker).chunks] == [
        "stage builder",
        "stage runtime",
    ]

    make = (
        ".PHONY: test lint\n\ntest:\n\tpytest\n"
        + "\tx\n" * 8
        + "\nlint:\n\truff check\n"
        + "\ty\n" * 8
    )
    assert [c.heading for c in _chunks("Makefile", make).chunks][-2:] == ["test", "lint"]

    shell = "set -e\n" + "echo a\n" * 10 + "\nbackup() {\n  pg_dump\n" + "  step\n" * 10 + "}\n"
    assert _chunks("backup.sh", shell).chunks[-1].heading == "backup"

    sql = (
        "CREATE TABLE event (id int);\n" + "-- filler\n" * 9 + "\n"
        "-- trigger forbids updates\nCREATE TRIGGER trg_event_no_update BEFORE UPDATE ON event\n"
        "  FOR EACH ROW EXECUTE FUNCTION forbid();\n" + "-- more\n" * 9
    )
    chunks = _chunks("db/schema.sql", sql).chunks
    assert chunks[-1].heading.startswith("CREATE TRIGGER trg_event_no_update")
    assert chunks[-1].text.startswith("-- trigger forbids updates")  # comment hoisted with it


def test_json_object_keys_and_record_lists() -> None:
    obj = (
        "{\n"
        + ",\n".join(f'  "key{n}": [\n' + "    1,\n" * 10 + "    2\n  ]" for n in range(4))
        + "\n}\n"
    )
    assert {c.heading for c in _chunks("data.json", obj).chunks} >= {"key0", "key1", "key2"}
    records = (
        "[\n"
        + ",\n".join('  {\n    "a": 1,\n    "b": 2,\n    "c": 3\n  }' for _ in range(30))
        + "\n]"
    )
    result = _chunks("records.json", records)
    assert len(result.chunks) >= 2
    _bounded(result)


def test_python_migration_is_cut_at_functions_and_operations() -> None:
    text = (
        '"""Add the event table."""\nimport sqlalchemy as sa\nfrom alembic import op\n\n\n'
        "def upgrade() -> None:\n"
        + "".join(
            f'    op.create_table("t{n}",\n'
            + "".join(f'        sa.Column("c{i}", sa.Text()),\n' for i in range(8))
            + "    )\n"
            for n in range(5)
        )
        + '    op.execute("CREATE TRIGGER trg_event_no_update BEFORE UPDATE ON event")\n\n\n'
        "def downgrade() -> None:\n    op.drop_table('t0')\n"
    )
    result = _chunks("migrations/versions/0002_x.py", text)
    headings = [c.heading for c in result.chunks]
    assert "def downgrade" in headings and any(
        h.startswith("upgrade: op.create_table") for h in headings
    )
    trigger = next(c for c in result.chunks if "trg_event_no_update" in c.text)
    assert "trg_event_no_update" in trigger.idents
    _bounded(result)


def test_a_file_is_capped_at_a_bounded_number_of_chunks() -> None:
    result = _chunks("docs/huge.md", _doc(sections=MAX_CHUNKS_PER_FILE * 2, lines=3))
    assert len(result.chunks) == MAX_CHUNKS_PER_FILE and result.truncated
    _bounded(result)


def test_lockfiles_keep_a_stub_and_nothing_more() -> None:
    lock = "version = 1\n" + "\n".join(f'[[package]]\nname = "dep{n}"' for n in range(5000))
    result = _chunks("uv.lock", lock)
    assert result.status == "stub" and len(result.chunks) == 1
    assert result.chunks[0].end_line <= 12 and "dep4000" not in result.chunks[0].text
    npm = '{\n  "lockfileVersion": 3,\n' + '  "x": 1,\n' * 3000 + "}\n"
    assert _chunks("web/package-lock.json", npm).status == "stub"
    assert is_lockfile("a/b/yarn.lock") and not is_lockfile("policy/root_policy.lock")
    assert _chunks("policy/root_policy.lock", "digest: abc\n").status == "chunked"


def test_minified_generated_and_huge_files_are_stubs() -> None:
    minified = "x=" + "1," * 4000 + "\n"
    assert _chunks("static/app.js", minified).status == "stub"
    assert _chunks("data/big.json", '{"a": "' + "y" * 6000 + '"}').status == "stub"
    assert _chunks("api/schema.json", '{"//": "@generated", "a": 1}').status == "stub"
    huge = "\n".join(f"line {i} of a very large text file" for i in range(20_000))
    result = _chunks("docs/dump.txt", huge)
    assert result.status == "stub" and len(result.chunks) == 1
    unknown = "x" * 100 + "\n" * 1 + ("y" * 70 + "\n") * 1500
    assert _chunks("blob.zzz", unknown).status == "stub"


def test_secret_bearing_files_are_never_stored_as_text() -> None:
    for path in (
        ".env",
        ".env.local",
        "deploy/.env.production",
        "certs/server.pem",
        "id_rsa",
        "tf/terraform.tfstate",
        "k/credentials.json",
        "secrets.yaml",
        ".npmrc",
    ):
        assert is_sensitive_path(path), path
        result = _chunks(path, "PASSWORD=hunter2\n")
        assert result.status == "skipped" and result.chunks == []
    assert not is_sensitive_path(".env.example") and not is_sensitive_path("docs/secrets.md")
    assert _chunks(".env.example", "EIOS_X=1\n").chunks


def test_credentials_in_urls_are_masked_in_stored_text() -> None:
    text = "database: postgresql://eios:hunter2@localhost:5432/eios\n"
    (chunk,) = _chunks("config/db.yaml", text).chunks
    assert "hunter2" not in chunk.text and "[REDACTED]" in chunk.text
    assert chunk.start_line == 1


def test_crlf_files_chunk_like_lf_files() -> None:
    body = "".join(f"fact {i}\r\n" for i in range(10))
    text = f"# T\r\n\r\n{body}\r\n## Next\r\n\r\n{body}"
    result = _chunks("docs/win.md", text)
    assert [c.heading for c in result.chunks] == ["T", "T > Next"]
    assert all("\r" not in c.text for c in result.chunks)


def test_identifiers_are_whole_names_with_dotted_names_and_their_parts() -> None:
    found = identifiers(
        "ALTER TABLE x ADD CONSTRAINT uq_event_run_seq UNIQUE; SET LOCAL eios.retention = 'on'; "
        "call policy.forbid_audit_mutation() from pyproject.toml, RetentionService.run"
    )
    for name in (
        "uq_event_run_seq",
        "eios.retention",
        "eios",
        "retention",
        "policy.forbid_audit_mutation",
        "forbid_audit_mutation",
        "RetentionService.run",
        "RetentionService",
        "pyproject",
    ):
        assert name in found, name
    assert "pyproject.toml" not in found and "toml" not in found
    assert "uq_event" not in found  # whole words only: no substrings


def test_which_names_are_worth_a_lookup() -> None:
    for name in ("uq_event_run_seq", "eios.retention", "RetentionService", "getUser", "MAX_ITEMS"):
        assert is_identifier_like(name), name
    for name in ("retention", "Vector", "SQL", "job"):
        assert not is_identifier_like(name), name


def test_more_credential_bearing_config_files_are_never_stored_as_text() -> None:
    for path in (
        "src/appsettings.Production.json",
        "func/local.settings.json",
        "app/application-prod.yml",
        "app/application-dev.properties",
        "ops/kubeconfig",
        "web/.htpasswd",
        "deploy/prod.env",
        "home/.docker/config.json",
        "secrets/credentials.toml",
    ):
        assert is_sensitive_path(path), path
    for path in (
        "src/appsettings.json",
        "app/application.yml",
        "deploy/secrets.example.yaml",
        "src/appsettings.Production.example.json",
        "docs/credentials.md",
        "web/config.json",
    ):
        assert not is_sensitive_path(path), path


def test_credential_assignments_are_masked_but_references_and_quantities_are_kept() -> None:
    text = "\n".join(
        [
            'password = "hunter2"',
            '"api_key": "abc123",',
            "token: s3cr3tvalue",
            "Authorization: Bearer eyJhbGciOi",
            "password: ${DB_PASSWORD}",
            "password: str",
            'secret_key = os.getenv("SECRET_KEY")',
            "max_tokens: 4000",
            "token_budget = 1200",
            "passed: true",
        ]
    )
    (chunk,) = _chunks("docs/config.txt", text).chunks
    for leaked in ("hunter2", "abc123", "s3cr3tvalue", "eyJhbGciOi"):
        assert leaked not in chunk.text, leaked
    for kept in (
        "${DB_PASSWORD}",
        "password: str",
        'os.getenv("SECRET_KEY")',
        "max_tokens: 4000",
        "token_budget = 1200",
        "passed: true",
    ):
        assert kept in chunk.text, kept
