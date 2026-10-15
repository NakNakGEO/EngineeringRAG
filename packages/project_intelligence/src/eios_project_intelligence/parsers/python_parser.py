"""Python parser built on the standard library ``ast`` (accurate; no external dependency)."""

from __future__ import annotations

import ast
import posixpath

from eios_project_intelligence.parsers.base import (
    ParsedCall,
    ParsedFile,
    ParsedImport,
    ParsedSymbol,
)


def python_module_keys(path: str) -> list[str]:
    """All dotted suffixes of a file's path: ``src/pkg/mod.py`` -> ``src.pkg.mod``, ``pkg.mod``,
    ``mod``. ``__init__.py`` provides its package. Imports are matched against these keys."""
    stem = posixpath.splitext(path)[0]
    parts = [p for p in stem.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts[i:]) for i in range(len(parts))] if parts else []


class PythonParser:
    language = "python"

    def parse(self, path: str, text: str) -> ParsedFile:
        line_count = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        result = ParsedFile(language="python", line_count=line_count)
        result.provides = python_module_keys(path)
        try:
            tree = ast.parse(text, filename=path)
        except (SyntaxError, ValueError, RecursionError) as exc:
            result.parse_error = f"{type(exc).__name__}: {exc}"[:300]
            return result
        self._walk(tree, result, scope=[], class_scope=None, names=set())
        return result

    # -- symbol/call collection -------------------------------------------------------------
    def _walk(
        self,
        node: ast.AST,
        out: ParsedFile,
        *,
        scope: list[str],
        class_scope: str | None,
        names: set[str],
    ) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    out.imports.append(ParsedImport(alias.name, child.lineno))
            elif isinstance(child, ast.ImportFrom):
                imported = tuple(a.name for a in child.names)
                out.imports.append(
                    ParsedImport(child.module or "", child.lineno, imported, level=child.level)
                )
            elif isinstance(child, ast.ClassDef):
                qualified = ".".join([*scope, child.name])
                names.add(qualified)
                out.symbols.append(
                    ParsedSymbol(
                        name=child.name,
                        qualified_name=qualified,
                        kind="class",
                        start_line=child.lineno,
                        end_line=child.end_lineno or child.lineno,
                        container=".".join(scope) or None,
                        signature=f"class {child.name}"
                        + (
                            f"({', '.join(ast.unparse(b) for b in child.bases)})"
                            if child.bases
                            else ""
                        ),
                        exported=not child.name.startswith("_"),
                    )
                )
                self._walk(
                    child, out, scope=[*scope, child.name], class_scope=qualified, names=names
                )
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                qualified = ".".join([*scope, child.name])
                prefix = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                names.add(qualified)
                out.symbols.append(
                    ParsedSymbol(
                        name=child.name,
                        qualified_name=qualified,
                        kind="method" if class_scope else "function",
                        start_line=child.lineno,
                        end_line=child.end_lineno or child.lineno,
                        container=class_scope,
                        signature=f"{prefix} {child.name}({ast.unparse(child.args)})",
                        exported=not child.name.startswith("_"),
                    )
                )
                self._collect_calls(child, qualified, out)
                # a nested def is a symbol of its own, but its calls stay with the outer symbol
                self._nested_defs(child, qualified, out, names)
            elif isinstance(child, ast.TypeAlias):
                if isinstance(child.name, ast.Name) and not scope:
                    self._add_extra(
                        out, names, child.name.id, child.name.id, "type", child, None, "type alias"
                    )
            else:
                if isinstance(child, ast.Assign | ast.AnnAssign):
                    if not scope:
                        self._module_variable(child, out, names)
                    elif class_scope is not None:
                        self._class_field(child, out, class_scope, names)
                self._walk(child, out, scope=scope, class_scope=class_scope, names=names)
        if isinstance(node, ast.Module):
            self._collect_calls_in_statements(node, out)

    @staticmethod
    def _add_extra(
        out: ParsedFile,
        names: set[str],
        name: str,
        qualified: str,
        kind: str,
        node: ast.AST,
        container: str | None,
        signature: str,
        *,
        exported: bool | None = None,
    ) -> None:
        """Add a symbol unless that qualified name is already taken (first definition wins)."""
        if qualified in names:
            return
        names.add(qualified)
        line = getattr(node, "lineno", 1)
        out.symbols.append(
            ParsedSymbol(
                name=name,
                qualified_name=qualified,
                kind=kind,
                start_line=line,
                end_line=getattr(node, "end_lineno", None) or line,
                container=container,
                signature=signature,
                exported=not name.startswith("_") if exported is None else exported,
            )
        )

    def _module_variable(
        self, node: ast.Assign | ast.AnnAssign, out: ParsedFile, names: set[str]
    ) -> None:
        """CONSTANTS always; other module-level names when bound to a call or subscript - a table
        (``job = sa.Table(...)``), a type alias (``Dep = Annotated[...]``), a registry."""
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        for target in targets:
            if not isinstance(target, ast.Name) or target.id.startswith("__"):
                continue
            constant = target.id.isupper() and len(target.id) > 1
            bound = isinstance(value, ast.Call | ast.Subscript) and not target.id.startswith("_")
            if constant or bound:
                self._add_extra(out, names, target.id, target.id, "variable", node, None, target.id)

    def _class_field(
        self, node: ast.Assign | ast.AnnAssign, out: ParsedFile, class_scope: str, names: set[str]
    ) -> None:
        """Class-body names: dataclass and pydantic fields, enum members, class constants."""
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if not isinstance(target, ast.Name) or (
                target.id.startswith("__") and target.id.endswith("__")
            ):
                continue
            annotation = ast.unparse(node.annotation) if isinstance(node, ast.AnnAssign) else None
            self._add_extra(
                out,
                names,
                target.id,
                f"{class_scope}.{target.id}",
                "field",
                node,
                class_scope,
                f"{target.id}: {annotation}" if annotation else target.id,
            )

    def _nested_defs(self, fn: ast.AST, outer: str, out: ParsedFile, names: set[str]) -> None:
        for child in ast.iter_child_nodes(fn):
            self._nested_def_in(child, outer, out, names)

    def _nested_def_in(self, node: ast.AST, outer: str, out: ParsedFile, names: set[str]) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            qualified = f"{outer}.{node.name}"
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            self._add_extra(
                out,
                names,
                node.name,
                qualified,
                "function",
                node,
                outer,
                f"{prefix} {node.name}({ast.unparse(node.args)})",
                exported=False,
            )
            outer = qualified
        elif isinstance(node, ast.ClassDef | ast.Lambda):
            return
        for child in ast.iter_child_nodes(node):
            self._nested_def_in(child, outer, out, names)

    @staticmethod
    def _callee(node: ast.Call) -> str | None:
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    def _collect_calls(self, fn: ast.AST, caller: str, out: ParsedFile) -> None:
        seen: set[tuple[str, int]] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                callee = self._callee(node)
                if callee and (callee, node.lineno) not in seen:
                    seen.add((callee, node.lineno))
                    out.calls.append(ParsedCall(caller, callee, node.lineno))

    def _collect_calls_in_statements(self, module: ast.Module, out: ParsedFile) -> None:
        """Module-level calls (outside any function/class body)."""
        for stmt in module.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            for node in ast.walk(stmt):
                if isinstance(node, ast.Call):
                    callee = self._callee(node)
                    if callee:
                        out.calls.append(ParsedCall(None, callee, node.lineno))

    # -- import resolution --------------------------------------------------------------------
    def import_keys(self, path: str, imp: ParsedImport) -> list[str]:
        base: list[str]
        if imp.level:
            directory = posixpath.dirname(path).split("/") if "/" in path else []
            up = imp.level - 1
            base_parts = directory[: len(directory) - up] if up else directory
            base = [p for p in base_parts if p]
            module = ".".join([*base, *([imp.spec] if imp.spec else [])])
        else:
            module = imp.spec
        keys = [module] if module else []
        # ``from pkg import mod`` may import a submodule
        keys += [f"{module}.{n}" if module else n for n in imp.names if n != "*"]
        return keys
