import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type EventPage, type RunPage } from "../api";
import { Button } from "../components/ui/button";
import { Input, Select } from "../components/ui/input";
import { EVENT_TYPES } from "../eventTypes";
import { cn } from "../lib/utils";
import { Badge, Empty, ErrorNote, Panel, VirtualList, when } from "../ui";
import { PageHelp } from "./PageHelp";

interface Health { status: string; service: string; checks: { name: string; status: string; detail?: string }[] }
interface Workflow { id: string; run_id: string; goal: string; status: string; current_node: string | null; risk: string; primary_role: string }

/** Runs and workflows are selected in place; the event query works on the selected run.
 * Leaving for Mission Control is a separate, explicit action. */
export function Observability({ initialRunId, onOpen }: { initialRunId: string | null; onOpen: (runId: string) => void }) {
  const ready = useQuery({ queryKey: ["ready"], queryFn: () => api<Health>("/health/ready"), refetchInterval: 5000, retry: false });
  const [status, setStatus] = useState("");
  const runs = useQuery({ queryKey: ["runs", status], queryFn: () => api<RunPage>(`/runs?limit=100${status ? `&status=${status}` : ""}`), refetchInterval: 4000 });
  const workflows = useQuery({ queryKey: ["workflows"], queryFn: () => api<Workflow[]>("/workflows?limit=50"), refetchInterval: 5000 });
  const [typeFilter, setTypeFilter] = useState("");
  const [runId, setRunId] = useState(initialRunId ?? "");
  const events = useQuery({
    queryKey: ["events", runId, typeFilter], enabled: !!runId,
    queryFn: () => api<EventPage>(`/runs/${runId}/events?limit=500${typeFilter ? `&type=${encodeURIComponent(typeFilter)}` : ""}`),
    // an error here is almost always "the api is restarting": keep trying until it answers
    refetchInterval: (q) => (q.state.status === "error" ? 4000 : false),
  });
  return (
    <div className="grid items-start gap-5 lg:grid-cols-2">
      <div className="lg:col-span-2">
        <PageHelp id="observability" title="What is Observability?">
          <p>The logbook and health check. <b>Service health</b> says whether the system&apos;s parts (database, API) are working. <b>Runs</b> lists every job it has done, <b>Workflows</b> the engineering workflows an assistant started, and <b>Event query</b> lets you filter the raw record, for example <code>POLICY_DENIED</code> to see everything the safety rules blocked.</p>
          <p><b>Why it is here:</b> the system promises that every important action leaves a record. This page is for checking that promise when something looks wrong. You do not need it day to day, and it is read-only.</p>
        </PageHelp>
      </div>
      <Panel title="Service health" className="lg:col-span-2">
        <ErrorNote error={ready.error} />
        {ready.data && (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge value={ready.data.status} /><b className="font-medium">{ready.data.service}</b>
            {ready.data.checks.map((c) => <span key={c.name} className="text-xs text-muted-foreground">{c.name}: {c.status}</span>)}
          </div>
        )}
      </Panel>
      <Panel
        title="Runs"
        actions={
          <Select aria-label="status filter" value={status} onChange={(e) => setStatus(e.target.value)} className="w-36">
            <option value="">all statuses</option>
            {["running", "completed", "failed", "cancelled"].map((s) => <option key={s}>{s}</option>)}
          </Select>
        }
      >
        {runs.data && (runs.data.items.length === 0 ? <Empty>No runs.</Empty> : (
          <VirtualList items={runs.data.items} keyOf={(r) => r.id} onSelect={(r) => setRunId(r.id)} selectedKey={runId} render={(r) => (
            <>
              <Badge value={r.status} /><b className="shrink-0">{r.kind}</b>
              <span className="truncate text-xs text-muted-foreground">{when(r.created_at)} · {r.goal.slice(0, 60)}</span>
            </>
          )} />
        ))}
      </Panel>
      <Panel title="Workflows">
        {workflows.data?.length === 0 && <Empty>No workflows yet.</Empty>}
        <ul className="max-h-[420px] space-y-1 overflow-auto">
          {workflows.data?.map((w) => (
            <li key={w.id}>
              <button onClick={() => setRunId(w.run_id)} className={cn("flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm hover:bg-accent", w.run_id === runId && "bg-accent")}>
                <Badge value={w.status} /><b className="font-medium">{w.current_node ?? "—"}</b>
                <span className="truncate text-xs text-muted-foreground">risk {w.risk} · {w.primary_role} · {w.goal.slice(0, 60)}</span>
              </button>
            </li>
          ))}
        </ul>
      </Panel>
      <Panel
        title="Event query"
        className="lg:col-span-2"
        description="The server filters by event type."
        actions={
          <>
            <Input className="w-56" placeholder="event type e.g. POLICY_DENIED" aria-label="event type" list="event-types" value={typeFilter} onChange={(e) => setTypeFilter(e.target.value.toUpperCase())} />
            <datalist id="event-types">{EVENT_TYPES.map((t) => <option key={t} value={t} />)}</datalist>
            <Button variant="outline" size="sm" disabled={!runId} onClick={() => onOpen(runId)}>Open in Mission Control</Button>
          </>
        }
      >
        <ErrorNote error={events.error} />
        {!runId ? <Empty>Pick a run or workflow above.</Empty> : events.data && (
          <>
            <p className="text-xs text-muted-foreground">run <code>{runId.slice(0, 8)}</code> · {events.data.items.length}{events.data.has_more ? "+" : ""} {events.data.items.length === 1 ? "event" : "events"}{typeFilter ? ` of type ${typeFilter}` : ""}</p>
            {events.data.items.length === 0 ? <Empty>No events match.</Empty> : (
              <VirtualList items={events.data.items} keyOf={(e) => e.event_id} render={(e) => (
                <>
                  <code className="w-8 shrink-0 text-xs text-muted-foreground">{e.seq}</code><Badge value={e.status} /><b className="shrink-0">{e.type}</b>
                  <span className="truncate text-xs text-muted-foreground">{e.summary}</span>
                </>
              )} />
            )}
          </>
        )}
      </Panel>
    </div>
  );
}
