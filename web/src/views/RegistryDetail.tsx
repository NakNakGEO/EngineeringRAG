import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api } from "../api";
import { Sheet } from "../components/ui/sheet";
import { Badge, ErrorNote } from "../ui";

export type Pick = { kind: "agent" | "skill" | "tool"; id: string };

interface Detail { id: string; version: string; origin: string; state: string; manifest: Record<string, unknown>; manifest_hash?: string; approved_by?: string | null; state_reason?: string; health_status?: string; health_detail?: string; history?: Record<string, string>[] }

const ENDPOINT = { agent: "agents", skill: "skills", tool: "tools" } as const;
const ABOUT = {
  agent: "A role brief. When an assistant asks for a specialist (request_specialist) it is handed this brief and the limits below to adopt. The reasoning stays with the assistant; nothing here runs by itself.",
  skill: "A checklist procedure an assistant can follow, step by step, for one kind of job. It names the capability each step needs; the system does not execute it for you.",
  tool: "A provider: the code that actually does a capability. This is what runs when an assistant invokes it, after policy allows it.",
} as const;

const text = (v: unknown): string => (typeof v === "string" ? v : "");
const list = (v: unknown): string[] => (Array.isArray(v) ? v.map((x) => (typeof x === "string" ? x : JSON.stringify(x))) : []);
const objs = (v: unknown): Record<string, unknown>[] => (Array.isArray(v) ? v.filter((x): x is Record<string, unknown> => typeof x === "object" && x !== null) : []);

function Section({ title, children }: { title: string; children: ReactNode }) {
  return <section className="space-y-1"><h3 className="label-caps text-[10px] text-muted-foreground">{title}</h3>{children}</section>;
}
const Bullets = ({ items, empty = "none" }: { items: string[]; empty?: string }) =>
  items.length === 0 ? <p className="text-xs text-muted-foreground">{empty}</p> : (
    <ul className="list-disc space-y-0.5 pl-5 text-[13px]">{items.map((i) => <li key={i}>{i}</li>)}</ul>
  );
const Chips = ({ items }: { items: string[] }) =>
  items.length === 0 ? <p className="text-xs text-muted-foreground">none</p> : (
    <div className="flex flex-wrap gap-1.5">{items.map((i) => <code key={i} className="rounded border bg-muted/40 px-1.5 py-0.5 text-xs">{i}</code>)}</div>
  );
const yesNo = (v: unknown) => (v === true || (typeof v === "string" && v !== "none") ? "yes" : "no");

function AgentBody({ m }: { m: Record<string, unknown> }) {
  const perms = (m.permissions ?? {}) as Record<string, unknown>;
  return (
    <>
      <Section title="What it is for"><p className="text-[13px]">{text(m.description)}</p></Section>
      <Section title="The brief it is given"><p className="whitespace-pre-wrap rounded-md border bg-muted/30 p-2.5 text-[13px]">{text(m.prompt)}</p></Section>
      <Section title="Capabilities it may use"><Chips items={list(m.capabilities)} /></Section>
      <Section title="Permissions">
        <p className="text-[13px]">Change files: <b>{yesNo(perms.write)}</b> · Network: <b>{yesNo(perms.network)}</b> · Folders: {list(perms.fs_scopes).join(", ") || "none"}</p>
      </Section>
      <Section title="Must never"><Bullets items={list(m.forbidden_actions)} /></Section>
      <Section title="Needs before its work is accepted"><Bullets items={list(m.review_requirements)} empty="nothing extra" /></Section>
    </>
  );
}

