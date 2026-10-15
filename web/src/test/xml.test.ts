import { describe, expect, it } from "vitest";
import { MAX_ELEMENTS, identityOf, isMarkdownPath, isXmlPath, parseXml } from "../lib/xml";

const CSPROJ = `<?xml version="1.0" encoding="utf-8"?>
<!-- packages -->
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Serilog" Version="3.1.1" />
    <PackageReference Include="Dapper" Version="2.1.0"></PackageReference>
  </ItemGroup>
</Project>
`;

describe("parseXml", () => {
  it("reads elements, attributes, text and line numbers", () => {
    const r = parseXml(CSPROJ);
    if (!r.ok) throw new Error(r.error);
    expect(r.root.name).toBe("Project");
    expect(r.root.attrs).toEqual([["Sdk", "Microsoft.NET.Sdk"]]);
    expect(r.elements).toBe(6);
    const group = r.root.children[1]!;
    expect(group.children.map((c) => c.attrs[0]![1])).toEqual(["Serilog", "Dapper"]);
    expect(group.children[0]!.line).toBe(6);
    expect(r.root.children[0]!.children[0]!.text).toBe("net8.0");
  });

  it("names an element by its identifying attribute", () => {
    const r = parseXml('<beans><bean id="db" class="x.Db"/></beans>');
    if (!r.ok) throw new Error(r.error);
    expect(identityOf(r.root.children[0]!)).toEqual(["id", "db"]);
    expect(identityOf(r.root)).toBeNull();
  });

  it("decodes the built-in entities and keeps a '>' inside a quoted attribute", () => {
    const r = parseXml('<a title="x &gt; y" n="1>2">fish &amp; chips &#65;</a>');
    if (!r.ok) throw new Error(r.error);
    expect(r.root.attrs).toEqual([["title", "x > y"], ["n", "1>2"]]);
    expect(r.root.text).toBe("fish & chips A");
  });

  it("keeps CDATA as text and ignores comments and declarations", () => {
    const r = parseXml("<?xml version='1.0'?><!DOCTYPE a><a><!-- no --><![CDATA[1 < 2]]></a>");
    if (!r.ok) throw new Error(r.error);
    expect(r.root.text).toBe("1 < 2");
  });

  it("says why it cannot read a document", () => {
    expect(parseXml("<a><b></a>")).toEqual({ ok: false, error: "</a> at line 1 does not match <b>" });
    expect(parseXml("<a><b>")).toEqual({ ok: false, error: "<b> is never closed" });
    expect(parseXml("just text")).toEqual({ ok: false, error: "there is no element in it" });
    const bomb = parseXml('<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>');
    expect(bomb.ok).toBe(false);
  });

  it("stops at a fixed number of elements instead of drawing a huge file", () => {
    const wide = `<root>${"<i/>".repeat(MAX_ELEMENTS + 500)}</root>`;
    const r = parseXml(wide);
    if (!r.ok) throw new Error(r.error);
    expect(r.truncated).toBe(true);
    expect(r.elements).toBe(MAX_ELEMENTS);
  });
});

describe("path helpers", () => {
  it("knows which files are read as documents", () => {
    for (const p of ["a/App.csproj", "pom.xml", "x/Main.xaml", "src/web.config", "src/Web.Config", "nuget.config"]) expect(isXmlPath(p)).toBe(true);
    for (const p of ["a.json", "config.ts", "notes.md", "my.config.js"]) expect(isXmlPath(p)).toBe(false);
    expect(isMarkdownPath("docs/README.MD")).toBe(true);
    expect(isMarkdownPath("a.xml")).toBe(false);
  });
});
