import { useEffect, useMemo, useState } from "react";
import { cn } from "../lib/utils";
import type { Relation } from "../data";
import { fileId, type Brain } from "./model";

const ROW = 46;
const GAP = 64; // width of the curve area between a column and the file
const OUT = "#ff7a1a";
const IN = "#4da3ff";

export interface Group { key: string; files: { path: string; label: string }[]; external: boolean; kinds: Set<string> }
/** The group of the diagram that is open: which side, its folder, and its files (paths). */
export interface OpenGroup { side: "in" | "out"; key: string; external: boolean; paths: string[] }

/** "a/b/c/d.py" -> "c/d" style folder label (the last two folders). */
export const folderOf = (path: string) => {
  const parts = path.split("/").slice(0, -1);
  return parts.length === 0 ? "(project root)" : parts.slice(-2).join("/");
};

const KIND_WORD: Record<Relation["kind"], string> = { imports: "imports", calls: "calls", references: "references" };

/** Everything on one side of the file, grouped by folder. A call into the same file is not a link. */
export function groupRelatives(rels: Relation[], self: string): Group[] {
  const by = new Map<string, Group>();
  for (const r of rels) {
    const path = r.other.path;
    if (path === self) continue;
    const external = !path;
    const key = external ? "external packages" : folderOf(path);
    const g = by.get(key) ?? { key, files: [], external, kinds: new Set<string>() };
    const id = path ?? r.other.label;
    if (!g.files.some((f) => f.path === id)) g.files.push({ path: id, label: r.other.label });
    g.kinds.add(KIND_WORD[r.kind]);
    by.set(key, g);
  }
  return [...by.values()].sort((a, b) => b.files.length - a.files.length || a.key.localeCompare(b.key));
}

