import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Badge, Empty, ErrorNote, Panel, Stat, VirtualList, when } from "../ui";

interface Approval { id: string; action: string; target: string | null; requested_by: string; reason: string; status: string; created_at: string }
interface Audit { id: string; at: string; actor_type: string; actor_id: string; action: string; target: string | null; effect: string; rule_id: string; reasons: string[] }
interface Root { version: string; digest: string; forbidden_capabilities: string[]; approval_required_actions: string[]; mutable_at_runtime: boolean }

export function Security({ adminToken }: { adminToken: string }) {
  const qc = useQueryClient();
  const root = useQuery({ queryKey: ["root"], queryFn: () => api<Root>("/policy/root") });
  const approvals = useQuery({ queryKey: ["approvals"], queryFn: () => api<Approval[]>("/approvals?status=pending"), refetchInterval: 4000 });
  const audit = useQuery({ queryKey: ["audit"], queryFn: () => api<Audit[]>("/audit?limit=300"), refetchInterval: 5000 });
  const [who, setWho] = useState("");
  const decide = useMutation({
    mutationFn: (v: { id: string; approve: boolean }) =>
      api(`/approvals/${v.id}/decision`, { method: "POST", adminToken, body: JSON.stringify({ approve: v.approve, decided_by: who, note: "via UI" }) }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["approvals"] }),
  });
  return (
    <div className="grid items-start gap-5 lg:grid-cols-2">
      <Panel title="Root policy" description="Read-only. Agents, tools and generated code cannot change it.">
        <ErrorNote error={root.error} />
        {root.data && (
          <>
            <div className="grid grid-cols-3 gap-2.5">
              <Stat label="Version" value={root.data.version} />
              <Stat label="Digest" value={root.data.digest.slice(0, 12)} />
              <Stat label="Mutable at runtime" value={<Badge value={String(root.data.mutable_at_runtime)} />} />
            </div>
            <div className="space-y-2 text-[13px]">
              <p><span className="font-medium">Forbidden: </span><span className="text-muted-foreground">{root.data.forbidden_capabilities.join(", ")}</span></p>
              <p><span className="font-medium">Always needs a human: </span><span className="text-muted-foreground">{root.data.approval_required_actions.join(", ")}</span></p>
            </div>
          </>
        )}
      </Panel>
      <Panel title="Pending approvals" description={adminToken ? "Decisions are recorded with your name." : "Enter the admin token in the sidebar to decide."}>
        <Input placeholder="your name (approver)" value={who} onChange={(e) => setWho(e.target.value)} aria-label="approver name" />
        <ErrorNote error={decide.error} />
        {approvals.data?.length === 0 && <Empty>Nothing is waiting for approval.</Empty>}
        <ul className="space-y-3">
          {approvals.data?.map((a) => (
            <li key={a.id} className="space-y-2 rounded-lg border p-3">
              <p className="text-sm font-medium">{a.action} <span className="font-normal text-muted-foreground">{a.target}</span></p>
              <p className="text-xs text-muted-foreground">by {a.requested_by} · {when(a.created_at)}</p>
              <p className="text-[13px]">{a.reason}</p>
              <div className="flex gap-2">
                <Button size="sm" disabled={!adminToken || !who} onClick={() => decide.mutate({ id: a.id, approve: true })}>Approve</Button>
                <Button size="sm" variant="outline" disabled={!adminToken || !who} onClick={() => decide.mutate({ id: a.id, approve: false })}>Deny</Button>
              </div>
            </li>
          ))}
        </ul>
      </Panel>
      <Panel title="Audit log" className="lg:col-span-2" description="Every policy decision, newest first.">
        {audit.data && (
          <VirtualList items={audit.data} rowHeight={40} height={440} keyOf={(a) => a.id} render={(a) => (
            <>
              <span className="w-20 shrink-0 text-xs text-muted-foreground">{when(a.at)}</span>
              <Badge value={a.effect} />
              <b className="shrink-0">{a.action}</b>
              <span className="truncate text-xs text-muted-foreground">{a.actor_id} → {a.target ?? ""} · {a.rule_id}</span>
            </>
          )} />
        )}
      </Panel>
    </div>
  );
}
