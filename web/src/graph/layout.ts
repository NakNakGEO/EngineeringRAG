/** Static layouts for the project map. Everything here is computed once per change and then
 * drawn as-is: there is no physics loop and nothing moves on screen. */
import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import { packFamilies, type Family } from "./pack";
import { fileId, noteNodeId, PROJECT_ID, type Brain, type BrainNode } from "./model";

export type LayoutMode = "force" | "circle" | "rings" | "hex";

export const NOTES_GROUP = "(notes)";
export const OTHER_GROUP = "(other)";
/** More departments than this make the map unreadable: the smallest are merged into "other". */
export const MAX_DEPARTMENTS = 15;
export const deptId = (group: string) => `dept:${group}`;

export interface Dept { id: string; group: string; label: string; count: number; isNotes: boolean }
export interface PlacedDept extends Dept { x: number; y: number; r: number; expanded: boolean }
export interface PlacedMember { id: string; deptId: string; x: number; y: number; node: BrainNode; family: string }
export interface PlacedFamily extends Family { deptId: string }
export interface MapLayout {
  hub: { x: number; y: number };
  depts: PlacedDept[];
  members: PlacedMember[];
  /** members that did not fit under the per-department cap, by department id */
  hidden: Map<string, number>;
  /** department -> department import counts (undirected, summed) */
  links: { a: string; b: string; weight: number }[];
  /** folder families inside the open departments, with absolute positions */
  families: PlacedFamily[];
}

export const MAX_MEMBERS_PER_DEPT = 500;
export const MAX_MEMBERS_TOTAL = 3200;

export function departmentsOf(brain: Brain): { depts: Dept[]; of: Map<string, string> } {
  const counts = new Map<string, number>();
  const of = new Map<string, string>(); // node id -> dept id
  for (const n of brain.nodes) {
    if (n.kind === "file") {
      counts.set(n.group, (counts.get(n.group) ?? 0) + 1);
      of.set(n.id, deptId(n.group));
    } else if (n.kind === "note") {
      counts.set(NOTES_GROUP, (counts.get(NOTES_GROUP) ?? 0) + 1);
      of.set(n.id, deptId(NOTES_GROUP));
    }
  }
  const all = [...counts].map(([group, count]) => ({
    id: deptId(group), group, count, isNotes: group === NOTES_GROUP,
    label: group === NOTES_GROUP ? "NOTES" : group.toUpperCase(),
  }));
  all.sort((a, b) => b.count - a.count || a.group.localeCompare(b.group));
  const notes = all.filter((d) => d.isNotes);
  const real = all.filter((d) => !d.isNotes);
  let depts = all;
  if (real.length > MAX_DEPARTMENTS) {
    const keep = real.slice(0, MAX_DEPARTMENTS - 1);
    const rest = real.slice(MAX_DEPARTMENTS - 1);
    const merged = new Set(rest.map((d) => d.id));
    for (const [node, d] of of) if (merged.has(d)) of.set(node, deptId(OTHER_GROUP));
    const other = { id: deptId(OTHER_GROUP), group: OTHER_GROUP, count: rest.reduce((n, d) => n + d.count, 0), isNotes: false, label: `OTHER · ${rest.length} AREAS` };
    depts = [...keep, other, ...notes];
  }
  return { depts, of };
}

/** Radius a department needs: collapsed it is a bubble; expanded it holds a sunflower of dots. */
export function deptRadius(count: number, expanded: boolean, step: number): number {
  const shown = Math.min(count, MAX_MEMBERS_PER_DEPT);
  return expanded ? Math.max(46, step * Math.sqrt(shown + 1) + 24) : 30 + Math.min(34, Math.sqrt(count) * 3.2);
}

function crossLinks(brain: Brain, of: Map<string, string>) {
  const w = new Map<string, number>();
  for (const e of brain.edges) {
    if (e.kind !== "imports") continue;
    const a = of.get(e.source);
    const b = of.get(e.target);
    if (!a || !b || a === b) continue;
    const key = a < b ? `${a}|${b}` : `${b}|${a}`;
    w.set(key, (w.get(key) ?? 0) + 1);
  }
  return [...w].map(([k, weight]) => {
    const [a, b] = k.split("|") as [string, string];
    return { a, b, weight };
  });
}

function hexSpiral(n: number): [number, number][] {
  const out: [number, number][] = [[0, 0]];
  const dirs: [number, number][] = [[1, 0], [1, -1], [0, -1], [-1, 0], [-1, 1], [0, 1]];
  for (let ring = 1; out.length < n; ring++) {
    let q = -ring, r = ring; // start at the south-west corner of the ring
    for (const [dq, dr] of dirs) {
      for (let i = 0; i < ring && out.length < n; i++) {
        out.push([q, r]);
        q += dq;
        r += dr;
      }
    }
  }
  return out.slice(0, n);
}

