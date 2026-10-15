/** Plain sentences for how one file connects to another, from the first file's relations.
 *  `rels` are the relations of `self`; those whose other end is `other` are the connection. */
import type { Relation } from "../data";

const base = (path: string) => path.split("/").at(-1) ?? path;

/** A relation's own end is stored as a key ("symbol:path::Class.method" / "file:path"); show the name. */
export function friendly(key: string): string {
  if (!key) return "";
  const i = key.lastIndexOf("::");
  if (key.startsWith("symbol:") && i >= 0) return key.slice(i + 2);
  if (key.startsWith("file:")) return base(key.slice(5));
  return key.startsWith("symbol:") ? key.slice(7) : key;
}

export interface Link { text: string; line: number | null; kind: Relation["kind"] }

export function describeConnection(rels: Relation[], self: string, other: string): Link[] {
  const a = base(self), b = base(other);
  const out: Link[] = [];
  const seen = new Set<string>();
  for (const r of rels) {
    if (r.other.path !== other) continue;
    const names = [...new Set([...(r.names ?? []), ...(r.other.kind === "external" ? [] : [r.other.label])].filter(Boolean))].slice(0, 4);
    let text: string;
    if (r.kind === "imports") text = r.direction === "out" ? `${a} imports ${b}` : `${b} imports ${a}`;
    else if (r.kind === "calls") {
      const who = names.length ? names.join(", ") : "a function";
      text = r.direction === "out" ? `${a} calls ${who} in ${b}` : `${b} calls ${friendly(r.own) || who} in ${a}`;
    } else {
      const who = names.length ? names.join(", ") : "a name";
      text = r.direction === "out" ? `${a} refers to ${who} in ${b}` : `${b} refers to ${friendly(r.own) || who} in ${a}`;
    }
    const key = `${r.kind}|${r.direction}|${text}|${r.line ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ text, line: r.line, kind: r.kind });
  }
  // imports first, then calls, then references; stable inside each kind
  const rank: Record<Relation["kind"], number> = { imports: 0, calls: 1, references: 2 };
  return out.sort((x, y) => rank[x.kind] - rank[y.kind]);
}
