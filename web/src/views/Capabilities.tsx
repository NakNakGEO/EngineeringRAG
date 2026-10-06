import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { Badge, Empty, ErrorNote, Panel, VirtualList } from "../ui";

interface Cap { id: string; description: string; risk: string; providers: number }
interface Tool { id: string; version: string; state: string; origin: string; health: string; capabilities: string[]; routable: boolean }
interface Agent { id: string; role: string; state: string; can_write: boolean; description: string }
interface Skill { id: string; state: string; goal: string }
interface CapDetail { providers: { id: string; state: string; routable: boolean; health: string; metrics: { samples: number; success_rate: number | null } | null }[] }

export function Capabilities() {
  const caps = useQuery({ queryKey: ["caps"], queryFn: () => api<Cap[]>("/capabilities") });
  const tools = useQuery({ queryKey: ["tools"], queryFn: () => api<Tool[]>("/tools") });
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api<Agent[]>("/agents") });
  const skills = useQuery({ queryKey: ["skills"], queryFn: () => api<Skill[]>("/skills") });
  const [sel, setSel] = useState<string | null>(null);
  const detail = useQuery({ queryKey: ["cap", sel], enabled: !!sel, queryFn: () => api<CapDetail>(`/capabilities/${sel}`) });
  return (
    <div className="grid">
      <Panel title={`Capabilities (${caps.data?.length ?? 0})`}>
        <ErrorNote error={caps.error} />
        {caps.data && <VirtualList items={caps.data} keyOf={(c) => c.id} render={(c) => (
          <span className="evrow link" onClick={() => setSel(c.id)}>
            <Badge value={c.risk} /> <b>{c.id}</b> <small>{c.providers ? `${c.providers} provider(s)` : "GAP — no provider"}</small>
          </span>
        )} />}
      </Panel>
      <Panel title={sel ?? "Providers of…"}>
        {!sel ? <Empty>Select a capability.</Empty> : detail.data?.providers.length === 0 ? <Empty>No provider: a gap for the resolver/workshop.</Empty> : (
          <ul className="list">{detail.data?.providers.map((p) => (
            <li key={p.id}><b>{p.id}</b> <Badge value={p.state} /> <Badge value={p.routable ? "routable" : "not routable"} /> health <Badge value={p.health} />
              {p.metrics && <small> · {p.metrics.samples} runs{p.metrics.success_rate !== null ? ` · ${(p.metrics.success_rate * 100).toFixed(0)}% ok` : ""}</small>}</li>
          ))}</ul>
        )}
      </Panel>
      <Panel title="Tools / providers">
        <ul className="list">{tools.data?.map((t) => <li key={t.id}><b>{t.id}</b> {t.version} <Badge value={t.state} /> <small>{t.origin} · {t.capabilities.join(", ")}</small></li>)}</ul>
      </Panel>
      <Panel title={`Agents (${agents.data?.length ?? 0})`}>
        <ul className="list">{agents.data?.map((a) => <li key={a.id}><b>{a.role}</b> <Badge value={a.state} /> {a.can_write ? <Badge value="writer" /> : <small>reviewer</small>}</li>)}</ul>
      </Panel>
      <Panel title={`Skills (${skills.data?.length ?? 0})`}>
        <ul className="list">{skills.data?.map((s) => <li key={s.id}><b>{s.id}</b> <Badge value={s.state} /><br /><small>{s.goal}</small></li>)}</ul>
      </Panel>
    </div>
  );
}
