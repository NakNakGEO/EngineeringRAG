"""Call-site resolution never crosses language families and never guesses on builtins."""

from __future__ import annotations

import uuid

from eios_project_intelligence.languages import is_reexport_hub, language_family
from eios_project_intelligence.resolver import CallRequest, is_builtin_callee, pick_targets
from eios_project_intelligence.store import SymbolRow


def _sym(
    path: str,
    qualified: str,
    kind: str = "function",
    container: str | None = None,
) -> SymbolRow:
    return SymbolRow(
        id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        path=path,
        scope="committed",
        name=qualified.split(".")[-1],
        qualified_name=qualified,
        kind=kind,
        container=container,
        start_line=1,
        end_line=2,
        signature="",
        exported=True,
    )


def _call(owner: str, callee: str, caller: str | None = "run", kind: str = "calls") -> CallRequest:
    return CallRequest(owner_path=owner, caller=caller, callee=callee, line=3, kind=kind)


def test_language_families_merge_ts_and_js_but_keep_python_apart() -> None:
    assert language_family("web/src/a.ts") == language_family("web/src/b.jsx") == "js"
    assert language_family("pkg/a.py") == "python"
    assert language_family("pkg/a.py") != language_family("web/src/a.ts")
    assert is_reexport_hub("pkg/__init__.py") and is_reexport_hub("web/src/index.ts")
    assert not is_reexport_hub("pkg/init_helpers.py")


def test_a_python_builtin_call_never_resolves_to_a_typescript_symbol() -> None:
    ts_str = _sym("web/src/mission.ts", "str", "variable")
    ts_fn = _sym("web/src/format.ts", "str")
    assert pick_targets(_call("pkg/a.py", "str"), [ts_str, ts_fn], set()) is None  # builtin
    # a project function of the same name in ANOTHER language is not a candidate either
    result = pick_targets(_call("pkg/a.py", "render"), [_sym("web/src/ui.tsx", "render")], set())
    assert result is not None and result.targets == () and result.confidence == 0.2


def test_a_js_regex_test_call_does_not_resolve_to_a_method_called_test() -> None:
    workshop_test = _sym("pkg/workshop.py", "Workshop.test", "method", "Workshop")
    assert pick_targets(_call("web/src/a.ts", "test"), [workshop_test], set()) is None


def test_confidence_tiers_same_file_import_unique_and_unresolved() -> None:
    here = _sym("pkg/a.py", "helper")
    there = _sym("pkg/b.py", "helper")
    other = _sym("pkg/c.py", "helper")
    assert pick_targets(_call("pkg/a.py", "helper"), [here, there], {"pkg/b.py"}).confidence == 0.95  # type: ignore[union-attr]
    via_import = pick_targets(_call("pkg/z.py", "helper"), [there, other], {"pkg/b.py"})
    assert via_import is not None and via_import.confidence == 0.8
    assert [t.path for t in via_import.targets] == ["pkg/b.py"]
    unique = pick_targets(_call("pkg/z.py", "helper"), [there], set())
    assert unique is not None and unique.confidence == 0.3
    ambiguous = pick_targets(_call("pkg/z.py", "helper"), [there, other], set())
    assert ambiguous is not None and ambiguous.targets == () and ambiguous.confidence == 0.2


def test_builtin_names_resolve_through_their_own_file_or_imports_only() -> None:
    mine = _sym("pkg/a.py", "get", "method", "Store")
    assert pick_targets(_call("pkg/a.py", "get"), [mine], set()).confidence == 0.95  # type: ignore[union-attr]
    assert pick_targets(_call("pkg/z.py", "get"), [mine], set()) is None  # not a project guess
    assert is_builtin_callee("pkg/z.py", "append") and not is_builtin_callee("pkg/z.py", "charge")
    assert is_builtin_callee("web/src/z.ts", "map") and not is_builtin_callee("pkg/z.py", "map_x")


def test_a_delegating_method_is_not_credited_with_a_same_file_call_to_itself() -> None:
    outer = _sym("pkg/a.py", "Engine.evaluate", "method", "Engine")
    result = pick_targets(_call("pkg/a.py", "evaluate", caller="Engine.evaluate"), [outer], set())
    assert result is not None and result.confidence < 0.95  # at best a weak project-wide guess


def test_nested_defs_are_callable_only_from_the_file_that_defines_them() -> None:
    nested = _sym("pkg/a.py", "run.inner", "function", "run")
    elsewhere = pick_targets(_call("pkg/b.py", "inner"), [nested], set())
    assert elsewhere is not None and elsewhere.targets == ()
    local = pick_targets(_call("pkg/a.py", "inner", caller="run"), [nested], set())
    assert local is not None and local.targets == (nested,) and local.confidence == 0.95


def test_fields_and_variables_are_never_call_targets() -> None:
    field = _sym("pkg/a.py", "Item.total", "field", "Item")
    var = _sym("pkg/a.py", "TOTAL", "variable")
    result = pick_targets(_call("pkg/a.py", "total"), [field, var], set())
    assert result is not None and result.targets == ()


def test_import_edges_record_which_names_each_statement_brings_in() -> None:
    from eios_project_intelligence.graph_builder import resolve_imports
    from eios_project_intelligence.parsers import ParsedFile, ParsedImport, PythonParser

    parsed = ParsedFile(language="python", line_count=3)
    parsed.imports = [
        ParsedImport("pkg.events", 1, ("EventType",)),
        ParsedImport("pkg.events", 2, ("Other",)),
        ParsedImport("pkg.other", 3, ()),
    ]
    index = {"pkg.events": ["pkg/events.py"], "pkg.other": ["pkg/other.py"]}
    edges = {e.dst_key: e for e in resolve_imports("app/a.py", parsed, PythonParser(), index)}
    assert edges["file:pkg/events.py"].attrs["names"] == ["EventType", "Other"]  # merged
    assert edges["file:pkg/other.py"].attrs["names"] == []  # whole-module import
    whole = ParsedFile(language="python", line_count=2)
    whole.imports = [ParsedImport("pkg.events", 1, ("EventType",)), ParsedImport("pkg.events", 2)]
    merged = resolve_imports("app/a.py", whole, PythonParser(), index)
    assert merged[0].attrs["names"] == []  # a bare module import means any name may be used
