"""XML read like Markdown: the element tree becomes an outline of sections.

``.csproj``, ``pom.xml``, ``web.config``, XAML, ``.resx`` and plain ``.xml`` have no functions or
classes, but they do have structure: which packages a project references, which settings a config
sets, which sections a document has. Each element worth naming becomes a ``section`` symbol with
its line range, like a Markdown heading. Standard library only (expat); the document is only ever
read: entity declarations are refused and nothing is fetched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.parsers import expat

from eios_project_intelligence.parsers.base import ParsedFile, ParsedImport, ParsedSymbol

MAX_TEXT = 4_000_000
MAX_SYMBOLS = 400  # a big data file is outlined, not enumerated
MAX_DEPTH = 6
SHALLOW = 2  # elements this close to the root are always listed
# attributes that name an element: <PackageReference Include="x">, <bean id="x">, <add key="x">
IDENTITY_ATTRS = (
    "Include", "Update", "name", "Name", "id", "Id", "key", "Key", "x:Name", "ref", "label",
    "title", "path",
)  # fmt: skip
_ENTITY_DECL = re.compile(r"<!ENTITY", re.I)


@dataclass
class _Open:
    tag: str
    attrs: dict[str, str]
    line: int
    depth: int
    identity: str | None
    children: int = 0


def element_label(tag: str, attrs: dict[str, str]) -> tuple[str, str | None]:
    """``(label, identity)``: ``PackageReference Include="Serilog"`` and ``Serilog``."""
    for key in IDENTITY_ATTRS:
        value = attrs.get(key)
        if value:
            value = " ".join(value.split())[:80]
            return f'{tag} {key}="{value}"', value
    return tag, None


def _line_count(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") or not text else 1)


class XmlParser:
    language = "xml"

    def parse(self, path: str, text: str) -> ParsedFile:
        out = ParsedFile(language="xml", line_count=_line_count(text))
        if len(text) > MAX_TEXT:
            out.parse_error = "file is too large to outline"
            return out
        if _ENTITY_DECL.search(text):
            out.parse_error = "entity declarations are not read"
            return out

        parser = expat.ParserCreate()
        stack: list[_Open] = []
        found: list[tuple[int, int, int, str, str | None, str]] = []  # line, end, depth, label, id, sig

        def start(tag: str, attrs: dict[str, str]) -> None:
            if stack:
                stack[-1].children += 1
            identity = element_label(tag, attrs)[1]
            stack.append(_Open(tag, attrs, parser.CurrentLineNumber, len(stack) + 1, identity))

        def end(_tag: str) -> None:
            if not stack:
                return
            el = stack.pop()
            listed = el.depth <= SHALLOW or (el.identity is not None and el.depth <= MAX_DEPTH) or (
                el.children > 0 and el.depth <= 3
            )
            if listed:
                label, identity = element_label(el.tag, el.attrs)
                shown = " ".join(f'{k}="{v[:40]}"' for k, v in list(el.attrs.items())[:4])
                found.append((el.line, max(parser.CurrentLineNumber, el.line), el.depth, label, identity, f"<{el.tag} {shown}>".replace(" >", ">")))

        parser.StartElementHandler = start
        parser.EndElementHandler = end
        try:
            parser.Parse(text.lstrip("﻿"), True)
        except expat.ExpatError as exc:
            out.parse_error = f"not well-formed XML near line {exc.lineno}: {expat.ErrorString(exc.code)}"

        found.sort(key=lambda f: (f[0], f[2]))
        trail: list[tuple[int, str]] = []  # (depth, label) of the listed ancestors
        for line, end_line, depth, label, _identity, signature in found[:MAX_SYMBOLS]:
            while trail and trail[-1][0] >= depth:
                trail.pop()
            parent = " > ".join(name for _, name in trail) or None
            trail.append((depth, label))
            qualified = " > ".join(name for _, name in trail)
            out.symbols.append(
                ParsedSymbol(label[:200], qualified[:300], "section", line, end_line, container=parent, signature=signature[:200])
            )
        return out

    def import_keys(self, path: str, imp: ParsedImport) -> list[str]:
        return []
