from __future__ import annotations

from eios_project_intelligence.languages import (
    detect_language,
    is_test_path,
    looks_binary,
)
from eios_project_intelligence.parsers import (
    CSharpParser,
    GoParser,
    JavaParser,
    MarkdownParser,
    ParsedFile,
    ParsedImport,
    ParserRegistry,
    PythonParser,
    SqlParser,
    TypeScriptParser,
)
from eios_project_intelligence.parsers.python_parser import python_module_keys


def _sym(parsed: ParsedFile) -> dict[str, tuple[str, int, int]]:
    return {s.qualified_name: (s.kind, s.start_line, s.end_line) for s in parsed.symbols}


def test_language_detection_and_test_paths() -> None:
    assert detect_language("src/app/Main.cs") == "csharp"
    assert detect_language("Dockerfile") == "dockerfile"
    assert detect_language("a/b/unknown.zzz") == "unknown"
    assert is_test_path("tests/unit/test_x.py") and is_test_path("src/foo.test.ts")
    assert is_test_path("UserServiceTests.cs") and is_test_path("pkg/x_test.go")
    assert not is_test_path("src/contest.py") and not is_test_path("src/latest/app.py")
    assert looks_binary(b"abc\0def") and not looks_binary(b"plain text")


PY = '''"""doc"""
import os
import pkg.util as u
from . import sibling
from ..core.settings import Settings, get_settings
from .models import *

MAX_RETRIES = 3

def top(a, b=1):
    helper(a)
    return os.path.join("x", "y")

async def fetch():
    await client.get_user()

class Repo(Base):
    def save(self, item):
        self._validate(item)
        db.insert(item)

    def _validate(self, item):
        def inner():
            deep_call()
        inner()

top(1)
'''


def test_python_symbols_imports_calls() -> None:
    p = PythonParser().parse("src/pkg/service.py", PY)
    assert p.parse_error is None and p.line_count == PY.count("\n")
    syms = _sym(p)
    assert syms["top"][0] == "function" and syms["fetch"][0] == "function"
    assert syms["Repo"][0] == "class" and syms["Repo.save"][0] == "method"
    assert syms["Repo._validate"][0] == "method" and syms["MAX_RETRIES"][0] == "variable"
    assert "inner" not in syms  # nested helpers are not indexed
    save = next(s for s in p.symbols if s.qualified_name == "Repo.save")
    assert (save.container, save.exported) == ("Repo", True)
    assert next(s for s in p.symbols if s.name == "_validate").exported is False
    top = next(s for s in p.symbols if s.name == "top")
    assert top.signature == "def top(a, b=1)" and top.end_line > top.start_line

    specs = [(i.spec, i.level, i.names) for i in p.imports]
    assert ("os", 0, ()) in specs and ("pkg.util", 0, ()) in specs
    assert ("", 1, ("sibling",)) in specs and (
        "core.settings",
        2,
        ("Settings", "get_settings"),
    ) in specs

    calls = {(c.caller, c.callee) for c in p.calls}
    assert ("top", "helper") in calls and ("top", "join") in calls
    assert ("fetch", "get_user") in calls and ("Repo.save", "_validate") in calls
    assert ("Repo._validate", "deep_call") in calls  # nested call attributed to the outer symbol
    assert (None, "top") in calls  # module-level call


def test_python_syntax_error_is_reported_not_raised() -> None:
    p = PythonParser().parse("bad.py", "def broken(:\n  pass\n")
    assert p.parse_error and p.symbols == []


def test_python_module_keys_and_import_resolution() -> None:
    assert python_module_keys("src/pkg/mod.py") == ["src.pkg.mod", "pkg.mod", "mod"]
    assert python_module_keys("pkg/__init__.py") == ["pkg"]
    parser = PythonParser()
    assert parser.import_keys("a/b/c.py", ParsedImport("x.y", 1)) == ["x.y"]
    assert parser.import_keys("a/b/c.py", ParsedImport("m", 1, ("n",), level=1)) == [
        "a.b.m",
        "a.b.m.n",
    ]
    assert parser.import_keys("a/b/c.py", ParsedImport("m", 1, level=2)) == ["a.m"]
    assert parser.import_keys("a/b/c.py", ParsedImport("", 1, ("s",), level=1)) == ["a.b", "a.b.s"]
    assert parser.import_keys("a.py", ParsedImport("pkg", 1, ("mod", "*"))) == ["pkg", "pkg.mod"]


