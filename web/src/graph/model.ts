/** Pure model of the project "brain": what the nodes and links are. No rendering here.
 * Built from GET /projects/{id}/overview (files, imports, uncommitted paths) and the notes. */

export interface OverviewFile { path: string; language: string; lines: number; symbols: number }
export interface Overview {
  files: OverviewFile[];
  imports: [string, string][];
  changed: { path: string; status: string }[];
  truncated: boolean;
}
export interface NoteRef { id: string; title: string; kind: string; path: string | null }

export type NodeKind = "project" | "dir" | "file" | "note";
export type EdgeKind = "contains" | "imports" | "documents";

export interface BrainNode {
  id: string;
  kind: NodeKind;
  label: string;
  /** repo-relative path for dir/file nodes */
  path: string | null;
  /** top-level folder this node belongs to: its colour group */
  group: string;
  size: number;
  lines: number;
  symbols: number;
  language: string;
  noteId: string | null;
  children: number;
}
export interface BrainEdge { id: string; source: string; target: string; kind: EdgeKind }
export interface Brain {
  nodes: BrainNode[];
  edges: BrainEdge[];
  groups: string[];
  /** note ids by file/dir path, for the inspector */
  notesByPath: Map<string, string[]>;
}

export const PROJECT_ID = "project";
export const fileId = (path: string) => `file:${path}`;
export const dirId = (path: string) => `dir:${path}`;
export const noteNodeId = (id: string) => `note:${id}`;

const ROOT_GROUP = "(root)";

/** Colour groups are top-level folders; when one folder holds most of the project (a python
 * package such as `jesse/`), its sub-folders become the groups so the map is not one colour. */
function groupDepth(files: OverviewFile[]): 1 | 2 {
  const counts = new Map<string, number>();
  for (const f of files) {
    const top = f.path.includes("/") ? f.path.split("/")[0]! : ROOT_GROUP;
    counts.set(top, (counts.get(top) ?? 0) + 1);
  }
  const biggest = Math.max(0, ...counts.values());
  return files.length > 0 && biggest / files.length > 0.5 ? 2 : 1;
}

function groupOf(path: string, depth: 1 | 2): string {
  const parts = path.split("/");
  return parts.length <= 1 ? ROOT_GROUP : parts.slice(0, Math.min(depth, parts.length - 1)).join("/");
}

export function buildBrain(projectName: string, overview: Overview, notes: NoteRef[]): Brain {
  const nodes = new Map<string, BrainNode>();
  const edges = new Map<string, BrainEdge>();
  const depth = groupDepth(overview.files);
  const addEdge = (source: string, target: string, kind: EdgeKind) => {
    const id = `${kind}:${source}>${target}`;
    if (!edges.has(id)) edges.set(id, { id, source, target, kind });
  };

  nodes.set(PROJECT_ID, {
    id: PROJECT_ID, kind: "project", label: projectName, path: null, group: ROOT_GROUP,
    size: 16, lines: 0, symbols: 0, language: "", noteId: null, children: overview.files.length,
  });

  const dirCounts = new Map<string, number>();
  for (const f of overview.files) {
    const parts = f.path.split("/");
    for (let i = 1; i < parts.length; i++) {
      const dir = parts.slice(0, i).join("/");
      dirCounts.set(dir, (dirCounts.get(dir) ?? 0) + 1);
    }
  }
  for (const [dir, count] of dirCounts) {
    nodes.set(dirId(dir), {
      id: dirId(dir), kind: "dir", label: dir.split("/").at(-1)!, path: dir, group: groupOf(dir + "/x", depth),
      size: 4 + Math.min(9, Math.sqrt(count) * 0.55), lines: 0, symbols: 0, language: "", noteId: null, children: count,
    });
    const parent = dir.includes("/") ? dir.slice(0, dir.lastIndexOf("/")) : null;
    addEdge(parent === null ? PROJECT_ID : dirId(parent), dirId(dir), "contains");
  }

  const known = new Set<string>();
  for (const f of overview.files) {
    known.add(f.path);
    nodes.set(fileId(f.path), {
      id: fileId(f.path), kind: "file", label: f.path.split("/").at(-1)!, path: f.path, group: groupOf(f.path, depth),
      size: 1.6 + Math.min(4.5, Math.log2(f.lines + 2) * 0.45), lines: f.lines, symbols: f.symbols,
      language: f.language, noteId: null, children: 0,
    });
    const slash = f.path.lastIndexOf("/");
    addEdge(slash < 0 ? PROJECT_ID : dirId(f.path.slice(0, slash)), fileId(f.path), "contains");
  }

  for (const [src, dst] of overview.imports) {
    if (src !== dst && known.has(src) && known.has(dst)) addEdge(fileId(src), fileId(dst), "imports");
  }

  const notesByPath = new Map<string, string[]>();
  for (const n of notes) {
    const id = noteNodeId(n.id);
    nodes.set(id, {
      id, kind: "note", label: n.title, path: n.path, group: ROOT_GROUP, size: 6.5, lines: 0, symbols: 0,
      language: n.kind, noteId: n.id, children: 0,
    });
    const target = n.path && known.has(n.path) ? fileId(n.path) : PROJECT_ID;
    addEdge(id, target, "documents");
    if (n.path) notesByPath.set(n.path, [...(notesByPath.get(n.path) ?? []), n.id]);
  }

  const size = new Map<string, number>();
  for (const n of nodes.values()) if (n.kind === "file") size.set(n.group, (size.get(n.group) ?? 0) + 1);
  const groups = [...new Set([...nodes.values()].filter((n) => n.kind !== "note").map((n) => n.group))].sort(
    (a, b) => (size.get(b) ?? 0) - (size.get(a) ?? 0) || a.localeCompare(b),
  );
  return { nodes: [...nodes.values()], edges: [...edges.values()], groups, notesByPath };
}

/** A hand-picked palette (readable on near-black and on paper). Departments are ordered by size,
 * so the biggest areas get the most distinct colours and the order is stable between loads. */
const PALETTE_DARK = ["#8b7cf6", "#f472b6", "#fb923c", "#2dd4bf", "#60a5fa", "#facc15", "#4ade80", "#f87171", "#22d3ee", "#c084fc", "#a3e635", "#fb7185"];
const PALETTE_LIGHT = ["#6d4fe0", "#d6317f", "#e0620c", "#0f9f90", "#2b6fe0", "#b88a00", "#1f9d4e", "#d93a3a", "#0a8fb0", "#9333ea", "#6a9a0a", "#d63a55"];

export function groupColor(group: string, groups: string[], dark: boolean): string {
  const i = Math.max(0, groups.indexOf(group));
  return (dark ? PALETTE_DARK : PALETTE_LIGHT)[i % PALETTE_DARK.length]!;
}
