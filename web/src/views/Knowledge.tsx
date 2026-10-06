import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { Badge, Empty, ErrorNote, Panel, VirtualList } from "../ui";

interface Hit { item: { id: string; title: string; kind: string; trust: string; health: string; vault: string; content: string; confidence: number }; score: number }
interface Contradiction { id: string; item_a: string; item_b: string; reason: string; status: string }

export function Knowledge() {
  const [q, setQ] = useState("");
  const [projectId, setProjectId] = useState("");
  const search = useMutation({
    mutationFn: () => api<Hit[]>("/knowledge/search", { method: "POST", body: JSON.stringify({ query: q, limit: 50, project_id: projectId || null }) }),
  });
  const contradictions = useQuery({ queryKey: ["contradictions"], queryFn: () => api<Contradiction[]>("/governance/contradictions"), refetchInterval: 10000 });
  const [sel, setSel] = useState<Hit | null>(null);
  return (
    <div className="grid">
      <Panel title="Knowledge Explorer">
        <form onSubmit={(e) => { e.preventDefault(); if (q.trim()) search.mutate(); }} className="row">
          <input placeholder="search knowledge…" value={q} onChange={(e) => setQ(e.target.value)} />
          <input placeholder="project id (optional)" value={projectId} onChange={(e) => setProjectId(e.target.value)} />
          <button disabled={!q.trim() || search.isPending}>search</button>
        </form>
        <ErrorNote error={search.error} />
        {search.data && (search.data.length === 0 ? <Empty>No results.</Empty> : (
          <VirtualList items={search.data} keyOf={(h) => h.item.id} render={(h) => (
            <span className="evrow link" onClick={() => setSel(h)}>
              <Badge value={h.item.trust} /> <Badge value={h.item.health} /> <b>{h.item.title}</b> <small>{h.item.kind} · {h.item.vault} · {h.score.toFixed(2)}</small>
            </span>
          )} />
        ))}
      </Panel>
      <Panel title="Item">
        {sel ? (
          <>
            <h4>{sel.item.title}</h4>
            <p><Badge value={sel.item.trust} /> <Badge value={sel.item.health} /> confidence {sel.item.confidence.toFixed(2)}</p>
            <pre className="content">{sel.item.content.slice(0, 4000)}</pre>
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
