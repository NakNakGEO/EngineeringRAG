import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { Badge, Empty, ErrorNote, Panel } from "../ui";

interface Proposal { id: string; kind: string; name: string; version: string; state: string; creator: string }
interface Detail extends Proposal { history: { from: string | null; to: string; actor: string; reason: string }[]; test_results: { passed: boolean; results: { id: string; passed: boolean; detail: string }[] } | null; scan_report: { ok: boolean; findings: string[] } | null; capability_claims: string[] }
interface Resolution { status: string; message: string; plan: string[]; artifact_kind: string | null; needs_human: boolean }

export function Workshop({ adminToken }: { adminToken: string }) {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["proposals"], queryFn: () => api<Proposal[]>("/workshop/proposals"), refetchInterval: 5000 });
  const [sel, setSel] = useState<string | null>(null);
  const detail = useQuery({ queryKey: ["proposal", sel], enabled: !!sel, queryFn: () => api<Detail>(`/workshop/proposals/${sel}`) });
  const [approver, setApprover] = useState("");
  const act = useMutation({
    mutationFn: (step: string) => api(`/workshop/proposals/${sel}/${step}`, { method: "POST", body: JSON.stringify({ approver, reason: "via UI" }), adminToken }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["proposals"] }); void qc.invalidateQueries({ queryKey: ["proposal", sel] }); },
  });
  const [need, setNeed] = useState("");
  const gap = useMutation({ mutationFn: () => api<Resolution>("/gaps/resolve", { method: "POST", body: JSON.stringify({ description: need }) }) });
  const next: Record<string, string[]> = { DRAFT: ["sandbox"], SANDBOXED: ["test"], TESTED: ["register"], EXPERIMENTAL: ["verify"], VERIFIED: ["trust"] };
  return (
    <div className="grid">
      <Panel title="Resolve a capability gap">
        <form className="row" onSubmit={(e) => { e.preventDefault(); if (need.trim()) gap.mutate(); }}>
          <input placeholder="describe the need…" value={need} onChange={(e) => setNeed(e.target.value)} />
          <button disabled={!need.trim()}>resolve</button>
        </form>
        <ErrorNote error={gap.error} />
        {gap.data && <p><Badge value={gap.data.status} /> {gap.data.message} {gap.data.plan.length > 0 && <small>plan: {gap.data.plan.join(" → ")}</small>}</p>}
      </Panel>
      <Panel title={`Proposals (${list.data?.length ?? 0})`}>
        <ErrorNote error={list.error} />
        {list.data?.length === 0 && <Empty>No proposals.</Empty>}
        <ul className="list">{list.data?.map((p) => <li key={p.id} className={p.id === sel ? "sel" : ""} onClick={() => setSel(p.id)}><Badge value={p.state} /> <b>{p.name}</b> <small>{p.kind} {p.version} · {p.creator}</small></li>)}</ul>
      </Panel>
      <Panel title="Lifecycle">
        {!detail.data ? <Empty>Select a proposal.</Empty> : (
          <>
            <p>DRAFT → SANDBOXED → TESTED → EXPERIMENTAL → VERIFIED → TRUSTED. Now: <Badge value={detail.data.state} /></p>
            {detail.data.scan_report && <p>static scan: <Badge value={detail.data.scan_report.ok ? "ok" : "failed"} /> <small>{detail.data.scan_report.findings.join("; ")}</small></p>}
            {detail.data.test_results && <ul className="list">{detail.data.test_results.results.map((r) => <li key={r.id}><Badge value={r.passed ? "passed" : "failed"} /> {r.id} <small>{r.detail}</small></li>)}</ul>}
            <div className="row">
              <input placeholder="approver name (human steps)" value={approver} onChange={(e) => setApprover(e.target.value)} />
              {(next[detail.data.state] ?? []).map((s) => <button key={s} disabled={act.isPending} onClick={() => act.mutate(s)}>{s}</button>)}
            </div>
            <ErrorNote error={act.error} />
            <h4>History</h4>
            <ul className="list">{detail.data.history.map((h, i) => <li key={i}>{h.from ?? "∅"} → <b>{h.to}</b> <small>{h.actor} — {h.reason}</small></li>)}</ul>
          </>
        )}
      </Panel>
    </div>
  );
}
