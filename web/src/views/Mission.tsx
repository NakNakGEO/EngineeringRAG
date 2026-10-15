import { useQuery } from "@tanstack/react-query";
import { useEffect, type ReactNode } from "react";
import { api, type RunPage } from "../api";
import { Select } from "../components/ui/input";
import { deriveMission } from "../mission";
import { useRunStream } from "../stream";
import { Badge, Empty, Panel, Stat, VirtualList, when } from "../ui";
import { PageHelp } from "./PageHelp";

const useRuns = () => useQuery({ queryKey: ["runs"], queryFn: () => api<RunPage>("/runs?limit=50"), refetchInterval: 4000 });

/** Open on the newest run instead of an empty page, until the person picks another. */
export function useNewestRun(runId: string | null, onRun: (id: string) => void) {
  const runs = useRuns();
  const newest = runs.data?.items[0]?.id;
  useEffect(() => { if (!runId && newest) onRun(newest); }, [runId, newest, onRun]);
}

export function RunPicker({ value, onChange }: { value: string | null; onChange: (id: string) => void }) {
  const runs = useRuns();
  return (
    <Select value={value ?? ""} onChange={(e) => onChange(e.target.value)} aria-label="run" className="w-full min-w-48 sm:w-72">
      <option value="">Select a run…</option>
      {runs.data?.items.map((r) => (
        <option key={r.id} value={r.id}>
          {r.kind} · {r.status} · {r.goal.slice(0, 50) || r.id.slice(0, 8)}
        </option>
      ))}
    </Select>
  );
}

export function Mission({ runId, onRun }: { runId: string | null; onRun: (id: string) => void }) {
  useNewestRun(runId, onRun);
  const stream = useRunStream(runId);
  const m = deriveMission(stream.events);
  // Only what this run actually has: a plain project sync has no agent, skill or build, so no empty boxes.
  const extra: [string, ReactNode | null][] = [
    ["Workflow step", m.workflowNode], ["Active agent", m.activeAgent],
    ["Reviewers", m.reviewers.length ? m.reviewers.join(", ") : null], ["Skill", m.skill],
    ["Capability", m.capability], ["Provider", m.provider],
    ["Context confidence", m.confidence === null ? null : m.confidence.toFixed(2)],
    ["Ready to act", m.readyToAct === null ? null : String(m.readyToAct)],
    ["Build / test", m.build || m.test ? `${m.build ?? "—"} / ${m.test ?? "—"}` : null],
    ["Pending approval", m.pendingApproval ? m.pendingApproval.slice(0, 8) : null],
  ];
  const present = extra.filter(([, v]) => v !== null);
  return (
    <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
      <div className="xl:col-span-2">
        <PageHelp id="mission" title="What is Mission Control?">
          <p>A live view of <b>one run</b>. A run is one job the system did: syncing a project, building context, or answering an assistant&apos;s request. The newest run is opened for you; use the picker to look at another.</p>
          <p><b>Why it is here:</b> every important action is recorded as an event, and this is how you watch that record. It shows what happened, whether policy allowed it, how long it took and how many model or tool calls it made. It is read-only: nothing here changes anything.</p>
          <p>Agent, skill, reviewer and build/test boxes only appear for engineering workflows an assistant starts. A plain project sync has none of those.</p>
        </PageHelp>
      </div>
      <Panel title="Mission Control" description="Everything here is derived from the run's live event stream." actions={<RunPicker value={runId} onChange={onRun} />}>
        {!runId ? (
          <Empty>No runs yet. A run appears whenever the system reads a project or answers a question.</Empty>
        ) : (
          <>
            <p className="text-base font-semibold tracking-tight">{m.goal || "…"}</p>
            <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
              <Stat label="Outcome" value={<Badge value={m.outcome} />} />
              <Stat label="Stream" value={<Badge value={stream.connection} />} />
              <Stat label="Duration" value={m.durationMs === null ? "—" : `${(m.durationMs / 1000).toFixed(1)}s`} />
              <Stat label="Policy" value={m.policy ? <><Badge value={m.policy.effect} /> <span className="text-sm font-normal">{m.policy.rule}</span></> : "—"} />
              <Stat label="LLM / tool calls" value={`${m.llmCalls} / ${m.toolCalls}`} />
              <Stat label="Tokens" value={m.tokens} />
              <Stat label="Retries" value={m.retries} />
              {present.map(([label, value]) => <Stat key={label} label={label} value={value} />)}
            </div>
            {present.length === 0 && <p className="text-xs text-muted-foreground">A plain job: it has no agent, skill, reviewer or build step, so those boxes are not shown.</p>}
          </>
        )}
      </Panel>
      <Panel title={`Live events (${stream.events.length}${stream.dropped ? `, ${stream.dropped} older in API` : ""})`}>
        {stream.events.length === 0 ? (
          <Empty>No events yet.</Empty>
        ) : (
          <VirtualList
            items={[...stream.events].reverse()}
            height={520}
            keyOf={(e) => e.event_id}
            render={(e) => (
              <>
                <code className="w-8 shrink-0 text-xs text-muted-foreground">{e.seq}</code>
                <span className="w-20 shrink-0 text-xs text-muted-foreground">{when(e.timestamp)}</span>
                <Badge value={e.status} />
                <b className="shrink-0">{e.type}</b>
                <span className="truncate text-muted-foreground">{e.actor_id} {e.summary}</span>
              </>
            )}
          />
        )}
      </Panel>
    </div>
  );
}
