import { useQuery } from "@tanstack/react-query";
import { Box, Braces, FileText, Network, Search, Star, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { CodeView } from "../components/CodeView";
import { Markdown } from "../components/Markdown";
import { XmlTree } from "../components/XmlTree";
import { isMarkdownPath, isXmlPath } from "../lib/xml";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Input, Select } from "../components/ui/input";
import { EmptyState, ErrorNote, Skeleton } from "../components/ui/misc";
import { useOverview, type Project, type Source } from "../data";
import { cn } from "../lib/utils";
import { useTheme } from "../theme";

interface SymbolHit { id: string; path: string; name: string; qualified_name: string; kind: string; container: string | null; start_line: number; end_line: number; signature: string; exported: boolean }
type Pick = { path: string; start?: number; end?: number; title: string; kind: string };

/** "a/b/c.py" -> file name bold, folder muted underneath (the name is what you look for). */
function PathLabel({ path, extra }: { path: string; extra?: string }) {
  const i = path.lastIndexOf("/");
  return (
    <span className="min-w-0 flex-1" title={path}>
      <span className="block truncate text-[13px] font-semibold">{path.slice(i + 1)}</span>
      <span className="block truncate font-mono text-[11px] text-muted-foreground">{i < 0 ? "(project root)" : path.slice(0, i)}{extra}</span>
    </span>
  );
}

const KIND_RANK: Record<string, number> = { class: 0, interface: 0, function: 1, method: 2 };
const isCode = (k: string) => k in KIND_RANK;

