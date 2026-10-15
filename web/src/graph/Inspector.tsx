import {
  ArrowDownToLine, ArrowUpFromLine, BookOpen, Box, Braces, ChevronLeft, ChevronRight, Crosshair, FileCode2, FileText, Folder, Link2, Network, X,
} from "lucide-react";
import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { Badge, toneOf } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { CodeView } from "../components/CodeView";
import { Markdown } from "../components/Markdown";
import { EmptyState, ErrorNote, Skeleton, Stat } from "../components/ui/misc";
import { useFileDetail, useFileDiff, useSource, type Note, type Relation } from "../data";
import { cn } from "../lib/utils";
import { ChangesView } from "./ChangesView";
import { FamilyDiagram, type OpenGroup } from "./Family";
import { Summary } from "./Summary";
import { dirId, fileId, noteNodeId, type Brain, type BrainNode } from "./model";

interface Props {
  projectId: string;
  brain: Brain;
  node: BrainNode | null;
  notes: Note[];
  dark: boolean;
  onSelect: (nodeId: string) => void;
  /** open the map areas that hold these files (repo paths) */
  onReveal?: (paths: string[]) => void;
  /** trail of picked files: go back / forward, and the name of the file "back" returns to */
  canBack?: boolean;
  canForward?: boolean;
  backLabel?: string;
  onBack?: () => void;
  onForward?: () => void;
  /** open this file's bubble on the map and centre it (links in the panel do not move the map) */
  onLocate?: () => void;
  /** the group of the Links diagram that is open (or null), so the info card can describe it */
  onGroup?: (g: OpenGroup | null) => void;
  /** ask the panel to open a tab (bumping `n` asks again) */
  tabRequest?: { tab: string; n: number };
  onClose: () => void;
  className?: string;
  style?: CSSProperties;
}


export function NoteBody({ note }: { note: Note }) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone={toneOf(note.trust)} dot>{note.trust}</Badge>
        <Badge tone={toneOf(note.health)}>{note.health}</Badge>
        <Badge tone="outline">{note.kind}</Badge>
        <Badge tone="outline">confidence {note.confidence.toFixed(2)}</Badge>
      </div>
      {note.limitations && (
        <p className="rounded-md border border-warning/30 bg-warning-soft px-3 py-2 text-[13px] text-warning">
          <b>Limits:</b> {note.limitations}
        </p>
      )}
      <Markdown>{note.content}</Markdown>
      <p className="text-xs text-muted-foreground">written by {note.created_by}{note.metadata?.path ? ` · about ${note.metadata.path}` : ""}</p>
    </div>
  );
}

function NotesList({ notes, empty }: { notes: Note[]; empty: string }) {
  if (notes.length === 0) return <EmptyState icon={<BookOpen className="size-6 opacity-60" />}>{empty}</EmptyState>;
  return <div className="space-y-5">{notes.map((n) => (
    <section key={n.id} className="space-y-2">
      <h4 className="text-sm font-semibold">{n.title}</h4>
      <NoteBody note={n} />
    </section>
  ))}</div>;
}

const KIND_ICON: Record<string, typeof Braces> = { class: Box, function: Braces, method: Braces };

