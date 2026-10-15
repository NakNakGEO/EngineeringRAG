import { describe, expect, it } from "vitest";
import { buildBrain, dirId, fileId, groupColor, noteNodeId, PROJECT_ID, type Overview } from "../graph/model";

const overview: Overview = {
  files: [
    { path: "README.md", language: "markdown", lines: 10, symbols: 0 },
    { path: "src/app.py", language: "python", lines: 120, symbols: 8 },
    { path: "src/util/io.py", language: "python", lines: 40, symbols: 3 },
    { path: "docs/guide.md", language: "markdown", lines: 30, symbols: 0 },
  ],
  imports: [["src/app.py", "src/util/io.py"], ["src/app.py", "missing.py"], ["src/app.py", "src/app.py"]],
  changed: [],
  truncated: false,
};

describe("buildBrain", () => {
  const brain = buildBrain("demo", overview, []);
  const ids = new Set(brain.nodes.map((n) => n.id));

  it("makes a project node, a node per folder and a node per file", () => {
    expect(ids.has(PROJECT_ID)).toBe(true);
    expect([dirId("src"), dirId("src/util"), dirId("docs")].every((i) => ids.has(i))).toBe(true);
    expect(brain.nodes.filter((n) => n.kind === "file")).toHaveLength(4);
  });

  it("counts files per folder, nested ones included", () => {
    expect(brain.nodes.find((n) => n.id === dirId("src"))?.children).toBe(2);
    expect(brain.nodes.find((n) => n.id === PROJECT_ID)?.children).toBe(4);
  });

  it("keeps only imports between known, different files", () => {
    const imports = brain.edges.filter((e) => e.kind === "imports");
    expect(imports.map((e) => [e.source, e.target])).toEqual([[fileId("src/app.py"), fileId("src/util/io.py")]]);
  });

  it("nests folders and files through contains edges", () => {
    const has = (s: string, t: string) => brain.edges.some((e) => e.kind === "contains" && e.source === s && e.target === t);
    expect(has(PROJECT_ID, dirId("src"))).toBe(true);
    expect(has(dirId("src"), dirId("src/util"))).toBe(true);
    expect(has(dirId("src/util"), fileId("src/util/io.py"))).toBe(true);
    expect(has(PROJECT_ID, fileId("README.md"))).toBe(true);
  });

  it("gives each top-level folder its own colour group", () => {
    expect(brain.groups).toEqual(["src", "(root)", "docs"]); // biggest first
    expect(groupColor("src", brain.groups, true)).not.toBe(groupColor("docs", brain.groups, true));
  });
});

describe("colour groups", () => {
  it("splits one dominant top-level folder into its sub-folders", () => {
    const b = buildBrain("pkg", {
      files: [
        { path: "pkg/a/x.py", language: "python", lines: 1, symbols: 0 },
        { path: "pkg/a/y.py", language: "python", lines: 1, symbols: 0 },
        { path: "pkg/b/z.py", language: "python", lines: 1, symbols: 0 },
        { path: "pkg/top.py", language: "python", lines: 1, symbols: 0 },
        { path: "README.md", language: "markdown", lines: 1, symbols: 0 },
      ],
      imports: [], changed: [], truncated: false,
    }, []);
    expect(b.groups).toEqual(["pkg/a", "(root)", "pkg", "pkg/b"]);
  });
});

describe("notes in the brain", () => {
  it("attaches a note to its file, or to the project when it has no known file", () => {
    const b = buildBrain("demo", overview, [
      { id: "n1", title: "How app works", kind: "architecture", path: "src/app.py" },
      { id: "n2", title: "Overview", kind: "overview", path: null },
      { id: "n3", title: "Stale", kind: "note", path: "gone.py" },
    ]);
    const to = (id: string) => b.edges.find((e) => e.source === noteNodeId(id))?.target;
    expect(to("n1")).toBe(fileId("src/app.py"));
    expect(to("n2")).toBe(PROJECT_ID);
    expect(to("n3")).toBe(PROJECT_ID);
    expect(b.notesByPath.get("src/app.py")).toEqual(["n1"]);
  });
});
