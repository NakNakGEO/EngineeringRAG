import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
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
    <div className="grid">
      <Panel title="Root Policy (read-only)">
        <ErrorNote error={root.error} />
        {root.data && (
          <>
            <div className="stats"><Stat label="version" value={root.data.version} /><Stat label="digest" value={root.data.digest.slice(0, 12)} /><Stat label="mutable at runtime" value={<Badge value={String(root.data.mutable_at_runtime)} />} /></div>
            <p><small>Forbidden: {root.data.forbidden_capabilities.join(", ")}</small></p>
            <p><small>Always needs a human: {root.data.approval_required_actions.join(", ")}</small></p>
          </>
        )}
      </Panel>
      <Panel title="Pending approvals">
        <div className="row"><input placeholder="your name (approver)" value={who} onChange={(e) => setWho(e.target.value)} />{!adminToken && <small>Enter the admin token in the header to decide.</small>}</div>
        <ErrorNote error={decide.error} />
        {approvals.data?.length === 0 && <Empty>Nothing waiting.</Empty>}
        <ul className="list">{approvals.data?.map((a) => (
          <li key={a.id}><b>{a.action}</b> <small>{a.target} · by {a.requested_by} · {when(a.created_at)}</small><br /><small>{a.reason}</small>
            <div className="row"><button disabled={!adminToken || !who} onClick={() => decide.mutate({ id: a.id, approve: true })}>approve</button><button disabled={!adminToken || !who} onClick={() => decide.mutate({ id: a.id, approve: false })}>deny</button></div></li>
        ))}</ul>
      </Panel>
      <Panel title="Audit log">
        {audit.data && <VirtualList items={audit.data} rowHeight={34} keyOf={(a) => a.id} render={(a) => (
          <span className="evrow"><small>{when(a.at)}</small> <Badge value={a.effect} /> <b>{a.action}</b> <small>{a.actor_id} → {a.target ?? ""} · {a.rule_id}</small></span>
        )} />}
      </Panel>
    </div>
  );
}
