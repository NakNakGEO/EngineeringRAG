/** "Folders" view: project -> folders -> files as an indented outline. Computed, static. */
import { dirId, PROJECT_ID, type Brain, type BrainNode } from "./model";

export const MAX_PER_FOLDER = 60;
export const ROW = 34;
export const COL = 290;

export interface TreeRow {
  id: string;
  node: BrainNode | null; // null for the "+N more" row
  label: string;
  depth: number;
  row: number;
  open: boolean;
  expandable: boolean;
  parent: string | null;
  x: number;
  y: number;
}

/** `open` holds dir node ids (`dir:<path>`). The project row is always open. */
export function treeRows(brain: Brain, open: ReadonlySet<string>): TreeRow[] {
  const byId = new Map(brain.nodes.map((n) => [n.id, n]));
  const kids = new Map<string, BrainNode[]>();
  for (const e of brain.edges) {
    if (e.kind !== "contains") continue;
    const child = byId.get(e.target);
    if (child) kids.set(e.source, [...(kids.get(e.source) ?? []), child]);
  }
  const out: TreeRow[] = [];
  const add = (node: BrainNode | null, label: string, depth: number, parent: string | null, isOpen: boolean, expandable: boolean, id?: string) => {
    out.push({ id: id ?? node!.id, node, label, depth, row: out.length, open: isOpen, expandable, parent, x: depth * COL, y: out.length * ROW });
  };
  const root = byId.get(PROJECT_ID);
  if (!root) return out;
  add(root, `${root.label} · ${root.children} files`, 0, null, true, false);

  const walk = (id: string, depth: number) => {
    const list = [...(kids.get(id) ?? [])].sort((a, b) => (a.kind === b.kind ? b.children - a.children || a.label.localeCompare(b.label) : a.kind === "dir" ? -1 : 1));
    for (const n of list.slice(0, MAX_PER_FOLDER)) {
      const isDir = n.kind === "dir";
      const isOpen = isDir && open.has(n.id);
      add(n, isDir ? `${n.label}/ · ${n.children}` : n.label, depth, id, isOpen, isDir);
      if (isOpen) walk(n.id, depth + 1);
    }
    if (list.length > MAX_PER_FOLDER) add(null, `+${list.length - MAX_PER_FOLDER} more`, depth, id, false, false, `${id}:more`);
  };
  walk(PROJECT_ID, 1);
  return out;
}

export { dirId };
