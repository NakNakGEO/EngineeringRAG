import { ArrowLeft, Search, SlidersHorizontal, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Input, Select } from "../components/ui/input";
import { EmptyState, ErrorNote, Skeleton } from "../components/ui/misc";
import { useBrain, useNotes, useSelectedProject } from "../data";
import { Inspector } from "../graph/Inspector";
import { departmentsOf, deptId, NOTES_GROUP, type LayoutMode } from "../graph/layout";
import { MapFlow, type MapView } from "../graph/MapFlow";
import { dirId, groupColor, type BrainNode } from "../graph/model";
import { canGoBack, canGoForward, currentEntry, EMPTY_NAV, navBack, navForward, navGo, previousEntry } from "../graph/nav";
import type { OpenGroup } from "../graph/Family";
import { Peek } from "../graph/Peek";
import { useLive } from "../live";
import { cn } from "../lib/utils";
import { loadNumber, saveNumber, useMedia } from "../lib/useMedia";
import { useTheme } from "../theme";

const KIND_LABEL: Record<BrainNode["kind"], string> = { project: "Project", dir: "Folder", file: "File", note: "Note" };
const LAYOUTS: [LayoutMode, string][] = [["force", "Force"], ["circle", "Circle"], ["hex", "Hex"], ["rings", "Rings"]];
const VIEWS: [MapView, string][] = [["departments", "Departments"], ["folders", "Folders"]];

const ancestors = (path: string): string[] => {
  const parts = path.split("/");
  return parts.slice(0, -1).map((_, i) => dirId(parts.slice(0, i + 1).join("/")));
};

function Segmented<T extends string>({ value, options, onChange, label }: { value: T; options: [T, string][]; onChange: (v: T) => void; label: string }) {
  return (
    <div>
      <p className="label-caps mb-1.5 text-[10px] text-muted-foreground">{label}</p>
      <div className="grid gap-1" style={{ gridTemplateColumns: `repeat(${options.length}, minmax(0, 1fr))` }}>
        {options.map(([v, text]) => (
          <button
            key={v}
            onClick={() => onChange(v)}
            aria-pressed={value === v}
            className={cn("h-7 rounded border px-1.5 text-[11.5px] font-semibold", value === v ? "border-foreground bg-foreground text-background" : "bg-card text-muted-foreground hover:bg-accent")}
          >{text}</button>
        ))}
      </div>
    </div>
  );
}

const INSPECTOR_KEY = "eios-inspector-width";
const MIN_W = 320;