function Column({ groups, color, side, active, onPick }: { groups: Group[]; color: string; side: "left" | "right"; active: string | null; onPick: (key: string) => void }) {
  return (
    <ul className="min-w-0" aria-label={side === "left" ? "Files that use it" : "Files it uses"}>
      {groups.map((g) => (
        <li key={g.key} style={{ height: ROW }} className="flex items-center">
          <button
            onClick={() => onPick(`${side}:${g.key}`)}
            aria-pressed={active === `${side}:${g.key}`}
            className={cn(
              "flex h-[38px] w-full min-w-0 items-center gap-2 rounded-md border bg-card px-2 text-left hover:bg-accent",
              active === `${side}:${g.key}` && "border-foreground bg-accent",
            )}
            style={{ borderLeft: side === "right" ? `3px solid ${color}` : undefined, borderRight: side === "left" ? `3px solid ${color}` : undefined }}
            title={g.key}
          >
            <span className="min-w-0 flex-1">
              <span className={cn("block truncate text-[12px] font-semibold", g.external && "italic")}>{g.key}</span>
              <span className="block truncate text-[10.5px] text-muted-foreground">{[...g.kinds].join(" · ")}: {g.files.slice(0, 2).map((f) => f.label).join(", ")}{g.files.length > 2 ? "…" : ""}</span>
            </span>
            <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[11px] font-semibold tabular-nums">{g.files.length}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

/** Who imports this file (left), the file, and what it imports (right), grouped by folder. */
export function FamilyDiagram({ path, relations, brain, onSelect, onGroup }: { path: string; relations: Relation[]; brain: Brain; onSelect: (id: string) => void; onGroup?: (g: OpenGroup | null) => void }) {
  const importers = useMemo(() => groupRelatives(relations.filter((r) => r.direction === "in"), path), [relations, path]);
  const imports = useMemo(() => groupRelatives(relations.filter((r) => r.direction === "out"), path), [relations, path]);
  const [active, setActive] = useState<string | null>(null);
  const rows = Math.max(1, importers.length, imports.length);
  const H = rows * ROW;
  const known = useMemo(() => new Set(brain.nodes.map((n) => n.id)), [brain]);
  // another file: nothing is open yet
  useEffect(() => { setActive(null); }, [path]);
  useEffect(() => {
    if (!onGroup) return;
    if (!active) { onGroup(null); return; }
    const side = active.slice(0, active.indexOf(":")), key = active.slice(active.indexOf(":") + 1);
    const g = (side === "left" ? importers : imports).find((x) => x.key === key);
    onGroup(g ? { side: side === "left" ? "in" : "out", key, external: g.external, paths: g.files.map((f) => f.path) } : null);
    return () => onGroup(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, importers, imports]);
  if (importers.length === 0 && imports.length === 0) return null;

  const [side, key] = active ? [active.slice(0, active.indexOf(":")), active.slice(active.indexOf(":") + 1)] : [null, null];
  const group = (side === "left" ? importers : imports).find((g) => g.key === key);
  // y of group i when n groups are centred in the column of height H
  const yOf = (i: number, n: number) => i * ROW + ROW / 2 + (H - n * ROW) / 2;
  const toFile = (i: number, n: number) => `M 0 ${yOf(i, n)} C ${GAP / 2} ${yOf(i, n)}, ${GAP / 2} ${H / 2}, ${GAP} ${H / 2}`;
  const fromFile = (i: number, n: number) => `M 0 ${H / 2} C ${GAP / 2} ${H / 2}, ${GAP / 2} ${yOf(i, n)}, ${GAP} ${yOf(i, n)}`;

  return (
    <section className="@container space-y-3" aria-label="Family of this file">
      {/* narrow panel: a vertical stack, imported-by -> this file -> imports */}
      <div className="space-y-1 @md:hidden">
        {importers.length > 0 && (
          <>
            <p className="label-caps text-[10px]" style={{ color: IN }}>Imported by</p>
            <Column groups={importers} color={IN} side="left" active={active} onPick={setActive} />
            <p className="text-center text-muted-foreground" aria-hidden>↓</p>
          </>
        )}
        <div className="rounded-md border-2 border-foreground bg-card px-2 py-2 text-center">
          <p className="label-caps text-[9.5px] text-muted-foreground">This file</p>
          <p className="mt-0.5 break-all text-[12.5px] leading-tight font-semibold">{path.split("/").at(-1)}</p>
        </div>
        {imports.length > 0 && (
          <>
            <p className="text-center text-muted-foreground" aria-hidden>↓</p>
            <p className="label-caps text-[10px]" style={{ color: OUT }}>Imports</p>
            <Column groups={imports} color={OUT} side="right" active={active} onPick={setActive} />
          </>
        )}
      </div>
      {/* wide panel: three columns joined by curves */}
      <div className="hidden items-center @md:grid" style={{ gridTemplateColumns: `minmax(0,1fr) ${GAP}px minmax(96px,150px) ${GAP}px minmax(0,1fr)` }}>
        <div className="flex items-center" style={{ height: H }}>
          <div className="w-full"><Column groups={importers} color={IN} side="left" active={active} onPick={setActive} /></div>
        </div>
        <svg width={GAP} height={H} aria-hidden>
          {importers.map((g, i) => <path key={g.key} d={toFile(i, importers.length)} fill="none" stroke={IN} strokeWidth={Math.min(3, 1 + g.files.length / 6)} opacity={0.8} />)}
        </svg>
        <div className="grid place-items-center">
          <div className="w-full rounded-md border-2 border-foreground bg-card px-2 py-2 text-center">
            <p className="label-caps text-[9.5px] text-muted-foreground">This file</p>
            <p className="mt-0.5 break-all text-[12.5px] leading-tight font-semibold">{path.split("/").at(-1)}</p>
          </div>
        </div>
        <svg width={GAP} height={H} aria-hidden>
          {imports.map((g, i) => <path key={g.key} d={fromFile(i, imports.length)} fill="none" stroke={OUT} strokeWidth={Math.min(3, 1 + g.files.length / 6)} opacity={0.8} />)}
        </svg>
        <div className="flex items-center" style={{ height: H }}>
          <div className="w-full"><Column groups={imports} color={OUT} side="right" active={active} onPick={setActive} /></div>
        </div>
      </div>
      <div className="flex justify-between text-[10.5px] tracking-wide text-muted-foreground uppercase">
        <span style={{ color: IN }}>← used by {importers.reduce((n, g) => n + g.files.length, 0)}</span>
        <span style={{ color: OUT }}>uses {imports.reduce((n, g) => n + g.files.length, 0)} →</span>
      </div>
      {group && (
        <div className="rounded-md border bg-muted/30 p-2">
          <p className="label-caps mb-1.5 px-1 text-[10px] text-muted-foreground">{group.key}</p>
          <ul className="max-h-48 space-y-0.5 overflow-auto">
            {group.files.map((f) => {
              const id = fileId(f.path);
              const can = known.has(id);
              return (
                <li key={f.path}>
                  <button disabled={!can} onClick={() => onSelect(id)} className={cn("w-full truncate rounded px-2 py-1 text-left font-mono text-[11.5px]", can ? "hover:bg-accent" : "opacity-60")} title={f.path}>
                    {f.path.split("/").at(-1)}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </section>
  );
}
