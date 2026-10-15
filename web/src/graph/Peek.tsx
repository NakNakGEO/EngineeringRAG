import { ArrowLeft, Crosshair, FileCode2, X } from "lucide-react";
import type { CSSProperties } from "react";
import { Button } from "../components/ui/button";
import { Skeleton } from "../components/ui/misc";
import { useFileDetail, type Note } from "../data";
import { describeConnection } from "./connection";
import { groupRelatives, type OpenGroup } from "./Family";
import { fileId } from "./model";
import { Summary } from "./Summary";
import type { Brain, BrainNode } from "./model";

const base = (p: string) => p.split("/").at(-1) ?? p;
const dirOf = (p: string) => p.split("/").slice(0, -1).join("/");

/** A small card for the file you just opened from a link: how it connects to the file you came from,
 * what it is, and where it lives. The map stays where it is; "Show on map" goes there on request. */
export function Peek({
  projectId, node, from, group, brain, notes, style, onOpen, onBack, onLocate, onCode, onClose,
}: {
  projectId: string; node: BrainNode; from: BrainNode | null; group: OpenGroup | null; brain: Brain; notes: Note[];
  style?: CSSProperties; onOpen: (id: string) => void; onBack: (() => void) | null; onLocate: () => void; onCode: () => void; onClose: () => void;
}) {
  const path = node.path!;
  const detail = useFileDetail(projectId, path);
  const rels = detail.data?.relations ?? [];
  const links = from?.path ? describeConnection(rels, path, from.path) : [];
  const sides = {
    users: groupRelatives(rels.filter((r) => r.direction === "in"), path),
    uses: groupRelatives(rels.filter((r) => r.direction === "out"), path),
  };
  const known = new Set(brain.nodes.map((n) => n.id));
  const folder = dirOf(path);
  const inFolder = brain.nodes.filter((n) => n.kind === "file" && n.path && dirOf(n.path) === folder).length;
  const imports = rels.filter((r) => r.direction === "out" && r.kind === "imports").length;
  const usedBy = rels.filter((r) => r.direction === "in" && r.kind === "imports").length;
  const symbols = [...(detail.data?.symbols ?? [])].sort((a, b) => b.end_line - b.start_line - (a.end_line - a.start_line)).slice(0, 4);
  const note = notes.find((n) => n.metadata?.path === path);

  return (
    <section
      aria-label={`About ${base(path)}`}
      className="absolute z-30 flex max-h-[calc(100%-7rem)] w-[340px] flex-col overflow-hidden rounded-md border bg-card shadow-xl"
      style={style}
    >
      <header className="flex items-start gap-2 border-b px-3 py-2.5">
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] font-semibold">{base(path)}</p>
          <p className="truncate text-[11px] text-muted-foreground">
            {from ? <>opened from <b className="font-medium text-foreground">{base(from.path ?? from.label)}</b></> : "picked on the map"}
          </p>
        </div>
        <Button variant="ghost" size="icon" className="-mr-1 size-7" onClick={onClose} aria-label="Close this card"><X className="size-4" /></Button>
      </header>

      <div className="min-h-0 space-y-3 overflow-y-auto px-3 py-2.5 text-[12.5px]">
        <Summary projectId={projectId} path={path} className="px-2.5 py-2" />
        {group && (
          <section className="rounded-md border border-primary/40 bg-primary/5 p-2">
            <h4 className="mb-1 text-[12px] font-semibold">
              {group.external ? "External packages" : group.key}
              <span className="ml-1.5 font-normal text-muted-foreground">
                {group.paths.length} {group.side === "in" ? `${group.paths.length === 1 ? "file uses" : "files use"} ${base(path)}` : `${group.paths.length === 1 ? "file" : "files"} used by ${base(path)}`}
              </span>
            </h4>
            <ul className="max-h-60 space-y-1 overflow-y-auto">
              {group.paths.slice(0, 25).map((p) => {
                const why = group.external ? [] : describeConnection(rels, path, p);
                const target = group.external ? null : fileId(p);
                const clickable = target !== null && known.has(target);
                return (
                  <li key={p}>
                    <button
                      disabled={!clickable}
                      onClick={() => target && onOpen(target)}
                      className={"w-full rounded px-1.5 py-1 text-left " + (clickable ? "hover:bg-accent" : "cursor-default")}
                    >
                      <span className="block truncate font-mono text-[11.5px] font-medium">{base(p)}</span>
                      {why[0] && <span className="block text-[11.5px] text-muted-foreground">{why[0].text}{why[0].line != null ? ` · line ${why[0].line}` : ""}{why.length > 1 ? ` (+${why.length - 1})` : ""}</span>}
                    </button>
                  </li>
                );
              })}
              {group.paths.length > 25 && <li className="px-1.5 text-muted-foreground">+{group.paths.length - 25} more</li>}
            </ul>
          </section>
        )}

        {from ? (
          <section>
            <h4 className="label-caps mb-1 text-[10px] text-muted-foreground">How it connects to {base(from.path ?? from.label)}</h4>
            {detail.isLoading ? <Skeleton className="h-10 w-full" /> : links.length === 0 ? (
              <p className="text-muted-foreground">No direct import or call between the two was found.</p>
            ) : (
              <ul className="space-y-1">
                {links.slice(0, 6).map((l, i) => (
                  <li key={i} className="flex items-baseline gap-2">
                    <span className="shrink-0 rounded bg-muted px-1.5 py-px text-[10px] font-semibold uppercase">{l.kind === "imports" ? "import" : l.kind === "calls" ? "call" : "ref"}</span>
                    <span className="min-w-0 flex-1">{l.text}</span>
                    {l.line != null && <span className="shrink-0 text-[11px] text-muted-foreground">line {l.line}</span>}
                  </li>
                ))}
                {links.length > 6 && <li className="text-muted-foreground">+{links.length - 6} more</li>}
              </ul>
            )}
          </section>
        ) : (
          <section>
            <h4 className="label-caps mb-1 text-[10px] text-muted-foreground">Connected to</h4>
            {detail.isLoading ? <Skeleton className="h-10 w-full" /> : sides.users.length + sides.uses.length === 0 ? (
              <p className="text-muted-foreground">Nothing in the project imports or calls this file, and it imports nothing from it.</p>
            ) : (
              <dl className="space-y-1">
                {([["Used by", sides.users], ["Uses", sides.uses]] as const).filter(([, g]) => g.length > 0).map(([label, g]) => (
                  <div key={label} className="grid grid-cols-[58px_1fr] gap-2">
                    <dt className="text-muted-foreground">{label}</dt>
                    <dd className="min-w-0 leading-snug">
                      {g.slice(0, 3).map((x) => `${x.external ? "external packages" : x.key} (${x.files.length})`).join(", ")}
                      {g.length > 3 ? `, +${g.length - 3} more` : ""}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
            <p className="mt-1 text-[11px] text-muted-foreground">Open the Links tab and click a group to see why.</p>
          </section>
        )}

        <section>
          <h4 className="label-caps mb-1 text-[10px] text-muted-foreground">What it is</h4>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
            <dt className="text-muted-foreground">Size</dt><dd>{node.lines.toLocaleString()} lines · {node.language}</dd>
            <dt className="text-muted-foreground">Imports</dt><dd>{imports} {imports === 1 ? "file" : "files"}</dd>
            <dt className="text-muted-foreground">Used by</dt><dd>{usedBy} {usedBy === 1 ? "file" : "files"}</dd>
            <dt className="text-muted-foreground">Folder</dt><dd className="min-w-0 truncate" title={folder}>{folder || "(project root)"} · {inFolder} {inFolder === 1 ? "file" : "files"}</dd>
            <dt className="text-muted-foreground">Area</dt><dd className="truncate">{node.group}</dd>
          </dl>
        </section>

        {symbols.length > 0 && (
          <section>
            <h4 className="label-caps mb-1 text-[10px] text-muted-foreground">Biggest functions and classes</h4>
            <ul className="space-y-0.5">
              {symbols.map((s) => (
                <li key={s.qualified_name} className="flex items-baseline gap-2">
                  <span className="min-w-0 flex-1 truncate font-mono text-[11.5px]">{s.qualified_name}</span>
                  <span className="shrink-0 text-[11px] text-muted-foreground">{s.end_line - s.start_line + 1} lines</span>
                </li>
              ))}
            </ul>
          </section>
        )}

        {note && (
          <section>
            <h4 className="label-caps mb-1 text-[10px] text-muted-foreground">What the notes say</h4>
            <p className="font-medium">{note.title}</p>
            <p className="line-clamp-3 text-muted-foreground">{note.content}</p>
          </section>
        )}
      </div>

      <footer className="flex flex-wrap gap-1.5 border-t px-3 py-2">
        {onBack && <Button size="sm" variant="outline" className="h-7" onClick={onBack}><ArrowLeft className="size-3.5" />Back</Button>}
        <Button size="sm" variant="outline" className="h-7" onClick={onLocate}><Crosshair className="size-3.5" />Show on map</Button>
        <Button size="sm" variant="outline" className="h-7" onClick={onCode}><FileCode2 className="size-3.5" />Code</Button>
      </footer>
    </section>
  );
}
