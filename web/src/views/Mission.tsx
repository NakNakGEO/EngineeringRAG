import { useQuery } from "@tanstack/react-query";
import { api, type RunPage } from "../api";
import { deriveMission } from "../mission";
import { useRunStream } from "../stream";
import { Badge, Empty, Panel, Stat, VirtualList, when } from "../ui";

export function RunPicker({ value, onChange }: { value: string | null; onChange: (id: string) => void }) {
  const runs = useQuery({ queryKey: ["runs"], queryFn: () => api<RunPage>("/runs?limit=50"), refetchInterval: 4000 });
  return (
    <select value={value ?? ""} onChange={(e) => onChange(e.target.value)} aria-label="run">
      <option value="">select a run…</option>
      {runs.data?.items.map((r) => (
        <option key={r.id} value={r.id}>
          {r.kind} · {r.status} · {r.goal.slice(0, 50) || r.id.slice(0, 8)}
        </option>
      ))}
    </select>
  );
}

export function Mission({ runId, onRun }: { runId: string | null; onRun: (id: string) => void }) {
  const stream = useRunStream(runId);
  const m = deriveMission(stream.events);
  return (
    <div className="grid">
      <Panel title="Mission Control" actions={<RunPicker value={runId} onChange={onRun} />}>
        {!runId ? (
          <Empty>Pick a run. Everything here is derived from its live event stream.</Empty>
        ) : (
          <>
            <p className="goal">{m.goal || "…"}</p>
            <div className="stats">
              <Stat label="outcome" value={<Badge value={m.outcome} />} />
              <Stat label="stream" value={<Badge value={stream.connection} />} />
              <Stat label="workflow node" value={m.workflowNode ?? "—"} />
              <Stat label="active agent" value={m.activeAgent ?? "—"} />
              <Stat label="skill" value={m.skill ?? "—"} />
              <Stat label="capability" value={m.capability ?? "—"} />
              <Stat label="provider" value={m.provider ?? "—"} />
              <Stat label="policy" value={m.policy ? <><Badge value={m.policy.effect} /> {m.policy.rule}</> : "—"} />
              <Stat label="context confidence" value={m.confidence === null ? "—" : m.confidence.toFixed(2)} />
              <Stat label="ready to act" value={m.readyToAct === null ? "—" : String(m.readyToAct)} />
              <Stat label="tokens" value={m.tokens} />
              <Stat label="LLM / tool calls" value={`${m.llmCalls} / ${m.toolCalls}`} />
              <Stat label="retries" value={m.retries} />
              <Stat label="build / test" value={`${m.build ?? "—"} / ${m.test ?? "—"}`} />
              <Stat label="duration" value={m.durationMs === null ? "—" : `${(m.durationMs / 1000).toFixed(1)}s`} />
              <Stat label="pending approval" value={m.pendingApproval ? m.pendingApproval.slice(0, 8) : "—"} />
            </div>
          </>
        )}
      </Panel>
      <Panel title={`Live events (${stream.events.length}${stream.dropped ? `, ${stream.dropped} older in API` : ""})`}>
        {stream.events.length === 0 ? (
          <Empty>No events yet.</Empty>
        ) : (
          <VirtualList
            items={[...stream.events].reverse()}
            keyOf={(e) => e.event_id}
            render={(e) => (
              <span className="evrow">
                <code>{e.seq}</code> <small>{when(e.timestamp)}</small> <Badge value={e.status} /> <b>{e.type}</b>{" "}
                <small>{e.actor_id}</small> {e.summary}
              </span>
            )}
          />
        )}
      </Panel>
    </div>
  );
}
