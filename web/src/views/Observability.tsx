import { useQuery } from "@tanstack/react-query";
import { api, type RunPage, type EventPage } from "../api";
import { useState } from "react";
import { Badge, Empty, ErrorNote, Panel, VirtualList, when } from "../ui";

interface Health { status: string; service: string; checks: { name: string; status: string; detail?: string }[] }
interface Workflow { id: string; run_id: string; goal: string; status: string; current_node: string | null; risk: string; primary_role: string }

export function Observability({ onOpen }: { onOpen: (runId: string) => void }) {
  const ready = useQuery({ queryKey: ["ready"], queryFn: () => api<Health>("/health/ready"), refetchInterval: 5000, retry: false });
  const [status, setStatus] = useState("");
  const runs = useQuery({ queryKey: ["runs", status], queryFn: () => api<RunPage>(`/runs?limit=100${status ? `&status=${status}` : ""}`), refetchInterval: 4000 });
  const workflows = useQuery({ queryKey: ["workflows"], queryFn: () => api<Workflow[]>("/workflows?limit=50"), refetchInterval: 5000 });
  const [typeFilter, setTypeFilter] = useState("");
  const [runId, setRunId] = useState("");
  const events = useQuery({
    queryKey: ["events", runId, typeFilter], enabled: !!runId,
    queryFn: () => api<EventPage>(`/runs/${runId}/events?limit=500${typeFilter ? `&type=${typeFilter}` : ""}`),
  });
  return (
    <div className="grid">
      <Panel title="Service health">
        <ErrorNote error={ready.error} />
        {ready.data && <p><Badge value={ready.data.status} /> {ready.data.service} {ready.data.checks.map((c) => <small key={c.name}> · {c.name}: {c.status}</small>)}</p>}
      </Panel>
      <Panel title="Runs" actions={<select value={status} onChange={(e) => setStatus(e.target.value)}><option value="">all</option>{["running", "completed", "failed", "cancelled"].map((s) => <option key={s}>{s}</option>)}</select>}>
        {runs.data && (runs.data.items.length === 0 ? <Empty>No runs.</Empty> : <VirtualList items={runs.data.items} keyOf={(r) => r.id} render={(r) => (
          <span className="evrow link" onClick={() => { setRunId(r.id); onOpen(r.id); }}><Badge value={r.status} /> <b>{r.kind}</b> <small>{when(r.created_at)} · {r.goal.slice(0, 60)}</small></span>
        )} />)}
      </Panel>
      <Panel title="Workflows">
        <ul className="list">{workflows.data?.map((w) => <li key={w.id} className="link" onClick={() => { setRunId(w.run_id); onOpen(w.run_id); }}><Badge value={w.status} /> <b>{w.current_node ?? "—"}</b> <small>risk {w.risk} · {w.primary_role} · {w.goal.slice(0, 60)}</small></li>)}</ul>
      </Panel>
      <Panel title="Event query (server-side filter)" actions={<input placeholder="event type e.g. POLICY_DENIED" value={typeFilter} onChange={(e) => setTypeFilter(e.target.value.toUpperCase())} />}>
        {!runId ? <Empty>Pick a run above.</Empty> : events.data && <VirtualList items={events.data.items} keyOf={(e) => e.event_id} render={(e) => <span className="evrow"><code>{e.seq}</code> <Badge value={e.status} /> <b>{e.type}</b> <small>{e.summary}</small></span>} />}
      </Panel>
    </div>
  );
}
