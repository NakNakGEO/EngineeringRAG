import { describe, expect, it } from "vitest";
import { computeLayout, deptId, departmentsOf } from "../graph/layout";
import { buildEdges } from "../graph/mapEdges";
import { buildBrain, fileId, type Overview } from "../graph/model";
import { areaRelations, coveredAreas, relatives } from "../graph/relations";

const f = (path: string) => ({ path, language: "python", lines: 10, symbols: 1 });
// api/a.py -> web/x.py, web/y.py ; web/x.py -> docs/d.py ; api/a.py -> api/b.py (same area) ; docs/d.py -> api/a.py
const overview: Overview = {
  files: [f("api/a.py"), f("api/b.py"), f("api/c.py"), f("web/x.py"), f("web/y.py"), f("docs/d.py"), f("docs/e.py")],
  imports: [["api/a.py", "web/x.py"], ["api/a.py", "web/y.py"], ["web/x.py", "docs/d.py"], ["api/a.py", "api/b.py"], ["docs/d.py", "api/a.py"]],
  changed: [], truncated: false,
};
const brain = buildBrain("demo", overview, [{ id: "n1", title: "How it works", kind: "how-it-works", path: "api/a.py" }]);
const { of } = departmentsOf(brain);
const layout = computeLayout(brain, "rings", new Set());
const A = fileId("api/a.py");

describe("relatives", () => {
  it("lists what the file imports and what imports it, by file id", () => {
    const r = relatives(brain, A);
    expect([...r.out].sort()).toEqual([fileId("api/b.py"), fileId("web/x.py"), fileId("web/y.py")].sort());
    expect([...r.into]).toEqual([fileId("docs/d.py")]);
  });
  it("is empty for anything that is not a file", () => {
    expect(relatives(brain, null).out.size).toBe(0);
    expect(relatives(brain, "dir:api").into.size).toBe(0);
  });
});

describe("area badges", () => {
  it("count the picked file's imports and importers per area, own area included", () => {
    const m = areaRelations(layout, relatives(brain, A), of, null);
    expect(m.get(deptId("web"))).toMatchObject({ out: 2, in: 0 });
    expect(m.get(deptId("docs"))).toMatchObject({ out: 0, in: 1 });
    expect(m.get(deptId("api"))).toMatchObject({ out: 1, in: 0 }); // api/b.py, same area
  });
  it("count the imports between the hovered area and every other, and never depend on the file", () => {
    const m = areaRelations(layout, relatives(brain, null), of, deptId("web"));
    expect(m.get(deptId("api"))?.hot).toBe(2); // api/a.py -> web/x.py and web/y.py
    expect(m.get(deptId("docs"))?.hot).toBe(1); // web/x.py -> docs/d.py
  });
});

describe("lines", () => {
  const opts = (selected: string | null, showImports = false) => ({ brain, selected, showImports });
  const idsOf = (selected: string | null, hover: string | null, expanded = new Set<string>()) => {
    const l = computeLayout(brain, "rings", expanded);
    const rel = relatives(brain, selected);
    const deptRel = areaRelations(l, rel, of, hover);
    const shown = new Set(l.members.map((m) => m.id));
    return { l, edges: buildEdges(opts(selected), l, shown, rel, hover, deptRel, of, new Set()), deptRel };
  };

  it("a picked file gets one line per area that holds its relatives, and none to its own area", () => {
    const { edges, deptRel } = idsOf(A, null);
    const ids = edges.map((e) => e.id).filter((i) => i.startsWith("area:")).sort();
    expect(ids).toEqual([`area:in:${deptId("docs")}`, `area:out:${deptId("web")}`].sort());
    // every badge (other than the own area's) has exactly one line
    for (const [id, r] of deptRel) {
      if (id === deptId("api")) continue;
      expect(ids.includes(`area:out:${id}`)).toBe(r.out > 0);
      expect(ids.includes(`area:in:${id}`)).toBe(r.in > 0);
    }
  });

  it("directions are right: orange goes from the file to the area it imports from, blue the other way", () => {
    const { edges } = idsOf(A, null);
    const out = edges.find((e) => e.id === `area:out:${deptId("web")}`)!;
    const inn = edges.find((e) => e.id === `area:in:${deptId("docs")}`)!;
    expect(out.source).toBe(deptId("api")); // closed area: the line starts at the area that holds the file
    expect(out.target).toBe(deptId("web"));
    expect(inn.source).toBe(deptId("docs"));
    expect(inn.target).toBe(deptId("api"));
  });

  it("with the area open the lines start at the file's own dot", () => {
    const { edges } = idsOf(A, null, new Set([deptId("api")]));
    expect(edges.find((e) => e.id === `area:out:${deptId("web")}`)!.source).toBe(A);
    // and the same-area import becomes a short dot-to-dot line
    expect(edges.some((e) => e.id === `rel:out:${fileId("api/b.py")}`)).toBe(true);
  });

  it("hovering an area draws a line to each area it shares imports with", () => {
    const { edges, deptRel } = idsOf(null, deptId("web"));
    const hov = edges.filter((e) => e.id.startsWith("hov:")).map((e) => e.target).sort();
    expect(hov).toEqual([deptId("api"), deptId("docs")].sort());
    for (const [id, r] of deptRel) expect(hov.includes(id)).toBe(r.hot > 0);
  });

  it("nothing is drawn when nothing is picked or hovered", () => {
    expect(idsOf(null, null).edges).toHaveLength(0);
  });

  it("note-to-file links are drawn only for the picked note or file", () => {
    const l = computeLayout(brain, "rings", new Set([deptId("api"), deptId("(notes)")]));
    const shown = new Set(l.members.map((m) => m.id));
    const none = buildEdges(opts(null), l, shown, relatives(brain, null), null, new Map(), of, new Set());
    expect(none.filter((e) => e.id.includes("documents"))).toHaveLength(0);
    const picked = buildEdges(opts(A), l, shown, relatives(brain, A), null, new Map(), of, new Set());
    expect(picked.some((e) => e.source === A || e.target === A)).toBe(true);
  });
});

describe("covered bubbles", () => {
  it("are none when nothing is open", () => {
    expect(coveredAreas(computeLayout(brain, "rings", new Set())).size).toBe(0);
  });
  it("never include an open area itself", () => {
    const open = new Set([deptId("api"), deptId("web")]);
    const c = coveredAreas(computeLayout(brain, "rings", open));
    for (const id of open) expect(c.has(id)).toBe(false);
  });
});
