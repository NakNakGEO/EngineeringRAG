import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { cn } from "../lib/utils";
import { Badge, Empty, ErrorNote, Panel } from "../ui";

interface Proposal { id: string; kind: string; name: string; version: string; state: string; creator: string }
interface Detail extends Proposal { history: { from: string | null; to: string; actor: string; reason: string }[]; test_results: { passed: boolean; results: { id: string; passed: boolean; detail: string }[] } | null; scan_report: { ok: boolean; findings: string[] } | null; capability_claims: string[] }
interface Resolution { status: string; message: string; plan: string[]; artifact_kind: string | null; needs_human: boolean }

const STEPS = ["DRAFT", "SANDBOXED", "TESTED", "EXPERIMENTAL", "VERIFIED", "TRUSTED"];

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
    <div className="grid items-start gap-5 lg:grid-cols-2">
      <Panel title="Resolve a capability gap" description="Describe what is missing; the system proposes how to get it.">
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (need.trim()) gap.mutate(); }}>
          <Input placeholder="describe the need…" value={need} onChange={(e) => setNeed(e.target.value)} />
          <Button disabled={!need.trim()}>Resolve</Button>
        </form>
        <ErrorNote error={gap.error} />
        {gap.data && (
          <div className="space-y-1.5 rounded-lg border bg-muted/40 p-3 text-[13px]">
            <p><Badge value={gap.data.status} /> {gap.data.message}</p>
            {gap.data.plan.length > 0 && <p className="text-xs text-muted-foreground">plan: {gap.data.plan.join(" → ")}</p>}
          </div>
        )}
      </Panel>
      <Panel title={`Proposals (${list.data?.length ?? 0})`}>
        <ErrorNote error={list.error} />
        {list.data?.length === 0 && <Empty>No proposals yet.</Empty>}
        <ul className="space-y-1">
          {list.data?.map((p) => (
            <li key={p.id}>
              <button onClick={() => setSel(p.id)} className={cn("flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm hover:bg-accent", p.id === sel && "bg-accent")}>
                <Badge value={p.state} /><b className="truncate font-medium">{p.name}</b>
                <span className="ml-auto shrink-0 text-xs text-muted-foreground">{p.kind} {p.version} · {p.creator}</span>
              </button>
            </li>
          ))}
        </ul>
      </Panel>
      <Panel title="Lifecycle" className="lg:col-span-2" description="A proposal moves one step at a time. Human steps need the admin token.">
        {!detail.data ? <Empty>Select a proposal.</Empty> : (
          <div className="space-y-4">
            <ol className="flex flex-wrap items-center gap-1.5 text-xs">
              {STEPS.map((s, i) => (
                <li key={s} className="flex items-center gap-1.5">
                  <span className={cn("rounded-full border px-2.5 py-1", s === detail.data.state ? "border-primary bg-primary text-primary-foreground" : "text-muted-foreground")}>{s}</span>
                  {i < STEPS.length - 1 && <span className="text-muted-foreground">→</span>}
                </li>
              ))}
            </ol>
            {detail.data.scan_report && <p className="text-[13px]">Static scan: <Badge value={detail.data.scan_report.ok ? "ok" : "failed"} /> <span className="text-muted-foreground">{detail.data.scan_report.findings.join("; ")}</span></p>}
            {detail.data.test_results && (
              <ul className="space-y-1 text-[13px]">
                {detail.data.test_results.results.map((r) => <li key={r.id}><Badge value={r.passed ? "passed" : "failed"} /> {r.id} <span className="text-xs text-muted-foreground">{r.detail}</span></li>)}
              </ul>
            )}
            <div className="flex flex-wrap gap-2">
              <Input className="max-w-xs" placeholder="approver name (human steps)" value={approver} onChange={(e) => setApprover(e.target.value)} />
              {(next[detail.data.state] ?? []).map((s) => <Button key={s} size="sm" disabled={act.isPending} onClick={() => act.mutate(s)}>{s}</Button>)}
            </div>
            <ErrorNote error={act.error} />
            <div>
              <h4 className="mb-1.5 text-xs font-semibold tracking-wide text-muted-foreground uppercase">History</h4>
              <ul className="space-y-1 text-[13px]">
                {detail.data.history.map((h, i) => <li key={i}>{h.from ?? "∅"} → <b>{h.to}</b> <span className="text-xs text-muted-foreground">{h.actor} — {h.reason}</span></li>)}
              </ul>
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}