/** Push overlapping bubbles apart. The hub (index 0) never moves. */
function separate(items: { x: number; y: number; r: number }[], gap = 28, rounds = 80): void {
  for (let round = 0; round < rounds; round++) {
    let moved = false;
    for (let i = 0; i < items.length; i++) {
      for (let j = i + 1; j < items.length; j++) {
        const a = items[i]!, b = items[j]!;
        let dx = b.x - a.x, dy = b.y - a.y;
        let d = Math.hypot(dx, dy);
        const need = a.r + b.r + gap;
        if (d >= need) continue;
        if (d < 1e-6) { dx = Math.cos(i + j); dy = Math.sin(i + j); d = 1; }
        const push = (need - d) / 2;
        const ux = dx / d, uy = dy / d;
        if (i === 0) { b.x += ux * push * 2; b.y += uy * push * 2; }
        else { a.x -= ux * push; a.y -= uy * push; b.x += ux * push; b.y += uy * push; }
        moved = true;
      }
    }
    if (!moved) break;
  }
}


/** Open areas stay where they are; every bubble one of them overlaps is pushed away along the line
 * between the two centres, and whatever that pushes is pushed in turn. Two open areas share the work. */
function pushApart(items: { x: number; y: number; r: number; pinned: boolean }[], gap = 26, rounds = 240): void {
  for (let round = 0; round < rounds; round++) {
    let moved = false;
    for (let i = 0; i < items.length; i++) {
      for (let j = i + 1; j < items.length; j++) {
        const a = items[i]!, b = items[j]!;
        let dx = b.x - a.x, dy = b.y - a.y;
        let d = Math.hypot(dx, dy);
        const need = a.r + b.r + gap;
        if (d >= need - 0.01) continue;
        if (d < 1e-6) { dx = Math.cos(i + j); dy = Math.sin(i + j); d = 1; }
        const over = need - d, ux = dx / d, uy = dy / d;
        if (a.pinned && !b.pinned) { b.x += ux * over; b.y += uy * over; }
        else if (!a.pinned && b.pinned) { a.x -= ux * over; a.y -= uy * over; }
        else { a.x -= (ux * over) / 2; a.y -= (uy * over) / 2; b.x += (ux * over) / 2; b.y += (uy * over) / 2; }
        moved = true;
      }
    }
    if (!moved) break;
  }
}

/** Related departments end up close together: one small force run, relative to the hub. */
function forcePositions(depts: Dept[], links: { a: string; b: string; weight: number }[]): { x: number; y: number }[] {
  const g = new Graph({ type: "undirected", multi: false });
  g.addNode(PROJECT_ID, { x: 0, y: 0, size: 6 });
  depts.forEach((d, i) => {
    const a = (i / Math.max(1, depts.length)) * Math.PI * 2;
    g.addNode(d.id, { x: Math.cos(a) * 100, y: Math.sin(a) * 100, size: 3 });
    g.addEdge(PROJECT_ID, d.id, { weight: 0.3 });
  });
  for (const l of links) if (g.hasNode(l.a) && g.hasNode(l.b) && !g.hasEdge(l.a, l.b)) g.addEdge(l.a, l.b, { weight: Math.log2(l.weight + 1) });
  if (g.order > 2) forceAtlas2.assign(g, { iterations: 400, settings: { ...forceAtlas2.inferSettings(g), gravity: 3, scalingRatio: 4, edgeWeightInfluence: 1, strongGravityMode: true } });
  const hub = g.getNodeAttributes(PROJECT_ID);
  return depts.map((d) => {
    const q = g.getNodeAttributes(d.id);
    return { x: (q.x as number) - (hub.x as number), y: (q.y as number) - (hub.y as number) };
  });
}

