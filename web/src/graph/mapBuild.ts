/** The React Flow nodes of the project map (areas view and folders view). */
import type { Edge, Node } from "@xyflow/react";
import { OTHER_GROUP, type MapLayout } from "./layout";
import { groupColor, PROJECT_ID, type Brain } from "./model";
import { areaName, Z, type DeptData, type DeptRel, type DotData, type HubData, type Rel, type TagData, type TitleData, type TreeData } from "./mapStyle";
import { treeRows } from "./tree";

export interface BuildOptions {
  brain: Brain;
  selected: string | null;
  highlight: ReadonlySet<string> | null;
  changed: ReadonlySet<string>;
  dark: boolean;
  showNames: boolean;
  openDirs: ReadonlySet<string>;
}

/** Every node's size is known up front. Handing it to React Flow as `width`/`height` and `measured`
 * lets the camera frame nodes at once; otherwise each rebuilt node object is "unmeasured" until the
 * browser's ResizeObserver reports back, and a queued fit waits for that. */
export const sized = (width: number, height: number) => ({ width, height, measured: { width, height } });

export type Centers = Map<string, [number, number]>;

export function buildTree(p: BuildOptions): { nodes: Node[]; edges: Edge[]; centers: Centers } {
  const centers: Centers = new Map();
  const nodes: Node[] = [];
  const edges: Edge[] = [];
  const groups = p.brain.groups;
  for (const r of treeRows(p.brain, p.openDirs)) {
    const n = r.node;
    const color = n ? (n.kind === "project" ? "var(--primary)" : groupColor(n.group, groups, p.dark)) : "var(--input)";
    const changed = !!n?.path && n.kind === "file" && p.changed.has(n.path);
    nodes.push({
      id: r.id, type: "tree", position: { x: r.x, y: r.y }, ...sized(250, 26), draggable: false,
      data: { label: r.label, kind: n?.kind ?? "more", open: r.open, expandable: r.expandable, picked: p.selected === r.id, color, changed, more: !n } satisfies TreeData,
    });
    centers.set(r.id, [r.x + 125, r.y + 13]);
    if (r.parent) edges.push({ id: `t:${r.parent}>${r.id}`, source: r.parent, target: r.id, type: "smoothstep", style: { strokeWidth: 1, opacity: 0.6 } });
  }
  return { nodes, edges, centers };
}

function deptColorOf(d: { group: string; isNotes: boolean }, groups: string[], dark: boolean): string {
  return d.isNotes ? "var(--success)" : d.group === OTHER_GROUP ? "var(--muted-foreground)" : groupColor(d.group, groups, dark);
}

