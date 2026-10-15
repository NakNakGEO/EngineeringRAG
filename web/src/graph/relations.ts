/** Which files and areas are related to a picked file or a hovered area. Pure data, no drawing. */
import type { MapLayout } from "./layout";
import type { DeptRel } from "./mapStyle";
import { PROJECT_ID, type Brain } from "./model";

/** What the selected file imports, and what imports it (file ids). */
export function relatives(brain: Brain, id: string | null): { out: Set<string>; into: Set<string> } {
  const out = new Set<string>();
  const into = new Set<string>();
  if (!id || !id.startsWith("file:")) return { out, into };
  for (const e of brain.edges) {
    if (e.kind !== "imports") continue;
    if (e.source === id) out.add(e.target);
    else if (e.target === id) into.add(e.source);
  }
  return { out, into };
}


/** Per area: how many files here the selected file imports / is imported by, and, for the hovered
 * area, how many imports tie each other area to it. Counts on the bubbles, not lines. */
export function areaRelations(layout: MapLayout, rel: { out: Set<string>; into: Set<string> }, of: Map<string, string>, hover: string | null): DeptRel {
  const m: DeptRel = new Map();
  const at = (id: string) => { let v = m.get(id); if (!v) { v = { out: 0, in: 0, hot: 0 }; m.set(id, v); } return v; };
  for (const id of rel.out) { const d = of.get(id); if (d) at(d).out++; }
  for (const id of rel.into) { const d = of.get(id); if (d) at(d).in++; }
  if (hover) for (const l of layout.links) {
    if (l.a === hover) at(l.b).hot += l.weight;
    else if (l.b === hover) at(l.a).hot += l.weight;
  }
  return m;
}


/** Closed bubbles (and the hub) that an open area's disc sits on. They are not drawn while it is open,
 * unless they matter right now (they hold relatives of the selected file / the hovered area). */
export function coveredAreas(layout: MapLayout): Set<string> {
  const covered = new Set<string>();
  const open = layout.depts.filter((d) => d.expanded);
  if (open.length === 0) return covered;
  for (const d of layout.depts) {
    if (!d.expanded && open.some((o) => Math.hypot(o.x - d.x, o.y - d.y) < o.r + 0.25 * d.r)) covered.add(d.id);
  }
  if (open.some((o) => Math.hypot(o.x - layout.hub.x, o.y - layout.hub.y) < o.r + 8)) covered.add(PROJECT_ID);
  return covered;
}


