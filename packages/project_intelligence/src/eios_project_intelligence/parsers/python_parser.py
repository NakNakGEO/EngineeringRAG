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
        self._walk(tree, result, scope=[], class_scope=None)
        return result

    # -- symbol/call collection -------------------------------------------------------------
    def _walk(
        self, node: ast.AST, out: ParsedFile, *, scope: list[str], class_scope: str | None
    ) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    out.imports.append(ParsedImport(alias.name, child.lineno))
            elif isinstance(child, ast.ImportFrom):
                names = tuple(a.name for a in child.names)
                out.imports.append(
                    ParsedImport(child.module or "", child.lineno, names, level=child.level)
                )
            elif isinstance(child, ast.ClassDef):
                qualified = ".".join([*scope, child.name])
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
                self._walk(child, out, scope=[*scope, child.name], class_scope=qualified)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                qualified = ".".join([*scope, child.name])
                prefix = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
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
                # nested defs are not indexed as symbols; their calls belong to the outer symbol
            else:
                if isinstance(child, ast.Assign | ast.AnnAssign) and not scope:
                    self._module_variable(child, out)
                self._walk(child, out, scope=scope, class_scope=class_scope)
        if isinstance(node, ast.Module):
            self._collect_calls_in_statements(node, out)

    @staticmethod
    def _module_variable(node: ast.Assign | ast.AnnAssign, out: ParsedFile) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id.isupper() and len(target.id) > 1:
                out.symbols.append(
                    ParsedSymbol(
                        name=target.id,
                        qualified_name=target.id,
                        kind="variable",
                        start_line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                    )
                )

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