function RelationGroup({ title, icon, items, brain, onSelect }: { title: string; icon: React.ReactNode; items: Relation[]; brain: Brain; onSelect: (id: string) => void }) {
  if (items.length === 0) return null;
  const known = new Set(brain.nodes.map((n) => n.id));
  return (
    <section>
      <h4 className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold tracking-wide text-muted-foreground uppercase">{icon}{title} <span className="font-normal">({items.length})</span></h4>
      <ul className="space-y-0.5">
        {items.slice(0, 60).map((r, i) => {
          const target = r.other.path ? fileId(r.other.path) : null;
          const clickable = target !== null && known.has(target);
          return (
            <li key={`${r.other.key}-${i}`}>
              <button
                disabled={!clickable}
                onClick={() => target && onSelect(target)}
                className={cn("flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px]", clickable ? "hover:bg-accent" : "opacity-70")}
              >
                <span className="min-w-0 flex-1 truncate font-medium">{r.other.label}</span>
                <span className="max-w-[45%] shrink-0 truncate text-xs text-muted-foreground">
                  {r.other.path ?? (r.other.kind === "external" ? "external package" : "")}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {items.length > 60 && <p className="px-2 text-xs text-muted-foreground">+{items.length - 60} more</p>}
    </section>
  );
}

const TAB_KEY = "eios.inspector.tab";
const TABS = ["overview", "changes", "code", "symbols", "links", "notes"];
const loadTab = () => {
  try { const t = localStorage.getItem(TAB_KEY); return t && TABS.includes(t) ? t : "overview"; } catch { return "overview"; }
};

function FileInspector(props: Props & { node: BrainNode; tab: string; onTab: (t: string) => void }) {
  const { projectId, node, brain, notes, dark, onSelect, tab, onTab: setTab } = props;
  const path = node.path!;
  const detail = useFileDetail(projectId, path);
  const change = useFileDiff(projectId, path, true).data; // counts only: for the tab label
  const [mark, setMark] = useState<[number, number] | null>(null);
  const [preview, setPreview] = useState(true); // Markdown files open as a document, not as source
  const [wrap, setWrap] = useState(true);
  const isDoc = /\.(md|markdown|mdx)$/i.test(path);
  const source = useSource(projectId, path, tab === "code");
  // another file: the same tab stays open (Links stays Links); only per-file view state resets
  useEffect(() => { setMark(null); setPreview(true); }, [path]);

  const rel = detail.data?.relations ?? [];
  const by = (dir: "out" | "in", kind: Relation["kind"]) => rel.filter((r) => r.direction === dir && r.kind === kind);
  const mine = notes.filter((n) => n.metadata?.path === path);
  const imports = by("out", "imports");
  const importedBy = by("in", "imports");
  const relatives = [...new Set(rel.map((r) => r.other.path).filter((x): x is string => !!x && x !== path))];

  return (
    <Tabs value={tab} onValueChange={setTab}>
      <TabsList className="w-full justify-start overflow-x-auto">
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="changes">
          Changes
          {change && change.status !== "unchanged" && change.status !== "missing" && (
            <span className="ml-1 font-mono text-[11px]">
              {change.additions != null ? <><b className="text-success">+{change.additions}</b> <b className="text-danger">−{change.deletions}</b></> : change.status}
            </span>
          )}
        </TabsTrigger>
        <TabsTrigger value="code"><FileCode2 className="size-3.5" />Code</TabsTrigger>
        <TabsTrigger value="symbols">Symbols{detail.data ? ` ${detail.data.symbols.length}` : ""}</TabsTrigger>
        <TabsTrigger value="links">Links{rel.length ? ` ${rel.length}` : ""}</TabsTrigger>
        <TabsTrigger value="notes">Notes{mine.length ? ` ${mine.length}` : ""}</TabsTrigger>
      </TabsList>

      <TabsContent value="overview" className="space-y-4">
        <Summary projectId={projectId} path={path} />
        <ErrorNote error={detail.error} />
        {detail.isLoading ? <Skeleton className="h-24 w-full" /> : detail.data && (
          <>
            <div className="grid grid-cols-2 gap-2">
              <Stat label="Lines" value={detail.data.lines.toLocaleString()} />
              <Stat label="Symbols" value={detail.data.symbols.length} hint="functions, classes…" />
              <Stat label="Imports" value={imports.length} hint="files it depends on" />
              <Stat label="Used by" value={importedBy.length} hint="files that import it" />
            </div>
            <p className="text-[13px] text-muted-foreground">
              {detail.data.scope === "overlay" ? "Showing the uncommitted version. " : ""}
              {mine.length > 0 ? `${mine.length} note${mine.length > 1 ? "s" : ""} explain this file; open the Notes tab.` : "No note explains this file yet."}
            </p>
            {mine[0] && <div className="rounded-lg border bg-muted/40 p-3"><p className="mb-1 text-sm font-semibold">{mine[0].title}</p><p className="line-clamp-5 text-[13px] text-foreground/80">{mine[0].content}</p></div>}
          </>
        )}
      </TabsContent>

      <TabsContent value="changes">
        <ChangesView projectId={projectId} path={path} />
      </TabsContent>

      <TabsContent value="code">
        {source.isLoading && <Skeleton className="h-64 w-full" />}
        <ErrorNote error={source.error} />
        {source.data && (
          <>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <p className="text-xs text-muted-foreground">lines {source.data.start_line}–{source.data.end_line} of {source.data.total_lines}{source.data.scope === "overlay" ? " · uncommitted version" : ""}</p>
              <div className="flex gap-1">
                {isDoc && <Button size="sm" variant={preview ? "default" : "outline"} className="h-7" onClick={() => setPreview(true)}>Preview</Button>}
                {isDoc && <Button size="sm" variant={preview ? "outline" : "default"} className="h-7" onClick={() => setPreview(false)}>Source</Button>}
                {(!isDoc || !preview) && <Button size="sm" variant={wrap ? "default" : "outline"} className="h-7" onClick={() => setWrap((w) => !w)} aria-pressed={wrap}>Wrap</Button>}
              </div>
            </div>
            {isDoc && preview
              ? <div className="rounded-md border bg-card px-4 py-2"><Markdown>{source.data.text}</Markdown></div>
              : <CodeView text={source.data.text} startLine={source.data.start_line} language={node.language} mark={mark} dark={dark} wrap={wrap} />}
          </>
        )}
      </TabsContent>

      <TabsContent value="symbols">
        {detail.data?.symbols.length === 0 && <EmptyState>No functions or classes were found in this file.</EmptyState>}
        <ul className="space-y-0.5">
          {detail.data?.symbols.map((s) => {
            const Icon = KIND_ICON[s.kind] ?? Braces;
            return (
              <li key={s.qualified_name}>
                <button
                  onClick={() => { setMark([s.start_line, s.end_line]); setTab("code"); }}
                  className="flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left hover:bg-accent"
                >
                  <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] font-medium">{s.qualified_name}</span>
                    {s.signature && <span className="block truncate font-mono text-xs text-muted-foreground">{s.signature}</span>}
                  </span>
                  <span className="shrink-0 text-xs text-muted-foreground">L{s.start_line}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </TabsContent>

      <TabsContent value="links" className="space-y-4">
        {props.onReveal && relatives.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 rounded-md border bg-muted/40 p-2.5">
            <p className="min-w-0 flex-1 text-[12.5px] text-muted-foreground">
              <b className="text-foreground">{relatives.length}</b> related files. On the map, the areas that hold them show an orange → count (files this one imports) and a blue ← count (files that import it).
            </p>
            <Button size="sm" variant="outline" onClick={() => props.onReveal?.(relatives)}>Show them on the map</Button>
          </div>
        )}
        {rel.length === 0 && !detail.isLoading && <EmptyState icon={<Network className="size-6 opacity-60" />}>No imports, calls or references were found for this file.</EmptyState>}
        <FamilyDiagram path={path} relations={rel} brain={brain} onSelect={onSelect} onGroup={props.onGroup} />
        <RelationGroup title="Imports" icon={<ArrowUpFromLine className="size-3.5" />} items={imports} brain={brain} onSelect={onSelect} />
        <RelationGroup title="Imported by" icon={<ArrowDownToLine className="size-3.5" />} items={importedBy} brain={brain} onSelect={onSelect} />
        <RelationGroup title="Calls" icon={<Link2 className="size-3.5" />} items={by("out", "calls")} brain={brain} onSelect={onSelect} />
        <RelationGroup title="Called by" icon={<Link2 className="size-3.5" />} items={by("in", "calls")} brain={brain} onSelect={onSelect} />
        <RelationGroup title="References" icon={<Link2 className="size-3.5" />} items={by("out", "references")} brain={brain} onSelect={onSelect} />
        <RelationGroup title="Referenced by" icon={<Link2 className="size-3.5" />} items={by("in", "references")} brain={brain} onSelect={onSelect} />
      </TabsContent>

      <TabsContent value="notes">
        <NotesList notes={mine} empty="No note is attached to this file yet. Notes are written through the API or the MCP server." />
      </TabsContent>
    </Tabs>
  );
}

function DirInspector({ node, brain, notes, onSelect }: Props & { node: BrainNode }) {
  const path = node.path!;
  const children = useMemo(() => {
    const ids = new Set(brain.edges.filter((e) => e.kind === "contains" && e.source === dirId(path)).map((e) => e.target));
    return brain.nodes.filter((n) => ids.has(n.id)).sort((a, b) => (a.kind === b.kind ? b.children - a.children || a.label.localeCompare(b.label) : a.kind === "dir" ? -1 : 1));
  }, [brain, path]);
  const mine = notes.filter((n) => n.metadata?.path === path);
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2">
        <Stat label="Files inside" value={node.children.toLocaleString()} />
        <Stat label="Direct items" value={children.length} />
      </div>
      {mine.length > 0 && <NotesList notes={mine} empty="" />}
      <section>
        <h4 className="mb-1.5 text-xs font-semibold tracking-wide text-muted-foreground uppercase">Inside this folder</h4>
        <ul className="space-y-0.5">
          {children.slice(0, 80).map((c) => (
            <li key={c.id}>
              <button onClick={() => onSelect(c.id)} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] hover:bg-accent">
                {c.kind === "dir" ? <Folder className="size-4 shrink-0 text-warning" /> : <FileText className="size-4 shrink-0 text-muted-foreground" />}
                <span className="min-w-0 flex-1 truncate">{c.label}</span>
                <span className="shrink-0 text-xs text-muted-foreground">{c.kind === "dir" ? `${c.children} files` : `${c.lines} lines`}</span>
              </button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

export function Inspector(props: Props) {
  const { node, notes, brain, onClose, className, style } = props;
  const [tab, setTabState] = useState(loadTab);
  const setTab = (t: string) => {
    setTabState(t);
    try { localStorage.setItem(TAB_KEY, t); } catch { /* private window: fine, just not remembered */ }
  };
  useEffect(() => { if (props.tabRequest && props.tabRequest.n > 0) setTab(props.tabRequest.tab); }, [props.tabRequest?.n]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!node) return null;
  const note = node.kind === "note" ? notes.find((n) => noteNodeId(n.id) === node.id) : null;
  const projectNotes = notes.filter((n) => !n.metadata?.path);
  const subtitle =
    node.kind === "file" ? node.path : node.kind === "dir" ? `${node.path}/` : node.kind === "note" ? "Knowledge note" : "Project";
  const Icon = node.kind === "file" ? FileText : node.kind === "dir" ? Folder : node.kind === "note" ? BookOpen : Network;
  return (
    <aside className={cn("flex min-h-0 flex-col overflow-hidden rounded-md border bg-card shadow-lg", className)} style={style} aria-label="Details">
      <header className="flex items-start gap-3 border-b px-4 py-3">
        <span className="mt-0.5 rounded-md bg-muted p-1.5"><Icon className="size-4" /></span>
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-semibold">{node.kind === "note" ? node.label : node.kind === "project" ? node.label : node.label}</h3>
          <p className="truncate text-xs text-muted-foreground" title={subtitle ?? ""}>{subtitle}</p>
          {node.kind === "file" && <div className="mt-1.5 flex flex-wrap gap-1.5"><Badge tone="outline">{node.language}</Badge><Badge tone="outline">{node.lines} lines</Badge></div>}
        </div>
        <div className="-mr-1.5 flex shrink-0 items-center">
          <Button variant="ghost" size="icon" className="size-8" disabled={!props.canBack} onClick={props.onBack} aria-label={props.backLabel ? `Back to ${props.backLabel}` : "Back"} title={props.backLabel ? `Back to ${props.backLabel}` : "Back"}><ChevronLeft /></Button>
          <Button variant="ghost" size="icon" className="size-8" disabled={!props.canForward} onClick={props.onForward} aria-label="Forward" title="Forward"><ChevronRight /></Button>
          {props.onLocate && node.kind === "file" && <Button variant="ghost" size="icon" className="size-8" onClick={props.onLocate} aria-label="Show on the map" title="Show on the map"><Crosshair /></Button>}
          <Button variant="ghost" size="icon" className="size-8" onClick={onClose} aria-label="Close details"><X /></Button>
        </div>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {node.kind === "file" && <FileInspector {...props} node={node} tab={tab} onTab={setTab} />}
        {node.kind === "dir" && <DirInspector {...props} node={node} />}
        {node.kind === "note" && (note ? <NoteBody note={note} /> : <EmptyState>This note is no longer available.</EmptyState>)}
        {node.kind === "project" && (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-2">
              <Stat label="Files" value={node.children.toLocaleString()} />
              <Stat label="Notes" value={notes.length} />
            </div>
            <h4 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">How this project works</h4>
            <NotesList notes={projectNotes} empty="No overview note yet." />
            <p className="text-xs text-muted-foreground">{brain.groups.length - 1} top-level areas. Click a coloured cluster to explore it.</p>
          </div>
        )}
      </div>
    </aside>
  );
}
