import { describe, expect, it } from "vitest";
import { computeLayout, deptId, departmentsOf, MAX_DEPARTMENTS, MAX_MEMBERS_PER_DEPT, NOTES_GROUP, OTHER_GROUP, type LayoutMode } from "../graph/layout";
import { buildBrain, dirId, type Overview } from "../graph/model";
import { treeRows } from "../graph/tree";

const files = (dir: string, n: number) =>
  Array.from({ length: n }, (_, i) => ({ path: `${dir}/f${String(i).padStart(3, "0")}.py`, language: "python", lines: 10 + i, symbols: 1 }));

const overview: Overview = {
  files: [...files("api", 40), ...files("web", 25), ...files("docs", 8), { path: "README.md", language: "markdown", lines: 5, symbols: 0 }],
  imports: [["api/f000.py", "web/f001.py"], ["api/f001.py", "web/f002.py"], ["web/f000.py", "docs/f000.py"], ["api/f002.py", "api/f003.py"]],
  changed: [],
  truncated: false,
};
const brain = buildBrain("demo", overview, [{ id: "n1", title: "How it works", kind: "how-it-works", path: null }]);
const MODES: LayoutMode[] = ["force", "circle", "rings", "hex"];

describe("departments", () => {
  it("groups files by top-level folder and puts notes in their own department", () => {
    const { depts } = departmentsOf(brain);
    expect(depts.map((d) => [d.group, d.count])).toEqual([["api", 40], ["web", 25], ["docs", 8], [NOTES_GROUP, 1], ["(root)", 1]]);
  });
});

describe("many departments", () => {
  it("keeps the biggest and merges the rest into one 'other' department", () => {
    const many = buildBrain("wide", {
      files: Array.from({ length: 30 }, (_, i) => ({ path: `d${String(i).padStart(2, "0")}/a.py`, language: "python", lines: 1, symbols: 0 })),
      imports: [], changed: [], truncated: false,
    }, []);
    const { depts, of } = departmentsOf(many);
    expect(depts).toHaveLength(MAX_DEPARTMENTS);
    const other = depts.find((d) => d.group === OTHER_GROUP)!;
    expect(other.count).toBe(30 - (MAX_DEPARTMENTS - 1));
    expect(of.get("file:d29/a.py")).toBe(other.id);
    expect(depts.reduce((n, d) => n + d.count, 0)).toBe(30);
  });
});

describe.each(MODES)("%s layout", (mode) => {
  it("places every department with finite numbers, without overlap", () => {
    const l = computeLayout(brain, mode, new Set());
    expect(l.depts).toHaveLength(5);
    for (const d of l.depts) expect(Number.isFinite(d.x) && Number.isFinite(d.y)).toBe(true);
    for (let i = 0; i < l.depts.length; i++) {
      for (let j = i + 1; j < l.depts.length; j++) {
        const a = l.depts[i]!, b = l.depts[j]!;
        expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeGreaterThanOrEqual(a.r + b.r - 1);
      }
      expect(Math.hypot(l.depts[i]!.x, l.depts[i]!.y)).toBeGreaterThan(l.depts[i]!.r); // not on the hub
    }
  });

  it("is deterministic: the same input gives the same picture", () => {
    expect(computeLayout(brain, mode, new Set())).toEqual(computeLayout(brain, mode, new Set()));
  });
});

