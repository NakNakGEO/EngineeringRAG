import { useQueries, useQuery } from "@tanstack/react-query";
import { ArrowLeft, BookOpen, FileText, Network, Search, X } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "../api";
import { Badge, toneOf } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Input, Select } from "../components/ui/input";
import { EmptyState, ErrorNote, Skeleton } from "../components/ui/misc";
import { Tabs, TabsList, TabsTrigger } from "../components/ui/tabs";
import { useSelectedProject, type Note } from "../data";
import { NoteBody } from "../graph/Inspector";
import { cn } from "../lib/utils";
import { CodeSearch } from "./CodeSearch";

interface Detail { item: Note; provenance: { source: string; source_version: string | null; actor: string; notes: string }[] }
interface Hit { item: Note; score: number }
interface Contradiction { id: string; item_a: string; item_b: string; reason: string; status: string }

/** A short plain-text preview of a Markdown body (no markers). */
const preview = (md: string) => md.replace(/[#>*_`|-]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 170);

export function Knowledge({ initialProject = "", onOpenMap }: { initialProject?: string; onOpenMap: (projectId: string, path?: string) => void }) {
  const [tab, setTab] = useState<"notes" | "code">("notes");
  const [projectId, setProjectId, list] = useSelectedProject(initialProject);
  const [notesProject, setNotesProject] = useState(initialProject); // "" = all projects
  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState(""); // submitted search; empty = browse everything
  const names = useMemo(() => new Map(list.map((p) => [p.id, p.name])), [list]);
  const scope = notesProject ? [notesProject] : list.map((p) => p.id);

  // Browse: every note of the chosen project(s), newest first. No search words needed.
  const browse = useQueries({
    queries: scope.map((id) => ({ queryKey: ["notes", id], queryFn: () => api<Note[]>(`/knowledge?project_id=${id}&limit=200`) })),
  });
  const searched = useQuery({
    queryKey: ["ksearch", query, scope.join(",")],
    enabled: query !== "" && tab === "notes",
    queryFn: async (): Promise<Note[]> => {
      const per = await Promise.all(scope.map((id) => api<Hit[]>("/knowledge/search", { method: "POST", body: JSON.stringify({ query, limit: 50, project_id: id }) })));
      const seen = new Map<string, { n: Note; s: number }>();
      for (const h of per.flat()) if (!seen.has(h.item.id)) seen.set(h.item.id, { n: h.item, s: h.score });
      return [...seen.values()].sort((a, b) => b.s - a.s).map((x) => x.n);
    },
  });

  const notes = useMemo(() => {
    const seen = new Map<string, Note>();
    for (const q of browse) for (const item of q.data ?? []) if (!seen.has(item.id)) seen.set(item.id, item);
    return [...seen.values()];
  }, [browse]);
  const shown = query ? (searched.data ?? []) : notes;
  const [sel, setSel] = useState<Note | null>(null);
  const detail = useQuery({ queryKey: ["note", sel?.id], enabled: !!sel, queryFn: () => api<Detail>(`/knowledge/${sel?.id}`) });
  const contradictions = useQuery({ queryKey: ["contradictions"], queryFn: () => api<Contradiction[]>("/governance/contradictions"), refetchInterval: 10_000 });

  const loading = browse.some((q) => q.isLoading);
  const status = query
    ? `${shown.length} result${shown.length === 1 ? "" : "s"} for “${query}”`
    : loading ? "Loading notes…"
    : `${notes.length} note${notes.length === 1 ? "" : "s"} ${notesProject ? `in ${names.get(notesProject) ?? "this project"}` : "across all projects"}, newest first`;
  const note = detail.data?.item ?? sel;
  const path = note?.metadata?.path;

  return (
    <div className="space-y-5">
      <Tabs value={tab} onValueChange={(v) => setTab(v as "notes" | "code")}>
        <TabsList><TabsTrigger value="notes"><BookOpen className="size-3.5" />Notes</TabsTrigger><TabsTrigger value="code"><Search className="size-3.5" />Code search</TabsTrigger></TabsList>
      </Tabs>

      {tab === "code" ? (
        <CodeSearch projects={list} projectId={projectId} onProject={setProjectId} onOpenMap={(id, p) => onOpenMap(id, p)} />
      ) : (
        <>
          <div className="grid items-start gap-5 lg:grid-cols-[minmax(300px,420px)_minmax(0,1fr)]">
            <Card className={cn(sel && "hidden lg:block")}>
              <CardHeader className="pb-3"><CardTitle>Notes</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                <form onSubmit={(e) => { e.preventDefault(); setQuery(typed.trim()); setSel(null); }} className="space-y-2">
                  <Select aria-label="Project" value={notesProject} onChange={(e) => { setNotesProject(e.target.value); setSel(null); }}>
                    <option value="">All projects</option>
                    {list.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                  </Select>
                  <div className="relative">
                    <Search className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-muted-foreground" />
                    <Input className="pl-8" aria-label="Search notes" placeholder="Filter notes by words…" value={typed} onChange={(e) => setTyped(e.target.value)} />
                    {query && <button type="button" className="absolute top-1.5 right-1.5 rounded p-1 text-muted-foreground hover:bg-accent" aria-label="Clear search" onClick={() => { setTyped(""); setQuery(""); setSel(null); }}><X className="size-4" /></button>}
                  </div>
                </form>
                <p className="text-xs text-muted-foreground" data-testid="note-count">{status}</p>
                <ErrorNote error={searched.error ?? browse.find((q) => q.error)?.error} />
                {loading && <div className="space-y-2"><Skeleton className="h-16 w-full" /><Skeleton className="h-16 w-full" /></div>}
                {!loading && !query && notes.length === 0 && <EmptyState icon={<BookOpen className="size-7 opacity-60" />}>No notes yet. They are added through the API (POST /knowledge) or the MCP server.</EmptyState>}
                {query && !searched.isFetching && shown.length === 0 && <EmptyState>No results. Clear the search to see all notes.</EmptyState>}
                <div className="max-h-[calc(100dvh-340px)] min-h-40 space-y-2 overflow-y-auto pr-1">
                  {shown.map((h) => (
                    <button
                      key={h.id}
                      onClick={() => setSel(h)}
                      aria-pressed={sel?.id === h.id}
                      className={cn("block w-full rounded-md border p-3 text-left transition-colors hover:bg-accent/60", sel?.id === h.id ? "border-primary bg-accent" : "bg-card")}
                    >
                      <span className="block text-[13.5px] leading-snug font-semibold">{h.title}</span>
                      <span className="mt-1 line-clamp-2 block text-xs text-muted-foreground">{preview(h.content)}</span>
                      <span className="mt-2 flex flex-wrap items-center gap-1.5">
                        <FileText className="size-3.5 text-muted-foreground" />
                        <Badge tone="outline">{h.kind}</Badge>
                        {h.metadata?.path && <span className="max-w-[160px] truncate font-mono text-[10.5px] text-muted-foreground">{h.metadata.path}</span>}
                      </span>
                    </button>
                  ))}
                </div>
              </CardContent>
            </Card>

            <Card className={cn("lg:sticky lg:top-20", !sel && "hidden lg:block")}>
              {sel && note ? (
                <>
                  <CardHeader className="pb-2">
                    <Button variant="ghost" size="sm" className="-ml-2 lg:hidden" onClick={() => setSel(null)}><ArrowLeft />Back to list</Button>
                    <div className="ml-auto flex flex-wrap items-center gap-2">
                      {path && <span className="font-mono text-xs text-muted-foreground">{path}</span>}
                      <Button variant="outline" size="sm" onClick={() => onOpenMap(note.project_id ?? notesProject ?? list[0]?.id ?? "", path)}><Network />Show on map</Button>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-4">
                    <h2 className="text-2xl leading-snug font-semibold tracking-tight">{note.title}</h2>
                    <NoteBody note={note} />
                    {detail.data && detail.data.provenance.length > 0 && (
                      <section>
                        <h4 className="label-caps mb-2 text-[10px] text-muted-foreground">Where it came from</h4>
                        <ul className="space-y-1.5">
                          {detail.data.provenance.map((p, i) => (
                            <li key={i} className="rounded-md border bg-muted/40 px-3 py-2 text-[13px]"><code className="font-mono text-xs break-all">{p.source}</code><br /><span className="text-xs text-muted-foreground">by {p.actor}{p.notes ? ` · ${p.notes}` : ""}</span></li>
                          ))}
                        </ul>
                      </section>
                    )}
                  </CardContent>
                </>
              ) : <EmptyState icon={<BookOpen className="size-7 opacity-60" />} className="py-24">Choose a note on the left to read it here.</EmptyState>}
            </Card>
          </div>

          {(contradictions.data?.length ?? 0) > 0 && (
            <Card>
              <CardHeader><CardTitle>Open contradictions</CardTitle></CardHeader>
              <CardContent>
                <ul className="space-y-2 text-sm">
                  {contradictions.data?.map((c) => <li key={c.id}><Badge tone={toneOf(c.status)}>{c.status}</Badge> {c.reason} <span className="text-xs text-muted-foreground">{c.item_a.slice(0, 8)} ↔ {c.item_b.slice(0, 8)}</span></li>)}
                </ul>
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
