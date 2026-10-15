import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { api } from "../api";
import { Badge, Empty, ErrorNote, Panel, VirtualList } from "../ui";
import { PageHelp } from "./PageHelp";
import { RegistrySheet, type Pick } from "./RegistryDetail";

interface Cap { id: string; description: string; risk: string; providers: number }
interface Tool { id: string; version: string; state: string; origin: string; health: string; capabilities: string[]; routable: boolean }
interface Agent { id: string; role: string; state: string; can_write: boolean; description: string }
interface Skill { id: string; state: string; goal: string }
interface CapDetail { providers: { id: string; state: string; routable: boolean; health: string; metrics: { samples: number; success_rate: number | null } | null }[] }

/** Why a capability has no provider here: it is done by the connected assistant, on purpose. */
const CLIENT_SIDE: Record<string, string> = {
  file_edit: "The connected assistant edits files itself; this system only reads projects.",
  test_run: "Running a project's tests needs that project's own tools, so the assistant does it on your machine.",
  build_run: "Same: building needs the project's own tools; the assistant does it.",
  lint_run: "Same: linters belong to the project; the assistant runs them.",
  code_review: "The connected assistant reviews the change; use request_specialist for the reviewer role.",
  sql_generate: "The assistant writes SQL as text for a human to run; the system never touches a database.",
  migration_plan: "The assistant plans the migration; the system supplies the facts (impact, history).",
};

const Row = ({ children }: { children: ReactNode }) => <li className="space-y-1 border-b py-2.5 last:border-0">{children}</li>;

/** A list row that opens the full definition of an agent, skill or tool. */
const OpenRow = ({ onOpen, label, children }: { onOpen: () => void; label: string; children: ReactNode }) => (
  <li className="border-b last:border-0">
    <button type="button" onClick={onOpen} aria-label={`Open ${label}`} className="block w-full space-y-1 rounded-md px-1.5 py-2.5 text-left hover:bg-accent/60">
      {children}
    </button>
  </li>
);

export function Capabilities() {
  const caps = useQuery({ queryKey: ["caps"], queryFn: () => api<Cap[]>("/capabilities") });
  const tools = useQuery({ queryKey: ["tools"], queryFn: () => api<Tool[]>("/tools") });
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api<Agent[]>("/agents") });
  const skills = useQuery({ queryKey: ["skills"], queryFn: () => api<Skill[]>("/skills") });
  const [sel, setSel] = useState<string | null>(null);
  const [pick, setPick] = useState<Pick | null>(null);
  const detail = useQuery({ queryKey: ["cap", sel], enabled: !!sel, queryFn: () => api<CapDetail>(`/capabilities/${sel}`) });
  return (
    <div className="grid items-start gap-5 lg:grid-cols-2">
      <div className="lg:col-span-2">
        <PageHelp id="capabilities" title="What is on this page?">
          <p><b>Capabilities</b> are the things the system can do for an assistant (search knowledge, scan for secrets, analyze a log…). <b>Tools</b> are the code that does each one.
            <b> Agents</b> are role briefs (architect, security engineer…) an assistant can ask for. <b>Skills</b> are step-by-step procedures it can follow.</p>
          <p>Click any capability, tool, agent or skill to see its full definition. A &quot;gap&quot; is a capability with no tool here, usually because your assistant does it itself.</p>
        </PageHelp>
      </div>
      <Panel title={`Capabilities (${caps.data?.length ?? 0})`} description="What the system knows how to do. Pick one to see who provides it.">
        <ErrorNote error={caps.error} />
        {caps.data && (
          <VirtualList items={caps.data} keyOf={(c) => c.id} onSelect={(c) => setSel(c.id)} selectedKey={sel} render={(c) => (
            <>
              <Badge value={c.risk} />
              <b className="truncate">{c.id}</b>
              <span className="ml-auto shrink-0 text-xs text-muted-foreground">{c.providers ? `${c.providers} provider(s)` : "gap: no provider"}</span>
            </>
          )} />
        )}
      </Panel>
      <Panel title={sel ?? "Providers"} description={sel ? undefined : "Choose a capability."}>
        {!sel ? <Empty>Select a capability on the left.</Empty> : detail.data?.providers.length === 0 ? (
          <Empty>
            <span className="block">No provider here.</span>
            <span className="mt-1 block text-xs text-muted-foreground">
              {CLIENT_SIDE[sel] ?? "A gap for the resolver / workshop. A human approves any executable it proposes."}
            </span>
          </Empty>
        ) : (
          <ul>
            {detail.data?.providers.map((p) => (
              <Row key={p.id}>
                <p className="font-medium">{p.id}</p>
                <div className="flex flex-wrap items-center gap-1.5">
                  <Badge value={p.state} /><Badge value={p.routable ? "routable" : "not routable"} /><Badge value={p.health} />
                  {p.metrics && <span className="text-xs text-muted-foreground">{p.metrics.samples} runs{p.metrics.success_rate !== null ? ` · ${(p.metrics.success_rate * 100).toFixed(0)}% ok` : ""}</span>}
                </div>
              </Row>
            ))}
          </ul>
        )}
      </Panel>
      <Panel title={`Tools / providers (${tools.data?.length ?? 0})`} description="The code behind each capability. Click one to see what it may touch.">
        <ul className="max-h-96 overflow-auto">
          {tools.data?.map((t) => (
            <OpenRow key={t.id} label={t.id} onOpen={() => setPick({ kind: "tool", id: t.id })}>
              <p><b className="font-medium">{t.id}</b> <span className="text-xs text-muted-foreground">{t.version}</span> <Badge value={t.state} /></p>
              <p className="text-xs text-muted-foreground">{t.origin} · {t.capabilities.join(", ")}</p>
            </OpenRow>
          ))}
        </ul>
      </Panel>
      <div className="grid gap-5">
        <Panel title={`Agents (${agents.data?.length ?? 0})`} description="Role briefs an assistant can ask for. Click one to read its brief and limits.">
          <ul>
            {agents.data?.map((a) => (
              <OpenRow key={a.id} label={a.role} onOpen={() => setPick({ kind: "agent", id: a.id })}>
                <p className="flex flex-wrap items-center gap-1.5"><b className="font-medium">{a.role}</b><Badge value={a.state} />{a.can_write ? <Badge value="writer" /> : <span className="text-xs text-muted-foreground">reviewer</span>}</p>
                {a.description && <p className="text-xs text-muted-foreground">{a.description}</p>}
              </OpenRow>
            ))}
          </ul>
        </Panel>
        <Panel title={`Skills (${skills.data?.length ?? 0})`} description="Step-by-step procedures. Click one to see its steps.">
          <ul>
            {skills.data?.map((s) => (
              <OpenRow key={s.id} label={s.id} onOpen={() => setPick({ kind: "skill", id: s.id })}>
                <p className="flex items-center gap-1.5"><b className="font-medium">{s.id}</b><Badge value={s.state} /></p>
                <p className="text-xs text-muted-foreground">{s.goal}</p>
              </OpenRow>
            ))}
          </ul>
        </Panel>
      </div>
      <RegistrySheet pick={pick} onClose={() => setPick(null)} />
    </div>
  );
}