export function computeLayout(
  brain: Brain,
  mode: LayoutMode,
  expanded: ReadonlySet<string>,
  opts: { spacing?: number; spread?: boolean } = {},
): MapLayout {
  const spacing = opts.spacing ?? 1;
  // By default an open area grows where it is and pushes its neighbours aside (see below).
  // "Spread" (expand all) sizes every ring by what is open instead, so nothing overlaps.
  const spread = opts.spread ?? false;
  const step = 11 * spacing;
  const { depts, of } = departmentsOf(brain);
  const links = crossLinks(brain, of);
  const hubR = 34;

  // Cap the number of drawn dots overall: expanding "everything" must stay drawable.
  const byDept = new Map<string, BrainNode[]>();
  for (const n of brain.nodes) {
    const d = of.get(n.id);
    if (d) byDept.set(d, [...(byDept.get(d) ?? []), n]);
  }
  let budget = MAX_MEMBERS_TOTAL;
  const packs = new Map<string, ReturnType<typeof packFamilies>>();
  for (const d of depts) {
    if (!expanded.has(d.id) || budget <= 0) continue;
    const cap = Math.min(MAX_MEMBERS_PER_DEPT, budget);
    const pack = packFamilies(byDept.get(d.id) ?? [], { step, cap, byGroup: d.group === OTHER_GROUP });
    budget -= pack.members.length;
    packs.set(d.id, pack);
  }
  const bubbles = [
    { x: 0, y: 0, r: hubR },
    ...depts.map((d) => {
      const pack = packs.get(d.id);
      return { x: 0, y: 0, r: spread && pack ? Math.max(52, pack.reach) : deptRadius(d.count, false, step) };
    }),
  ];
  const sumR = bubbles.slice(1).reduce((s, b) => s + b.r, 0);
  const forceRaw = forcePositions(depts, links);
  const forceSpan = Math.max(1, ...forceRaw.map((q) => Math.hypot(q.x, q.y)));
  const forceScale = Math.max(240, sumR * 0.5) / forceSpan;
  // angular order of every department around the hub, from the force run: neighbours stay together
  const byAngle = depts.map((_, i) => i).sort((a, b) => Math.atan2(forceRaw[a]!.y, forceRaw[a]!.x) - Math.atan2(forceRaw[b]!.y, forceRaw[b]!.x));

  if (mode === "circle") {
    const ring = Math.max(260, (sumR * 2.2) / Math.PI + hubR);
    byAngle.forEach((idx, k) => {
      const a = (k / Math.max(1, depts.length)) * Math.PI * 2 - Math.PI / 2;
      bubbles[idx + 1]!.x = Math.cos(a) * ring;
      bubbles[idx + 1]!.y = Math.sin(a) * ring;
    });
  } else if (mode === "rings") {
    let i = 0, ringIndex = 0, inner = hubR + 40;
    while (i < depts.length) {
      const cap = 4 + ringIndex * 4;
      const inRing = byAngle.filter((idx) => idx >= i && idx < i + cap); // this ring's departments, by angle
      const slice = inRing.map((idx) => bubbles[idx + 1]!);
      const widest = Math.max(...slice.map((b) => b.r));
      const radius = Math.max(inner + widest, (slice.reduce((s, b) => s + 2 * b.r + 30, 0)) / (2 * Math.PI));
      slice.forEach((b, k) => {
        const a = (k / slice.length) * Math.PI * 2 - Math.PI / 2 + ringIndex * 0.35;
        b.x = Math.cos(a) * radius;
        b.y = Math.sin(a) * radius;
      });
      inner = radius + widest + 24;
      i += slice.length;
      ringIndex++;
    }
  } else if (mode === "hex") {
    const cell = Math.max(...bubbles.map((b) => b.r)) * 2 + 36;
    const spots = hexSpiral(depts.length + 1);
    depts.forEach((_, i) => {
      const [q, r] = spots[i + 1]!;
      bubbles[i + 1]!.x = cell * (q + r / 2);
      bubbles[i + 1]!.y = cell * (r * Math.sqrt(3) / 2);
    });
  } else {
    forceRaw.forEach((q, i) => {
      bubbles[i + 1]!.x = q.x * forceScale;
      bubbles[i + 1]!.y = q.y * forceScale;
    });
  }
  separate(bubbles);

  // The closed picture above is the resting state. Opening areas grows them in place and pushes
  // their neighbours aside from it, so closing always puts everything back exactly.
  if (!spread && packs.size > 0) {
    const items = depts.map((d, i) => {
      const b = bubbles[i + 1]!;
      const pk = packs.get(d.id);
      return { x: b.x, y: b.y, r: pk ? Math.max(52, pk.reach) : b.r, pinned: !!pk };
    });
    pushApart(items);
    items.forEach((it, i) => { bubbles[i + 1]!.x = it.x; bubbles[i + 1]!.y = it.y; });
  }

  const placed: PlacedDept[] = depts.map((d, i) => ({
    ...d, x: bubbles[i + 1]!.x, y: bubbles[i + 1]!.y, r: packs.has(d.id) ? Math.max(52, packs.get(d.id)!.reach) : bubbles[i + 1]!.r, expanded: packs.has(d.id),
  }));

  const members: PlacedMember[] = [];
  const families: PlacedFamily[] = [];
  const hidden = new Map<string, number>();
  for (const d of placed) {
    const pack = packs.get(d.id);
    if (!pack) continue;
    for (const m of pack.members) members.push({ id: m.node.id, deptId: d.id, x: d.x + m.x, y: d.y + m.y, node: m.node, family: m.family });
    for (const f of pack.families) families.push({ ...f, x: d.x + f.x, y: d.y + f.y, deptId: d.id });
    if (pack.hidden > 0) hidden.set(d.id, pack.hidden);
  }
  return { hub: { x: bubbles[0]!.x, y: bubbles[0]!.y }, depts: placed, members, hidden, links, families };
}

export { fileId, noteNodeId };