function SkillBody({ m }: { m: Record<string, unknown> }) {
  return (
    <>
      <Section title="Goal"><p className="text-[13px]">{text(m.goal)}</p></Section>
      <Section title="Use it when"><p className="text-[13px]">{text(m.trigger) || "—"}</p></Section>
      <Section title="Steps">
        <ol className="space-y-1.5">
          {objs(m.procedure).map((s, i) => (
            <li key={text(s.id) || i} className="rounded-md border px-2.5 py-1.5 text-[13px]">
              <span className="font-medium">{i + 1}. {text(s.instruction) || text(s.id)}</span>
              {text(s.capability) && <span className="ml-2 text-xs text-muted-foreground">uses <code>{text(s.capability)}</code></span>}
            </li>
          ))}
        </ol>
      </Section>
      <Section title="Needs these capabilities"><Chips items={list(m.required_capabilities)} /></Section>
      <Section title="Done when"><Bullets items={list(m.completion_criteria)} /></Section>
      <Section title="Counts as failed when"><Bullets items={list(m.failure_conditions)} empty="not stated" /></Section>
      <Section title="How it is checked"><Bullets items={list(m.verification)} empty="not stated" /></Section>
    </>
  );
}

function ToolBody({ d }: { d: Detail }) {
  const m = d.manifest;
  const perms = (m.permissions ?? {}) as Record<string, unknown>;
  return (
    <>
      <Section title="What it does"><p className="text-[13px]">{text(m.description)}</p></Section>
      <Section title="Provides capabilities"><Chips items={list(m.capabilities)} /></Section>
      <Section title="Health">
        <p className="text-[13px]"><Badge value={d.health_status ?? "unknown"} /> <span className="text-muted-foreground">{d.health_detail}</span></p>
      </Section>
      <Section title="Trust and checks">
        <p className="text-[13px]">Trust: <b>{text(m.trust) || "—"}</b> · Maturity: <b>{text(m.maturity) || "—"}</b> · Verification: <b>{text(m.verification_state) || "—"}</b></p>
      </Section>
      <Section title="What it may touch">
        <p className="text-[13px]">Network: <b>{text(perms.network) || "none"}</b> · Programs: {list(perms.process).join(", ") || "none"} · Folders: {list(perms.filesystem).join(", ") || "none"}</p>
      </Section>
      <Section title="Where it lives"><code className="text-xs">{text(m.entrypoint) || text(m.type)}</code></Section>
      {d.history && d.history.length > 0 && <Section title="State changes"><Bullets items={d.history.map((h) => `${h.created_at ?? ""} ${h.from_state ?? ""} → ${h.to_state ?? ""} ${h.reason ?? ""}`.trim())} /></Section>}
    </>
  );
}

/** The full definition of one agent, skill or tool, in plain language, in a side drawer. */
export function RegistrySheet({ pick, onClose }: { pick: Pick | null; onClose: () => void }) {
  const q = useQuery({
    queryKey: ["registry", pick?.kind, pick?.id], enabled: !!pick,
    queryFn: () => api<Detail>(`/${ENDPOINT[pick!.kind]}/${encodeURIComponent(pick!.id)}`),
  });
  const d = q.data;
  const title = pick ? `${pick.kind}: ${pick.id}` : "Details";
  return (
    <Sheet open={!!pick} onOpenChange={(o) => { if (!o) onClose(); }} title={title} side="right" className="w-[560px] max-w-[94vw]">
      <div className="flex-1 space-y-4 overflow-auto p-5 pr-12">
        {pick && <p className="label-caps text-[10px] text-muted-foreground">{pick.kind}</p>}
        <h2 className="text-lg font-semibold tracking-tight">{text(d?.manifest.role) || pick?.id}</h2>
        {pick && <p className="rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">{ABOUT[pick.kind]}</p>}
        <ErrorNote error={q.error} />
        {d && pick && (
          <>
            <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <Badge value={d.state} /><span>v{d.version}</span><span>{d.origin}</span>
              {pick.kind === "agent" && <Badge value={d.manifest.can_write ? "writer" : "reviewer"} />}
            </p>
            {pick.kind === "agent" && <AgentBody m={d.manifest} />}
            {pick.kind === "skill" && <SkillBody m={d.manifest} />}
            {pick.kind === "tool" && <ToolBody d={d} />}
            <p className="border-t pt-3 text-[11px] text-muted-foreground">
              {d.approved_by ? `Approved by ${d.approved_by}. ` : "Shipped with the system. "}
              {d.manifest_hash && <>Definition hash <code>{d.manifest_hash.slice(0, 12)}</code>. </>}
              {d.state_reason}
            </p>
          </>
        )}
      </div>
    </Sheet>
  );
}