describe("expanding", () => {
  it("shows a department's members only when it is expanded, inside its bubble", () => {
    const closed = computeLayout(brain, "circle", new Set());
    expect(closed.members).toHaveLength(0);
    const open = computeLayout(brain, "circle", new Set([deptId("api")]));
    const api = open.depts.find((d) => d.group === "api")!;
    expect(open.members).toHaveLength(40);
    expect(api.expanded).toBe(true);
    for (const m of open.members) expect(Math.hypot(m.x - api.x, m.y - api.y)).toBeLessThanOrEqual(api.r + 1);
  });

  it("caps very large departments and reports how many are hidden", () => {
    const big = buildBrain("big", { files: files("lib", MAX_MEMBERS_PER_DEPT + 25), imports: [], changed: [], truncated: false }, []);
    const l = computeLayout(big, "force", new Set([deptId("lib")]));
    expect(l.members).toHaveLength(MAX_MEMBERS_PER_DEPT);
    expect(l.hidden.get(deptId("lib"))).toBe(25);
  });

  it.each(MODES)("%s: an opened area stays put, pushes its neighbours aside and nothing overlaps", (mode) => {
    const closed = computeLayout(brain, mode, new Set());
    const open = computeLayout(brain, mode, new Set([deptId("api"), deptId("web")]));
    // the opened areas themselves are not moved by being opened...
    for (const g of ["api", "web"]) {
      const c = closed.depts.find((d) => d.group === g)!;
      const o = open.depts.find((d) => d.group === g)!;
      expect(o.r).toBeGreaterThan(c.r);
    }
    // ...and no two bubbles overlap
    for (let i = 0; i < open.depts.length; i++) {
      for (let j = i + 1; j < open.depts.length; j++) {
        const a = open.depts[i]!, b = open.depts[j]!;
        expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeGreaterThanOrEqual(a.r + b.r - 1);
      }
    }
    // closing puts everything back exactly
    expect(computeLayout(brain, mode, new Set())).toEqual(closed);
  });

  it("a single opened area keeps its own centre", () => {
    const closed = computeLayout(brain, "rings", new Set());
    const open = computeLayout(brain, "rings", new Set([deptId("api")]));
    const c = closed.depts.find((d) => d.group === "api")!;
    const o = open.depts.find((d) => d.group === "api")!;
    expect([o.x, o.y]).toEqual([c.x, c.y]);
  });

  it("spread mode (expand all) sizes the rings by what is open, so open areas do not overlap", () => {
    const all = new Set(departmentsOf(brain).depts.map((d) => d.id));
    const l = computeLayout(brain, "rings", all, { spread: true });
    for (let i = 0; i < l.depts.length; i++) {
      for (let j = i + 1; j < l.depts.length; j++) {
        const a = l.depts[i]!, b = l.depts[j]!;
        expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeGreaterThanOrEqual(a.r + b.r - 1);
      }
    }
  });

  it("counts imports between departments", () => {
    const l = computeLayout(brain, "force", new Set());
    const between = (a: string, b: string) => l.links.find((k) => [k.a, k.b].sort().join() === [deptId(a), deptId(b)].sort().join())?.weight;
    expect(between("api", "web")).toBe(2);
    expect(between("web", "docs")).toBe(1);
  });
});

describe("folder families inside an open department", () => {
  const mk = (path: string, lines = 10) => ({ path, language: "python", lines, symbols: 0 });
  const wide = buildBrain("fam", {
    files: [
      ...Array.from({ length: 20 }, (_, i) => mk(`app/models/m${i}.py`)),
      ...Array.from({ length: 20 }, (_, i) => mk(`app/views/v${i}.py`)),
      ...Array.from({ length: 30 }, (_, i) => mk(`docs/d${i}.md`)),
      ...Array.from({ length: 30 }, (_, i) => mk(`web/w${i}.ts`)),
    ],
    imports: [], changed: [], truncated: false,
  }, []);
  const open = computeLayout(wide, "rings", new Set([deptId("app")]));

  it("groups the files by the folder below the department", () => {
    expect(open.families.map((f) => [f.key, f.count]).sort()).toEqual([["models", 20], ["views", 20]]);
  });

  it("keeps a folder's files together and the families apart", () => {
    const of = (family: string) => open.members.filter((m) => m.family === family);
    const centre = (family: string) => open.families.find((f) => f.key === family)!;
    for (const family of ["models", "views"]) {
      const c = centre(family);
      for (const m of of(family)) expect(Math.hypot(m.x - c.x, m.y - c.y)).toBeLessThanOrEqual(c.r);
    }
    const a = centre("models"), b = centre("views");
    expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeGreaterThanOrEqual(a.r + b.r);
  });

  it("keeps every dot inside the department disc", () => {
    const app = open.depts.find((d) => d.group === "app")!;
    for (const m of open.members) expect(Math.hypot(m.x - app.x, m.y - app.y)).toBeLessThanOrEqual(app.r);
  });
});

describe("folders view", () => {
  it("lists the top level first and opens folders in place", () => {
    const closed = treeRows(brain, new Set());
    expect(closed.map((r) => r.label)).toEqual(["demo · 74 files", "api/ · 40", "web/ · 25", "docs/ · 8", "README.md"]);
    const open = treeRows(brain, new Set([dirId("docs")]));
    expect(open.map((r) => r.label).slice(0, 5)).toEqual(["demo · 74 files", "api/ · 40", "web/ · 25", "docs/ · 8", "f000.py"]);
  });
});
