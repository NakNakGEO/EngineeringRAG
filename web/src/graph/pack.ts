/** Inside an open department the files are grouped into folder "families": each family is a small
 * sunflower of dots, and the families are packed side by side without overlap. Static; computed
 * once per change. Files of one folder therefore sit together, which is what lets you see a
 * file's relatives at a glance. */
import type { BrainNode } from "./model";

export interface Family { key: string; label: string; count: number; x: number; y: number; r: number }
export interface PackedMember { node: BrainNode; x: number; y: number; family: string }
export interface Pack { members: PackedMember[]; families: Family[]; reach: number; hidden: number }

const GOLDEN = Math.PI * (3 - Math.sqrt(5));

/** The folder family of a file inside its department: the first folder below the department's own
 * path (or "." for files that sit directly in it). In "other" every area is its own family. */
export function familyOf(node: BrainNode, byGroup: boolean): string {
  if (node.kind === "note") return "notes";
  if (byGroup) return node.group;
  const path = node.path ?? "";
  const prefix = node.group.startsWith("(") ? "" : `${node.group}/`;
  const rest = prefix && path.startsWith(prefix) ? path.slice(prefix.length) : path;
  const i = rest.indexOf("/");
  return i < 0 ? "." : rest.slice(0, i);
}

function disc(n: number, step: number): { x: number; y: number }[] {
  return Array.from({ length: n }, (_, i) => {
    const rr = step * Math.sqrt(i + 0.6);
    const a = i * GOLDEN;
    return { x: Math.cos(a) * rr, y: Math.sin(a) * rr };
  });
}

export function packFamilies(
  nodes: BrainNode[],
  opts: { step: number; cap: number; byGroup: boolean },
): Pack {
  const { step, cap, byGroup } = opts;
  // the biggest files survive the cap
  const taken = [...nodes].sort((a, b) => b.lines - a.lines || a.label.localeCompare(b.label)).slice(0, cap);
  const groups = new Map<string, BrainNode[]>();
  for (const n of taken) {
    const k = familyOf(n, byGroup);
    groups.set(k, [...(groups.get(k) ?? []), n]);
  }
  const fams = [...groups]
    .map(([key, list]) => ({ key, list: list.sort((a, b) => (a.path ?? a.label).localeCompare(b.path ?? b.label)) }))
    .sort((a, b) => b.list.length - a.list.length || a.key.localeCompare(b.key));

  const placed: Family[] = [];
  const gap = step * 1.3;
  const members: PackedMember[] = [];
  for (const f of fams) {
    const r = step * Math.sqrt(f.list.length + 0.6) + step * 0.7;
    let x = 0, y = 0;
    if (placed.length > 0) {
      // walk outwards along a spiral until the family fits beside the ones already placed
      const first = placed[0]!;
      for (let t = 0; t < 4000; t++) {
        const dist = first.r * 0.4 + r + t * (step * 0.35);
        const a = t * 0.55;
        const cx = Math.cos(a) * dist, cy = Math.sin(a) * dist;
        if (placed.every((p) => Math.hypot(p.x - cx, p.y - cy) >= p.r + r + gap)) { x = cx; y = cy; break; }
      }
    }
    placed.push({ key: f.key, label: f.key === "." ? "loose files" : f.key, count: f.list.length, x, y, r });
    const offsets = disc(f.list.length, step);
    f.list.forEach((node, i) => members.push({ node, x: x + offsets[i]!.x, y: y + offsets[i]!.y, family: f.key }));
  }
  // Recentre on (roughly) the smallest circle around all the families: the open disc is as small
  // as it can be, so less has to make room for it.
  if (placed.length > 1) {
    let cx = (Math.min(...placed.map((p) => p.x - p.r)) + Math.max(...placed.map((p) => p.x + p.r))) / 2;
    let cy = (Math.min(...placed.map((p) => p.y - p.r)) + Math.max(...placed.map((p) => p.y + p.r))) / 2;
    for (let i = 0; i < 60; i++) {
      let far = placed[0]!, farD = -1;
      for (const p of placed) {
        const d = Math.hypot(p.x - cx, p.y - cy) + p.r;
        if (d > farD) { farD = d; far = p; }
      }
      const dx = far.x - cx, dy = far.y - cy, len = Math.hypot(dx, dy) || 1;
      // towards the outer edge of the farthest family, a shrinking step each round
      cx += ((far.x + (dx / len) * far.r) - cx) / (i + 3);
      cy += ((far.y + (dy / len) * far.r) - cy) / (i + 3);
    }
    for (const p of placed) { p.x -= cx; p.y -= cy; }
    for (const m of members) { m.x -= cx; m.y -= cy; }
  }
  const reach = placed.reduce((m, p) => Math.max(m, Math.hypot(p.x, p.y) + p.r), 0) + 20;
  return { members, families: placed, reach, hidden: Math.max(0, nodes.length - taken.length) };
}