export function buildNodes(p: BuildOptions, layout: MapLayout, rel: { out: Set<string>; into: Set<string> }, of: Map<string, string>, deptRel: DeptRel, hover: string | null, covered: Set<string>): { nodes: Node[]; centers: Centers; shown: Set<string> } {
  const centers: Centers = new Map();
  const nodes: Node[] = [];
  const { brain } = p;
  const groups = brain.groups;
  const hit = new Set<string>();
  if (p.highlight) for (const id of p.highlight) { const d = of.get(id); if (d) hit.add(d); }

  const project = brain.nodes.find((n) => n.id === PROJECT_ID);
  if (!covered.has(PROJECT_ID)) {
    nodes.push({
      id: PROJECT_ID, type: "hub", position: { x: layout.hub.x - 34, y: layout.hub.y - 34 }, ...sized(68, 68), draggable: false, zIndex: Z.bubble,
      data: { label: project?.label ?? "project", count: project?.children ?? 0, picked: p.selected === PROJECT_ID } satisfies HubData,
    });
    centers.set(PROJECT_ID, [layout.hub.x, layout.hub.y]);
  }

  for (const d of layout.depts) {
    if (covered.has(d.id)) continue;
    const diameter = d.r * 2;
    nodes.push({
      id: d.id, type: "dept", position: { x: d.x - d.r, y: d.y - d.r }, ...sized(diameter, diameter), draggable: false, zIndex: d.expanded ? Z.disc : Z.bubble,
      data: {
        color: deptColorOf(d, groups, p.dark), label: d.label, count: d.count, d: diameter, expanded: d.expanded,
        more: layout.hidden.get(d.id) ?? 0, dim: !!p.highlight && !hit.has(d.id), reach: d.r,
        relOut: deptRel.get(d.id)?.out ?? 0, relIn: deptRel.get(d.id)?.in ?? 0, hot: deptRel.get(d.id)?.hot ?? 0, src: hover === d.id,
      } satisfies DeptData,
    });
    centers.set(d.id, [d.x, d.y]);
    if (d.expanded) {
      const nm = areaName(d.label);
      nodes.push({
        id: `title:${d.id}`, type: "title", position: { x: d.x, y: d.y - d.r - 14 }, draggable: false, selectable: false, focusable: false, zIndex: Z.title,
        data: {
          deptId: d.id, top: nm.top, leaf: nm.leaf, count: d.count, color: deptColorOf(d, groups, p.dark),
          relOut: deptRel.get(d.id)?.out ?? 0, relIn: deptRel.get(d.id)?.in ?? 0, hot: deptRel.get(d.id)?.hot ?? 0,
        } satisfies TitleData,
      });
    }
  }

  // folder names above their families (only when a department holds more than one family)
  const perDept = new Map<string, number>();
  for (const f of layout.families) perDept.set(f.deptId, (perDept.get(f.deptId) ?? 0) + 1);
  const deptById = new Map(layout.depts.map((d) => [d.id, d]));
  for (const f of layout.families) {
    if ((perDept.get(f.deptId) ?? 0) < 2 || f.count < 4) continue;
    const owner = deptById.get(f.deptId);
    nodes.push({
      id: `tag:${f.deptId}:${f.key}`, type: "tag", position: { x: f.x, y: f.y - f.r - 14 }, draggable: false, selectable: false, focusable: false, zIndex: Z.tag,
      style: { pointerEvents: "none" },
      data: { name: f.label, count: f.count, color: owner ? deptColorOf(owner, groups, p.dark) : "var(--muted-foreground)" } satisfies TagData,
    });
  }

  // the selected file's family: folder-mates, files it imports, files that import it
  const sel = p.selected && p.selected.startsWith("file:") ? p.selected : null;
  const selMember = sel ? layout.members.find((m) => m.id === sel) : undefined;
  const mates = new Set<string>();
  if (selMember) for (const m of layout.members) if (m.deptId === selMember.deptId && m.family === selMember.family) mates.add(m.id);

  const shown = new Set<string>();
  for (const m of layout.members) {
    shown.add(m.id);
    const n = m.node;
    const isNote = n.kind === "note";
    const size = isNote ? 13 : Math.max(6, Math.min(14, n.size * 2.4));
    const owner = deptById.get(m.deptId);
    // Inside "Other" the dots keep their own area's colour: it is a mix of small areas.
    const color = owner?.group === OTHER_GROUP ? groupColor(n.group, groups, p.dark) : owner ? deptColorOf(owner, groups, p.dark) : "var(--muted-foreground)";
    const r: Rel = m.id === sel ? "self" : rel.out.has(m.id) ? "out" : rel.into.has(m.id) ? "in" : mates.has(m.id) ? "folder" : null;
    nodes.push({
      id: m.id, type: "dot", position: { x: m.x - size / 2, y: m.y - size / 2 }, ...sized(size, size), draggable: false, zIndex: m.id === sel ? Z.picked : Z.dot,
      data: {
        color, size, label: n.path ?? n.label, showLabel: p.showNames,
        changed: !!n.path && p.changed.has(n.path),
        dim: (!!p.highlight && !p.highlight.has(m.id)) || (!!selMember && r === null),
        picked: p.selected === m.id, note: isNote, rel: r,
      } satisfies DotData,
    });
    centers.set(m.id, [m.x, m.y]);
  }
  return { nodes, centers, shown };
}