CS = """using System;
using System.Collections.Generic;
using Acme.Billing.Core;

namespace Acme.Billing.Services
{
    // class Fake { }
    public class InvoiceService : IInvoiceService
    {
        private readonly string _s = "class NotAClass { }";

        public decimal CalculateTotal(Invoice invoice, decimal tax)
        {
            var net = SumLines(invoice);
            return ApplyTax(net, tax);
        }

        private static decimal SumLines(Invoice invoice)
        {
            return 0m;
        }
    }

    public interface IInvoiceService
    {
        decimal CalculateTotal(Invoice invoice, decimal tax);
    }
}
"""


def test_csharp() -> None:
    p = CSharpParser().parse("Services/InvoiceService.cs", CS)
    syms = _sym(p)
    assert syms["InvoiceService"][0] == "class" and syms["IInvoiceService"][0] == "interface"
    assert "NotAClass" not in syms and "Fake" not in syms  # strings/comments are ignored
    assert syms["InvoiceService.CalculateTotal"][0] == "method"
    assert syms["InvoiceService.SumLines"][0] == "method"
    _, start, end = syms["InvoiceService.CalculateTotal"]
    assert end - start == 4  # block extent from brace matching
    assert "ns:Acme.Billing.Services" in p.provides and "type:InvoiceService" in p.provides
    assert [i.spec for i in p.imports] == [
        "System",
        "System.Collections.Generic",
        "Acme.Billing.Core",
    ]
    calls = {(c.caller, c.callee) for c in p.calls}
    assert ("InvoiceService.CalculateTotal", "SumLines") in calls
    assert ("InvoiceService.CalculateTotal", "ApplyTax") in calls
    assert CSharpParser().import_keys("x.cs", ParsedImport("Acme.Billing.Core", 1)) == [
        "ns:Acme.Billing.Core"
    ]


JAVA = """package com.acme.billing;

import java.util.List;
import com.acme.core.Money;
import static org.junit.Assert.assertEquals;

/** doc with class Hidden */
public class InvoiceService {
    public Money total(List<Line> lines) {
        return sum(lines);
    }
    private Money sum(List<Line> lines) {
        return Money.ZERO;
    }
}
"""


def test_java() -> None:
    p = JavaParser().parse("src/InvoiceService.java", JAVA)
    syms = _sym(p)
    assert set(syms) == {"InvoiceService", "InvoiceService.total", "InvoiceService.sum"}
    assert (
        "pkg:com.acme.billing" in p.provides
        and "type:com.acme.billing.InvoiceService" in p.provides
    )
    assert [i.spec for i in p.imports] == [
        "java.util.List",
        "com.acme.core.Money",
        "org.junit.Assert.assertEquals",
    ]
    assert ("InvoiceService.total", "sum") in {(c.caller, c.callee) for c in p.calls}
    keys = JavaParser().import_keys("x.java", ParsedImport("com.acme.core.Money", 1))
    assert keys == ["type:com.acme.core.Money", "pkg:com.acme.core"]


TS = """import { a } from './util';
import b from "../lib/b";
import React from 'react';
export * from './index-barrel';
const lazy = require('./lazy');

export interface User { id: number }
export type Id = string;

export class UserService {
  private cache = new Map();
  async load(id: Id): Promise<User> {
    return fetchUser(id);
  }
}

export function fetchUser(id: Id) {
  return http.get(`/u/${id}`);
}

export const compute = (x: number) => {
  return x * 2;
};
"""


