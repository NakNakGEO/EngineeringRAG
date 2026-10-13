import { useMutation, useQueries, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { formatMetric } from "../format";
import { fromCode, fromKnowledge, vaultState, type CodeItem, type KnowledgeHit, type SearchResult } from "../knowledge";
import { Badge, Empty, ErrorNote, Panel, VirtualList } from "../ui";

interface Project { id: string; name: string }
interface Contradiction { id: string; item_a: string; item_b: string; reason: string; status: string }
type Mode = "knowledge" | "code";
interface Outcome { mode: Mode; results: SearchResult[] }

export function Knowledge() {
  const [mode, setMode] = useState<Mode>("knowledge");
  const [q, setQ] = useState("");
  const [projectId, setProjectId] = useState("");
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects"), refetchInterval: 8000 });
  const list = projects.data ?? [];
  // Code search is always scoped to one project; knowledge search may span all of them.
  const project = projectId || (mode === "code" ? (list[0]?.id ?? "") : "");
  const coverages = useQueries({
    queries: list.map((p) => ({ queryKey: ["coverage", p.id], queryFn: () => api<Record<string, unknown>>(`/projects/${p.id}/coverage`) })),
  });
  const vault = vaultState(list.flatMap((p, i) => (!project || p.id === project ? [coverages[i]?.data] : [])));
  const search = useMutation({
    mutationFn: async (): Promise<Outcome> => {
      if (mode === "code") {
        const r = await api<{ items: CodeItem[] }>("/retrieval/search", { method: "POST", body: JSON.stringify({ text: q, project_id: project, limit: 50 }) });
        return { mode, results: r.items.map(fromCode) };
      }
      const hits = await api<KnowledgeHit[]>("/knowledge/search", { method: "POST", body: JSON.stringify({ query: q, limit: 50, project_id: project || null }) });
      return { mode, results: hits.map(fromKnowledge) };
    },
  });
  const contradictions = useQuery({ queryKey: ["contradictions"], queryFn: () => api<Contradiction[]>("/governance/contradictions"), refetchInterval: 10000 });
  const [sel, setSel] = useState<SearchResult | null>(null);
  const switchMode = (next: Mode) => { setMode(next); setSel(null); search.reset(); };
  const ratio = vault ? formatMetric("knowledge_coverage", vault.files ? vault.linked / vault.files : 0) : "";
  return (
    <div className="grid">
      <Panel
        title="Knowledge Explorer"
        actions={
          <div className="actions" role="group" aria-label="search source">
            <button className={mode === "knowledge" ? "on" : ""} onClick={() => switchMode("knowledge")}>Knowledge</button>
            <button className={mode === "code" ? "on" : ""} onClick={() => switchMode("code")}>Code</button>
          </div>
        }
      >
        {mode === "knowledge" && vault && (vault.empty ? (
          <p className="banner" data-testid="vault-empty">
            <b>Empty vault.</b> No knowledge item is linked to any indexed file ({vault.linked} of {vault.files} files, coverage {ratio}),
            so knowledge search has nothing to match. Add knowledge through the API or MCP, or switch to Code to search symbols and files.
          </p>
        ) : (
          <p className="empty" data-testid="vault-stats">{vault.linked} of {vault.files} files have linked knowledge (coverage {ratio}).</p>
        ))}
        <form onSubmit={(e) => { e.preventDefault(); if (q.trim()) search.mutate(); }} className="row">
          <input placeholder={mode === "code" ? "search symbols and files…" : "search knowledge…"} aria-label="query" value={q} onChange={(e) => setQ(e.target.value)} />
          <select aria-label="project" value={project} onChange={(e) => setProjectId(e.target.value)}>
            {mode === "knowledge" && <option value="">all projects</option>}
            {list.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <button disabled={!q.trim() || search.isPending || (mode === "code" && !project)}>search</button>
        </form>
        <ErrorNote error={search.error} />
        {search.data && (search.data.results.length === 0 ? (
          <Empty>{search.data.mode === "knowledge" && vault?.empty ? "No results: the vault is empty." : "No results."}</Empty>
        ) : (
          <VirtualList items={search.data.results} keyOf={(h) => h.id} onSelect={setSel} selectedKey={sel?.id} render={(h) => (
            <span className="evrow">
              {h.trust && <Badge value={h.trust} />} {h.health && <Badge value={h.health} />} <b>{h.title}</b> <small>{h.kind} · {h.where} · {h.score.toFixed(2)}</small>
            </span>
          )} />
        ))}
      </Panel>
      <Panel title="Item">
        {sel ? (
          <>
            <h4>{sel.title}</h4>
            <p>
              {sel.trust && <Badge value={sel.trust} />} {sel.health && <Badge value={sel.health} />}
              {sel.confidence !== null && <> confidence {sel.confidence.toFixed(2)}</>} <small>{sel.kind} · {sel.where}{sel.sources.length ? ` · via ${sel.sources.join(", ")}` : ""}</small>
            </p>
            <pre className="content">{sel.body.slice(0, 4000)}</pre>
          </>
        ) : <Empty>Select a result.</Empty>}
      </Panel>
      <Panel title="Open contradictions">
        <ErrorNote error={contradictions.error} />
        {contradictions.data?.length === 0 && <Empty>None.</Empty>}
        <ul className="list">{contradictions.data?.map((c) => <li key={c.id}><Badge value={c.status} /> {c.reason}<br /><small>{c.item_a.slice(0, 8)} ↔ {c.item_b.slice(0, 8)}</small></li>)}</ul>
      </Panel>
    </div>
  );
}