function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function CodeSearch({ projects, projectId, onProject, onOpenMap }: {
  projects: Project[]; projectId: string; onProject: (id: string) => void; onOpenMap: (projectId: string, path: string) => void;
}) {
  const dark = useTheme() === "dark";
  const [typed, setTyped] = useState("");
  const q = useDebounced(typed.trim());
  const [pick, setPick] = useState<Pick | null>(null);
  const overview = useOverview(projectId);

  const symbols = useQuery({
    queryKey: ["symbols", projectId, q],
    enabled: !!projectId && q.length >= 2,
    queryFn: () => api<SymbolHit[]>(`/projects/${projectId}/symbols?q=${encodeURIComponent(q)}&limit=80`),
  });
  const hits = useMemo(() => {
    const list = [...(symbols.data ?? [])];
    list.sort((a, b) => (KIND_RANK[a.kind] ?? 9) - (KIND_RANK[b.kind] ?? 9) || Number(b.exported) - Number(a.exported) || a.qualified_name.length - b.qualified_name.length);
    return list;
  }, [symbols.data]);
  const files = useMemo(() => {
    if (q.length < 2) return [];
    const low = q.toLowerCase();
    return (overview.data?.files ?? []).filter((f) => f.path.toLowerCase().includes(low)).slice(0, 25);
  }, [overview.data, q]);

  // "Important" files: the ones most other files import.
  const popular = useMemo(() => {
    const n = new Map<string, number>();
    for (const [, dst] of overview.data?.imports ?? []) n.set(dst, (n.get(dst) ?? 0) + 1);
    return [...n].sort((a, b) => b[1] - a[1]).slice(0, 15);
  }, [overview.data]);

  useEffect(() => setPick(null), [projectId]);

  const source = useQuery({
    queryKey: ["codesrc", projectId, pick?.path, pick?.start, pick?.end],
    enabled: !!pick && !!projectId,
    retry: false,
    queryFn: () => {
      const start = Math.max(1, (pick!.start ?? 1) - 3);
      const end = pick!.end ? Math.min(pick!.end + 3, start + 399) : undefined;
      const qs = new URLSearchParams({ path: pick!.path, start_line: String(start), max_lines: "400", ...(end ? { end_line: String(end) } : {}) });
      return api<Source>(`/projects/${projectId}/source?${qs}`);
    },
  });
  const doc = !!pick && (isMarkdownPath(pick.path) || isXmlPath(pick.path));

  return (
    <div className="grid items-start gap-5 lg:grid-cols-[minmax(300px,420px)_minmax(0,1fr)]">
      <Card className={cn(pick && "hidden lg:block")}>
        <CardHeader className="pb-3"><CardTitle>Code search</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <Select aria-label="Project" value={projectId} onChange={(e) => onProject(e.target.value)}>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
          <div className="relative">
            <Search className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-muted-foreground" />
            <Input className="pl-8" autoFocus aria-label="Search code" placeholder="Function, class, method or file name…" value={typed} onChange={(e) => setTyped(e.target.value)} />
            {typed && <button className="absolute top-1.5 right-1.5 rounded p-1 text-muted-foreground hover:bg-accent" aria-label="Clear" onClick={() => setTyped("")}><X className="size-4" /></button>}
          </div>
          <p className="text-xs text-muted-foreground">
            Searches the functions, classes and files the system has read. It stores nothing: it looks up what was indexed.
          </p>
          <ErrorNote error={symbols.error} />

          <div className="max-h-[calc(100dvh-360px)] min-h-40 space-y-4 overflow-y-auto pr-1">
            {q.length < 2 ? (
              <section>
                <h4 className="label-caps mb-2 flex items-center gap-1.5 text-[10px] text-muted-foreground"><Star className="size-3" />Most used files</h4>
                {overview.isLoading && <Skeleton className="h-24 w-full" />}
                {popular.length === 0 && !overview.isLoading && <p className="text-[13px] text-muted-foreground">No import links were found in this project.</p>}
                <ul className="space-y-0.5">
                  {popular.map(([path, n]) => (
                    <li key={path}>
                      <button onClick={() => setPick({ path, title: path, kind: "file" })} className={cn("flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[13px] hover:bg-accent", pick?.path === path && "bg-accent")}>
                        <FileText className="size-4 shrink-0 text-muted-foreground" />
                        <PathLabel path={path} />
                        <span className="shrink-0 text-xs text-muted-foreground">used by {n}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            ) : (
              <>
                <section>
                  <h4 className="label-caps mb-2 text-[10px] text-muted-foreground">Functions &amp; classes {symbols.isFetching ? "…" : `(${hits.length})`}</h4>
                  {!symbols.isFetching && hits.length === 0 && <p className="text-[13px] text-muted-foreground">No function or class matches “{q}”.</p>}
                  <ul className="space-y-0.5">
                    {hits.map((h) => {
                      const Icon = h.kind === "class" || h.kind === "interface" ? Box : Braces;
                      const active = pick?.path === h.path && pick.start === h.start_line;
                      return (
                        <li key={h.id}>
                          <button
                            onClick={() => setPick({ path: h.path, start: h.start_line, end: h.end_line, title: h.qualified_name, kind: h.kind })}
                            className={cn("flex w-full items-start gap-2 rounded px-2 py-1.5 text-left hover:bg-accent", active && "bg-accent", !isCode(h.kind) && "opacity-75")}
                          >
                            <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-[13px] font-semibold">{h.qualified_name}</span>
                              <span className="block truncate font-mono text-[11px] text-muted-foreground" title={`${h.path}:${h.start_line}`}>{h.path.split("/").slice(-2).join("/")}:{h.start_line}</span>
                            </span>
                            <Badge tone="outline" className="shrink-0">{h.kind}</Badge>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </section>
                {files.length > 0 && (
                  <section>
                    <h4 className="label-caps mb-2 text-[10px] text-muted-foreground">Files ({files.length})</h4>
                    <ul className="space-y-0.5">
                      {files.map((f) => (
                        <li key={f.path}>
                          <button onClick={() => setPick({ path: f.path, title: f.path, kind: "file" })} className={cn("flex w-full items-center gap-2 rounded px-2 py-1.5 text-left hover:bg-accent", pick?.path === f.path && !pick.start && "bg-accent")}>
                            <FileText className="size-4 shrink-0 text-muted-foreground" />
                            <PathLabel path={f.path} />
                            <span className="shrink-0 text-xs text-muted-foreground">{f.lines} lines</span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  </section>
                )}
              </>
            )}
          </div>
        </CardContent>
      </Card>

      <Card className={cn("lg:sticky lg:top-20", !pick && "hidden lg:block")}>
        {pick ? (
          <>
            <CardHeader className="pb-2">
              <div className="min-w-0">
                <CardTitle className="truncate normal-case tracking-normal"><span className="truncate text-[15px]">{pick.title}</span></CardTitle>
                <p className="truncate font-mono text-xs text-muted-foreground">{pick.path}{pick.start ? `:${pick.start}–${pick.end}` : ""}</p>
              </div>
              <div className="flex gap-2">
                <Button variant="ghost" size="sm" className="lg:hidden" onClick={() => setPick(null)}>Back</Button>
                <Button variant="outline" size="sm" onClick={() => onOpenMap(projectId, pick.path)}><Network />Show on map</Button>
              </div>
            </CardHeader>
            <CardContent>
              {source.isLoading && <Skeleton className="h-64 w-full" />}
              <ErrorNote error={source.error} />
              {source.data && (() => {
                const code = <CodeView text={source.data.text} startLine={source.data.start_line} language={langOf(pick.path)} mark={pick.start ? [pick.start, pick.end ?? pick.start] : null} dark={dark} wrap />;
                if (!doc || pick.start) return code; // a symbol was picked: show its lines
                if (isXmlPath(pick.path)) return <XmlTree text={source.data.text} fallback={code} />;
                return <div className="rounded-md border px-4 py-2"><Markdown>{source.data.text}</Markdown></div>;
              })()}
            </CardContent>
          </>
        ) : <EmptyState icon={<Braces className="size-7 opacity-60" />} className="py-24">Search for a function, class or file, or pick one of the most used files.</EmptyState>}
      </Card>
    </div>
  );
}

const EXT: Record<string, string> = {
  py: "python", ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript", json: "json", yml: "yaml", yaml: "yaml",
  md: "markdown", sql: "sql", html: "html", css: "css", sh: "shell", php: "php", java: "java", cs: "csharp", go: "go", rs: "rust", toml: "toml",
};
const langOf = (path: string) => (isXmlPath(path) ? "xml" : EXT[path.split(".").at(-1)?.toLowerCase() ?? ""] ?? "plain");