def test_typescript() -> None:
    p = TypeScriptParser().parse("src/app/user.ts", TS)
    syms = _sym(p)
    assert syms["User"][0] == "interface" and syms["Id"][0] == "type"
    assert syms["UserService"][0] == "class" and syms["UserService.load"][0] == "method"
    assert syms["fetchUser"][0] == "function" and syms["compute"][0] == "function"
    assert [i.spec for i in p.imports] == [
        "./util",
        "../lib/b",
        "react",
        "./index-barrel",
        "./lazy",
    ]
    assert p.provides == ["file:src/app/user"]
    assert ("UserService.load", "fetchUser") in {(c.caller, c.callee) for c in p.calls}
    parser = TypeScriptParser()
    assert parser.import_keys("src/app/user.ts", ParsedImport("./util", 1)) == [
        "file:src/app/util",
        "file:src/app/util/index",
    ]
    assert (
        parser.import_keys("src/app/user.ts", ParsedImport("../lib/b.js", 1))[0] == "file:src/lib/b"
    )
    assert parser.import_keys("src/app/user.ts", ParsedImport("react", 1)) == []  # external
    assert TypeScriptParser().parse("src/x/index.ts", "export const a = 1;\n").provides == [
        "file:src/x/index",
        "file:src/x",
    ]


GO = """package billing

import (
	"fmt"
	"github.com/acme/core/money"
)

import "strings"

type Invoice struct {
	ID string
}

func (i *Invoice) Total() int {
	return sumLines(i)
}

func sumLines(i *Invoice) int { return 0 }
"""


def test_go() -> None:
    p = GoParser().parse("billing/invoice.go", GO)
    syms = _sym(p)
    assert syms["Invoice"][0] == "struct" and syms["Invoice.Total"][0] == "method"
    assert syms["sumLines"][0] == "function"
    assert next(s for s in p.symbols if s.name == "sumLines").exported is False
    assert sorted(i.spec for i in p.imports) == ["fmt", "github.com/acme/core/money", "strings"]
    assert p.provides == ["gopkg:billing"]
    assert "gopkg:core/money" in GoParser().import_keys(
        "x.go", ParsedImport("github.com/acme/core/money", 1)
    )


SQL = """-- create table Ghost (id int);
CREATE TABLE [dbo].[Orders] (
    Id INT PRIMARY KEY,
    CustomerId INT REFERENCES dbo.Customers(Id)
);
GO
CREATE OR ALTER PROCEDURE dbo.usp_GetOrders @cid INT AS
BEGIN
    SELECT o.Id FROM dbo.Orders o JOIN Customers c ON c.Id = o.CustomerId;
    EXEC dbo.usp_Audit;
END
"""


def test_sql_is_analysed_as_text_only() -> None:
    p = SqlParser().parse("db/orders.sql", SQL)
    syms = _sym(p)
    assert syms["orders"][0] == "table" and syms["usp_getorders"][0] == "procedure"
    assert "ghost" not in syms  # commented out
    assert set(p.provides) == {"sql:orders", "sql:usp_getorders"}
    refs = {(c.caller, c.callee, c.kind) for c in p.calls}
    assert ("orders", "customers", "references") in refs  # FK
    assert ("usp_getorders", "orders", "references") in refs
    assert ("usp_getorders", "customers", "references") in refs
    assert ("usp_getorders", "usp_audit", "references") in refs


def test_markdown_sections_ignore_code_fences() -> None:
    text = "# Title\n\nintro\n\n## Setup\n```\n# not a heading\n```\n\n## Usage\ntext\n"
    p = MarkdownParser().parse("README.md", text)
    assert [(s.name, s.start_line) for s in p.symbols] == [
        ("Title", 1),
        ("Setup", 5),
        ("Usage", 10),
    ]
    assert p.symbols[0].end_line >= p.symbols[2].end_line  # H1 spans the document


def test_registry_covers_parsed_languages() -> None:
    from eios_project_intelligence.languages import PARSED_LANGUAGES

    assert ParserRegistry().languages >= PARSED_LANGUAGES
