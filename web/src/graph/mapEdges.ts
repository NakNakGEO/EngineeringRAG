/** The lines of the project map. Every badge has a line that points at it; see buildEdges. */
import type { Edge } from "@xyflow/react";
import type { MapLayout } from "./layout";
import { IN_COLOR, OUT_COLOR, Z, type BubbleData, type DeptRel } from "./mapStyle";
import type { Brain } from "./model";

export interface EdgeOptions { brain: Brain; selected: string | null; showImports: boolean }

const edge = (id: string, source: string, target: string, rs: number, rt: number, style: Edge["style"], text?: string, bend = 0): Edge => ({
  id, source, target, type: "bubble", zIndex: Z.line, data: { rs, rt, text, bend } satisfies BubbleData, style,
});

/** Every badge has a line that points at it, and nothing else is drawn by default:
 *  - hovering an area: a line to each area it has imports with (the "N imports" badges);
 *  - a selected file: an orange line to each area holding files it imports, a blue line from each
 *    area holding files that import it (the → / ← badges), plus short lines to relatives next to it;
 *  - "Links between areas" (opt-in): the strongest few links, thin and grey.
 * Lines are curved, start and end on the bubbles' edges and sit behind them. */
export function buildEdges(
  p: EdgeOptions, layout: MapLayout, shown: Set<string>, rel: { out: Set<string>; into: Set<string> },
  hover: string | null, deptRel: DeptRel, of: Map<string, string>, covered: Set<string>,
): Edge[] {
  const edges: Edge[] = [];
  const radius = new Map(layout.depts.filter((d) => !covered.has(d.id)).map((d) => [d.id, d.r]));
  const at = new Map(layout.members.map((m) => [m.id, m]));
  const dotR = (id: string) => Math.max(6, Math.min(14, (at.get(id)?.node.size ?? 3) * 2.4)) / 2 + 1;
  const hovered = hover && radius.has(hover) ? hover : null;

  if (p.showImports) {
    const perDept = new Map<string, number>();
    const strongest = [...layout.links].filter((l) => l.a !== hovered && l.b !== hovered && radius.has(l.a) && radius.has(l.b)).sort((a, b) => b.weight - a.weight).filter((l) => {
      const na = perDept.get(l.a) ?? 0, nb = perDept.get(l.b) ?? 0;
      if (na >= 2 || nb >= 2) return false;
      perDept.set(l.a, na + 1);
      perDept.set(l.b, nb + 1);
      return true;
    }).slice(0, 14);
    const top = Math.max(1, ...strongest.map((l) => l.weight));
    for (const l of strongest) {
      edges.push(edge(`l:${l.a}|${l.b}`, l.a, l.b, radius.get(l.a) ?? 0, radius.get(l.b) ?? 0,
        { stroke: "var(--muted-foreground)", strokeWidth: 1 + (1.5 * l.weight) / top, opacity: 0.28 }, undefined, 0.12));
    }
  }

  if (hovered) {
    const mine = layout.links.filter((l) => l.a === hovered || l.b === hovered);
    const top = Math.max(1, ...mine.map((l) => l.weight));
    for (const l of mine) {
      const other = l.a === hovered ? l.b : l.a;
      if (!radius.has(other)) continue;
      edges.push(edge(`hov:${hovered}|${other}`, hovered, other, radius.get(hovered) ?? 0, radius.get(other) ?? 0,
        { stroke: OUT_COLOR, strokeWidth: 1.5 + (2.5 * l.weight) / top, opacity: 0.85 }, undefined, 0.12));
    }
  }

  const sel = p.selected && p.selected.startsWith("file:") ? p.selected : null;
  if (sel) {
    // the dot when its area is open, otherwise the (closed) area that holds it
    const home = of.get(sel);
    const origin = shown.has(sel) ? sel : home && radius.has(home) ? home : null;
    const originR = origin === sel ? dotR(sel) : radius.get(origin ?? "") ?? 0;
    if (origin) {
      for (const [areaId, r] of deptRel) {
        if (areaId === home && origin === sel) continue; // same open area: short lines below
        const ar = radius.get(areaId);
        if (ar === undefined || areaId === origin) continue;
        if (r.out > 0) edges.push(edge(`area:out:${areaId}`, origin, areaId, originR, ar, { stroke: OUT_COLOR, strokeWidth: 1.5 + Math.min(2.5, Math.log2(1 + r.out) / 2), opacity: 0.9 }, undefined, 0.1));
        if (r.in > 0) edges.push(edge(`area:in:${areaId}`, areaId, origin, ar, originR, { stroke: IN_COLOR, strokeWidth: 1.5 + Math.min(2.5, Math.log2(1 + r.in) / 2), opacity: 0.9 }, undefined, 0.1));
      }
    }
    const me = at.get(sel);
    if (me) {
      const near = (id: string) => { const o = at.get(id); return !!o && o.deptId === me.deptId && Math.hypot(o.x - me.x, o.y - me.y) < 420; };
      for (const id of rel.out) if (near(id)) edges.push(edge(`rel:out:${id}`, sel, id, dotR(sel), dotR(id), { stroke: OUT_COLOR, strokeWidth: 1.5, opacity: 0.9 }, undefined, 0.1));
      for (const id of rel.into) if (near(id)) edges.push(edge(`rel:in:${id}`, id, sel, dotR(id), dotR(sel), { stroke: IN_COLOR, strokeWidth: 1.5, opacity: 0.9 }, undefined, 0.1));
    }
  }
  for (const e of p.brain.edges) {
    // note <-> file links only for the picked note or file (all of them at once is a tangle)
    if (e.kind === "documents" && shown.has(e.source) && shown.has(e.target) && (e.source === p.selected || e.target === p.selected)) {
      edges.push(edge(e.id, e.source, e.target, dotR(e.source), dotR(e.target), { stroke: "var(--success)", strokeWidth: 1, opacity: 0.5 }));
    }
  }
  return edges;
}