export function Brain({ initialProject = "", initialFile = "", onBack }: { initialProject?: string; initialFile?: string; onBack: () => void }) {
  const [projectId, setProject, list] = useSelectedProject(initialProject);
  const { brain, loading, error, overview } = useBrain(projectId);
  const notes = useNotes(projectId);
  const live = useLive();
  const dark = useTheme() === "dark";

  const [view, setView] = useState<MapView>("departments");
  const [mode, setMode] = useState<LayoutMode>("rings");
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set([deptId(NOTES_GROUP)]));
  const [openDirs, setOpenDirs] = useState<ReadonlySet<string>>(new Set());
  const [spacing, setSpacing] = useState(1);
  const [showNames, setShowNames] = useState(false);
  const [showImports, setShowImports] = useState(false); // lines between areas are opt-in
  // "expand all" lays every ring out around what is open; single clicks open an area in place
  const [spread, setSpread] = useState(false);
  const [reframe, setReframe] = useState(0);
  // what is open in the details panel, plus the trail that led to it (back / forward)
  const [nav, setNav] = useState(EMPTY_NAV);
  const selected = currentEntry(nav)?.id ?? null;
  const clearSelection = () => setNav(EMPTY_NAV);
  const [cardOff, setCardOff] = useState<string | null>(null); // the info card the user closed
  const [group, setGroup] = useState<OpenGroup | null>(null); // the group open in the Links diagram
  const [tabRequest, setTabRequest] = useState({ tab: "overview", n: 0 }); // e.g. the card's Code button
  const [focus, setFocus] = useState<{ id: string; n: number } | null>(null);
  const [query, setQuery] = useState("");
  const [legend, setLegend] = useState(false);
  const [panel, setPanel] = useState(false); // settings panel on small screens
  const desktop = useMedia("(min-width: 768px)");
  const stage = useRef<HTMLDivElement>(null);
  const [inspW, setInspW] = useState(() => loadNumber(INSPECTOR_KEY, 560));
  const [dragging, setDragging] = useState(false);
  const maxW = () => Math.max(MIN_W, Math.min(1200, (stage.current?.clientWidth ?? 1200) - 330));
  const setWidth = (w: number) => setInspW(Math.max(MIN_W, Math.min(maxW(), w)));

  useEffect(() => {
    setNav(EMPTY_NAV);
    setExpanded(new Set([deptId(NOTES_GROUP)]));
    setOpenDirs(new Set());
    setSpread(false);
  }, [projectId]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setNav(EMPTY_NAV); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const [openedFile, setOpenedFile] = useState("");
  const deptOf = useMemo(() => (brain ? departmentsOf(brain) : null), [brain]);
  const q = query.trim().toLowerCase();
  const matches = useMemo(() => {
    if (!brain || q.length < 2) return [];
    return brain.nodes.filter((n) => n.kind !== "project" && ((n.path ?? "").toLowerCase().includes(q) || n.label.toLowerCase().includes(q)));
  }, [brain, q]);
  const highlight = useMemo(() => (matches.length ? new Set(matches.slice(0, 600).map((n) => n.id)) : null), [matches]);
  const changed = useMemo(() => new Set(live.pulses(projectId).keys()), [live, projectId]);
  const node = brain?.nodes.find((n) => n.id === selected) ?? null;

  const exists = (id: string) => !!brain?.nodes.some((n) => n.id === id);
  const previous = previousEntry(nav);
  const previousNode = previous ? brain?.nodes.find((n) => n.id === previous.id) ?? null : null;
  const entry = currentEntry(nav);
  const cardShown = desktop && node?.kind === "file" && cardOff !== `${nav.at}:${node.id}`;
  const cameFrom = entry?.via === "link" && previousNode?.kind === "file" ? previousNode : null;
  // while a file is open the settings give way (unless the user asked for them); a search keeps them,
  // except under the info card, which opens on the same side
  const settingsHidden = !!node && !panel && (!query || cardShown);
  /** Open a file from a link in the panel: it loads there, the map stays where it is. */
  const openFromLink = (id: string) => setNav((n) => navGo(n, id, "link"));

  const toggle = (set: ReadonlySet<string>, id: string) => { const next = new Set(set); if (!next.delete(id)) next.add(id); return next; };

  /** Make a node visible in the current view, select it and centre on it. */
  const pick = (id: string) => {
    if (!brain) return;
    const n = brain.nodes.find((b) => b.id === id);
    if (!n) return;
    let useView = view;
    if (n.kind === "dir" && view === "departments") { setView("folders"); useView = "folders"; }
    if (useView === "departments") {
      const d = deptOf?.of.get(id);
      if (d) setExpanded((e) => new Set(e).add(d));
    } else if (n.path) {
      setOpenDirs((o) => new Set([...o, ...ancestors(n.kind === "dir" ? `${n.path}/x` : n.path!)]));
    }
    setNav((n) => navGo(n, id, "search"));
    setFocus((f) => ({ id, n: (f?.n ?? 0) + 1 }));
  };

  useEffect(() => {
    if (!initialFile || !brain || openedFile === initialFile) return;
    const id = `file:${initialFile}`;
    if (brain.nodes.some((n) => n.id === id)) {
      setOpenedFile(initialFile);
      pick(id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [brain, initialFile]);

  /** Open the areas (or folders) that hold these files, so their lines can be seen. */
  const reveal = (paths: string[]) => {
    if (!brain || !deptOf) return;
    if (view === "departments") {
      const depts = new Set<string>();
      for (const path of paths) {
        const d = deptOf.of.get(`file:${path}`);
        if (d) depts.add(d);
      }
      setExpanded((e) => new Set([...e, ...[...depts].slice(0, 8)]));
    } else {
      setOpenDirs((o) => new Set([...o, ...paths.flatMap((x) => ancestors(x))]));
    }
  };

  const expandAll = () => {
    setSpread(true);
    setReframe((n) => n + 1);
    if (view === "departments") setExpanded(new Set(deptOf?.depts.map((d) => d.id) ?? []));
    else setOpenDirs(new Set(brain?.nodes.filter((n) => n.kind === "dir").slice(0, 400).map((n) => n.id) ?? []));
  };
  const collapseAll = () => { setSpread(false); setReframe((n) => n + 1); setExpanded(new Set()); setOpenDirs(new Set()); };

  const files = brain ? brain.nodes.filter((n) => n.kind === "file").length : 0;
  const project = list.find((p) => p.id === projectId);

  return (
    <div ref={stage} className="relative h-[calc(100dvh-3.5rem)] min-h-[520px] overflow-hidden bg-background">
      {brain && (
        <MapFlow
          brain={brain} view={view} mode={mode} expanded={expanded} openDirs={openDirs} spacing={spacing}
          showNames={showNames} showImports={showImports} spread={spread && expanded.size > 2} selected={selected} highlight={highlight} changed={changed} dark={dark}
          onSelect={(id) => setNav((n) => (id ? navGo(n, id, "map") : EMPTY_NAV))} quiet={entry?.via === "link"} onToggleDept={(id) => setExpanded((e) => toggle(e, id))} onToggleDir={(id) => setOpenDirs((o) => toggle(o, id))}
          fitKey={`${projectId}|${view}|${mode}|${reframe}`} focus={focus}
          inset={{ left: desktop && node ? Math.min(inspW, maxW()) + 24 : 0, right: desktop && !settingsHidden ? 314 : 0, top: 60 }}
        />
      )}
      {loading && <div className="absolute inset-0 grid place-items-center"><div className="w-64 space-y-3"><Skeleton className="h-3 w-full" /><Skeleton className="h-3 w-3/4" /><p className="text-center text-xs text-muted-foreground">Reading the project…</p></div></div>}
      {!loading && !brain && <div className="absolute inset-0 grid place-items-center"><EmptyState>This project has no indexed files yet. Index it, then its map appears here.</EmptyState></div>}

      {/* title + back, like the OS "second brain" screen */}
      <div className="pointer-events-none absolute top-3 left-3 z-10 flex items-center gap-3">
        <Button variant="outline" size="sm" className="pointer-events-auto border-2 border-foreground bg-background" onClick={onBack}><ArrowLeft />Back to the OS</Button>
        <div className="hidden leading-tight sm:block">
          <p className="label-caps text-[13px]">Project <span className="text-muted-foreground">Map</span></p>
          <p className="truncate text-[11px] text-muted-foreground">{project?.name} · {files.toLocaleString()} files</p>
        </div>
      </div>

      {/* settings panel */}
      {/* on a wide screen the settings step aside while a file is open, so the map gets the room */}
      <Button variant="outline" size="sm" className={cn("absolute top-3 right-3 z-20 bg-card", !node && "md:hidden")} onClick={() => setPanel((v) => !v)} aria-expanded={panel}>
        <SlidersHorizontal />Settings
      </Button>
      <aside className={cn("absolute top-3 right-3 z-10 w-[min(290px,calc(100%-1.5rem))] space-y-3 rounded-md border bg-card/95 p-3 backdrop-blur max-md:top-14", node && "md:top-14", !panel && "max-md:hidden", settingsHidden && "md:hidden")} aria-label="Map settings">
        <Select aria-label="Project" value={projectId} onChange={(e) => setProject(e.target.value)}>
          {list.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </Select>
        <div className="relative">
          <Search className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-muted-foreground" />
          <Input className="pl-8" placeholder={`Search ${files.toLocaleString()} files… `} value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search the map" />
          {query && <button className="absolute top-1.5 right-1.5 rounded p-1 text-muted-foreground hover:bg-accent" onClick={() => setQuery("")} aria-label="Clear search"><X className="size-4" /></button>}
        </div>
        {matches.length > 0 && (
          <ul className="max-h-48 space-y-0.5 overflow-auto" aria-label="Search results">
            {matches.slice(0, 8).map((m) => (
              <li key={m.id}>
                <button onClick={() => pick(m.id)} className="flex w-full items-center gap-2 rounded px-1.5 py-1 text-left text-[12.5px] hover:bg-accent">
                  <Badge tone="outline" className="shrink-0">{KIND_LABEL[m.kind]}</Badge>
                  <span className="min-w-0 flex-1 truncate">{m.path ?? m.label}</span>
                </button>
              </li>
            ))}
            {matches.length > 8 && <li className="px-1.5 text-xs text-muted-foreground">{matches.length - 8} more are dimmed-out on the map</li>}
          </ul>
        )}
        {q.length >= 2 && matches.length === 0 && <p className="text-xs text-muted-foreground">Nothing matches “{query}”.</p>}
        <Segmented label="Layout" value={mode} options={LAYOUTS} onChange={setMode} />
        <Segmented label="View" value={view} options={VIEWS} onChange={setView} />
        <label className="block">
          <span className="label-caps mb-1 flex justify-between text-[10px] text-muted-foreground"><span>Spacing</span><span>{spacing.toFixed(2)}</span></span>
          <input type="range" min={0.6} max={1.8} step={0.05} value={spacing} onChange={(e) => setSpacing(Number(e.target.value))} className="w-full accent-[var(--primary)]" />
        </label>
        <label className="flex items-center gap-2 text-[12.5px]"><input type="checkbox" checked={showNames} onChange={(e) => setShowNames(e.target.checked)} className="accent-[var(--primary)]" />File names</label>
        <label className="flex items-center gap-2 text-[12.5px]"><input type="checkbox" checked={showImports} onChange={(e) => setShowImports(e.target.checked)} className="accent-[var(--primary)]" />Links between areas</label>
        <div className="grid grid-cols-2 gap-2">
          <Button variant="outline" size="sm" onClick={expandAll}>Expand all</Button>
          <Button variant="outline" size="sm" onClick={collapseAll}>Collapse all</Button>
        </div>
        <ErrorNote error={error} />
        {overview.data?.truncated && <p className="text-xs text-warning">Very large project: only the first part is listed.</p>}
      </aside>

      {/* legend */}
      <div className="absolute bottom-3 left-3 z-10 max-w-[calc(100%-1.5rem)]">
        {legend && brain && deptOf && (
          <div className="mb-2 max-h-[40dvh] w-64 space-y-1 overflow-auto rounded-md border bg-card/95 p-3 backdrop-blur">
            <p className="label-caps text-[10px] text-muted-foreground">Departments</p>
            {deptOf.depts.map((d) => (
              <button key={d.id} onClick={() => setExpanded((e) => toggle(e, d.id))} className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-[12px] hover:bg-accent">
                <i className="size-2.5 shrink-0 rounded-full" style={{ background: d.isNotes ? "var(--success)" : groupColor(d.group, brain.groups, dark) }} />
                <span className="min-w-0 flex-1 truncate">{d.label}</span>
                <span className="text-muted-foreground">{d.count.toLocaleString()}</span>
              </button>
            ))}
            <div className="space-y-1 border-t pt-2 text-[11px] text-muted-foreground">
              <p className="label-caps text-[10px]">Selected file</p>
              <p><i className="mr-1.5 inline-block rounded px-1 text-[10px] font-bold text-black" style={{ background: "#ff7a1a" }}>→ 3</i>this file imports 3 files in that area</p>
              <p><i className="mr-1.5 inline-block rounded px-1 text-[10px] font-bold text-black" style={{ background: "#4da3ff" }}>← 3</i>3 files in that area import this file</p>
              <p><i className="mr-1.5 inline-block size-2 rounded-full ring-2 ring-foreground/50" />ringed dot: same folder or a relative of the selected file</p>
              <p><i className="mr-1.5 inline-block size-2 rounded-full bg-[#ffb020]" />amber ring: changed in the last 10 minutes</p>
              <p>Hover an area: a line goes to each area it shares imports with, and the badge on the other end says how many.</p>
              <p>Select a file: an orange line goes to each area with files it imports, a blue line comes from each area that imports it. The full family is in its Links tab.</p>
            </div>
          </div>
        )}
        <Button variant="outline" size="sm" className="border-2 border-foreground bg-background" onClick={() => setLegend((v) => !v)} aria-expanded={legend}>◆ Legend</Button>
      </div>

      {/* details: resizable by dragging its right edge (double-click toggles wide / default) */}
      {node && brain && (
        <>
          <Inspector
            className="absolute right-3 bottom-3 left-3 z-30 h-[60dvh] md:top-14 md:right-auto md:bottom-14 md:h-auto"
            style={desktop ? { width: Math.min(inspW, maxW()) } : undefined}
            projectId={projectId} brain={brain} node={node} notes={notes.data ?? []} dark={dark}
            onSelect={openFromLink} onReveal={reveal} onClose={clearSelection}
            canBack={canGoBack(nav, exists)} canForward={canGoForward(nav, exists)}
            backLabel={previousNode ? previousNode.path?.split("/").at(-1) ?? previousNode.label : undefined}
            onBack={() => setNav((n) => navBack(n, exists))} onForward={() => setNav((n) => navForward(n, exists))}
            onLocate={() => pick(node.id)} tabRequest={tabRequest} onGroup={setGroup}
          />
          {cardShown && (
            <Peek
              projectId={projectId} node={node} from={cameFrom} group={group} brain={brain} notes={notes.data ?? []}
              style={{ top: 56, left: 12 + Math.min(inspW, maxW()) + 14 }}
              onOpen={openFromLink}
              onBack={cameFrom ? () => setNav((n) => navBack(n, exists)) : null} onLocate={() => pick(node.id)}
              onCode={() => setTabRequest((r) => ({ tab: "code", n: r.n + 1 }))}
              onClose={() => setCardOff(`${nav.at}:${node.id}`)}
            />
          )}
          {desktop && (
            <div
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize details panel (drag, or use the arrow keys)"
              tabIndex={0}
              className={cn("absolute top-14 bottom-14 z-40 w-3 -translate-x-1/2 cursor-col-resize rounded hover:bg-primary/40 focus-visible:bg-primary/60", dragging && "bg-primary/60")}
              style={{ left: 12 + Math.min(inspW, maxW()) }}
              onPointerDown={(e) => { (e.target as HTMLElement).setPointerCapture(e.pointerId); setDragging(true); }}
              onPointerMove={(e) => { if (dragging) setWidth(e.clientX - (stage.current?.getBoundingClientRect().left ?? 0) - 12); }}
              onPointerUp={() => { setDragging(false); saveNumber(INSPECTOR_KEY, inspW); }}
              onDoubleClick={() => { const w = inspW > 700 ? 560 : maxW(); setWidth(w); saveNumber(INSPECTOR_KEY, w); }}
              onKeyDown={(e) => {
                if (e.key === "ArrowRight") { e.preventDefault(); setWidth(inspW + 32); saveNumber(INSPECTOR_KEY, inspW + 32); }
                if (e.key === "ArrowLeft") { e.preventDefault(); setWidth(inspW - 32); saveNumber(INSPECTOR_KEY, inspW - 32); }
              }}
            />
          )}
        </>
      )}
    </div>
  );
}
