import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Trash2, X } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { Badge, toneOf } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Input, Select } from "../components/ui/input";
import { EmptyState, ErrorNote, Skeleton } from "../components/ui/misc";
import { Tabs, TabsList, TabsTrigger } from "../components/ui/tabs";
import { useSelectedProject } from "../data";
import { explainError } from "../lib/adminError";
import { Panel } from "../ui";

interface MemoryItem { id: string; vault: string; project_id: string | null; scope: string; content: string; tags: string[]; importance: number; created_by: string; created_at: string | null }
interface Decision { id: string; question: string; selected: string; rationale: string; status: "proposed" | "accepted" | "rejected" | "superseded"; decider: string; alternatives: unknown[]; created_at: string }

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "");
const area = "min-h-[84px] w-full rounded-md border bg-card px-3 py-2 text-sm placeholder:text-muted-foreground";

/** What the system remembers about a project, and the decisions proposed for it. Anything an
 * assistant writes arrives marked as client-written and unverified. */
export function Memory({ adminToken, initialProject = "" }: { adminToken: string; initialProject?: string }) {
  const [projectId, setProjectId, list] = useSelectedProject(initialProject);
  const [tab, setTab] = useState<"memory" | "decisions">("memory");
  const qc = useQueryClient();
  const memories = useQuery({ queryKey: ["memory", projectId], enabled: !!projectId, queryFn: () => api<MemoryItem[]>(`/memory?project_id=${projectId}&limit=200`) });
  const decisions = useQuery({ queryKey: ["decisions", projectId], enabled: !!projectId, queryFn: () => api<Decision[]>(`/decisions?project_id=${projectId}&limit=200`) });

  const [text, setText] = useState("");
  const [tags, setTags] = useState("");
  const remember = useMutation({
    mutationFn: () => api("/memory", { method: "POST", body: JSON.stringify({ project_id: projectId, content: text, tags: tags.split(",").map((t) => t.trim()).filter(Boolean), created_by: "ui" }) }),
    onSuccess: () => { setText(""); setTags(""); void qc.invalidateQueries({ queryKey: ["memory", projectId] }); },
  });
  const forget = useMutation({
    mutationFn: (id: string) => api(`/memory/${id}`, { method: "DELETE", adminToken }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["memory", projectId] }),
  });

  const [question, setQuestion] = useState("");
  const [selected, setSelected] = useState("");
  const [why, setWhy] = useState("");
  const [who, setWho] = useState("");
  const propose = useMutation({
    mutationFn: () => api("/decisions", { method: "POST", body: JSON.stringify({ project_id: projectId, question, selected, rationale: why, proposer: "ui" }) }),
    onSuccess: () => { setQuestion(""); setSelected(""); setWhy(""); void qc.invalidateQueries({ queryKey: ["decisions", projectId] }); },
  });
  const decide = useMutation({
    mutationFn: (v: { id: string; act: "accept" | "reject" }) => api(`/decisions/${v.id}/${v.act}`, { method: "POST", adminToken, body: JSON.stringify({ approver: who, reason: "via UI" }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["decisions", projectId] }),
  });

  return (
    <div className="mx-auto max-w-4xl space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <Select aria-label="Project" value={projectId} onChange={(e) => setProjectId(e.target.value)} className="w-56">
          {list.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </Select>
        <Tabs value={tab} onValueChange={(v) => setTab(v as typeof tab)}>
          <TabsList>
            <TabsTrigger value="memory">Memory{memories.data ? ` ${memories.data.length}` : ""}</TabsTrigger>
            <TabsTrigger value="decisions">Decisions{decisions.data ? ` ${decisions.data.length}` : ""}</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>

      {tab === "memory" ? (
        <>
          <Panel title="Remember something" description="Short facts worth keeping about this project. Assistants write here too (through the memory_write capability); those are marked as written by a client and unverified.">
            <textarea className={area} value={text} onChange={(e) => setText(e.target.value)} placeholder="For example: the map opens areas in place and never moves the camera on a click." aria-label="What to remember" />
            <div className="flex flex-wrap items-center gap-2">
              <Input className="w-64" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="tags, separated by commas" aria-label="Tags" />
              <Button onClick={() => remember.mutate()} disabled={!text.trim() || !projectId || remember.isPending}>Remember</Button>
            </div>
            {remember.error && <p role="alert" className="text-[13px] text-danger">{explainError(remember.error)}</p>}
          </Panel>
          <ErrorNote error={memories.error} />
          {memories.isLoading && <Skeleton className="h-24 w-full" />}
          {memories.data?.length === 0 && <EmptyState>Nothing is remembered for this project yet.</EmptyState>}
          <ul className="space-y-2">
            {memories.data?.map((m) => (
              <li key={m.id} className="rounded-lg border bg-card p-3.5">
                <p className="whitespace-pre-wrap text-sm">{m.content}</p>
                <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                  <Badge tone="outline">{m.vault === "default" ? "all projects" : m.scope}</Badge>
                  {m.tags.map((t) => <Badge key={t} tone="outline">{t}</Badge>)}
                  <span>by {m.created_by} · {when(m.created_at)}</span>
                  <Button variant="ghost" size="sm" className="ml-auto h-7" onClick={() => forget.mutate(m.id)} disabled={forget.isPending} aria-label="Forget this"><Trash2 />Forget</Button>
                </div>
              </li>
            ))}
          </ul>
          {forget.error && <p role="alert" className="text-[13px] text-danger">{explainError(forget.error)}</p>}
        </>
      ) : (
        <>
          <Panel title="Propose a decision" description="Decisions start as proposed. Accepting or rejecting one is a human act (admin token).">
            <Input value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="The question, for example: should summaries be visible to Codex?" aria-label="Question" />
            <Input value={selected} onChange={(e) => setSelected(e.target.value)} placeholder="The answer you propose" aria-label="Proposed answer" />
            <textarea className={area} value={why} onChange={(e) => setWhy(e.target.value)} placeholder="Why (optional)" aria-label="Why" />
            <Button onClick={() => propose.mutate()} disabled={!question.trim() || !projectId || propose.isPending}>Propose</Button>
            {propose.error && <p role="alert" className="text-[13px] text-danger">{explainError(propose.error)}</p>}
          </Panel>
          <div className="flex items-center gap-2 text-sm">
            <label htmlFor="who" className="text-muted-foreground">Deciding as</label>
            <Input id="who" className="w-48" value={who} onChange={(e) => setWho(e.target.value)} placeholder="your name" />
          </div>
          <ErrorNote error={decisions.error} />
          {decisions.isLoading && <Skeleton className="h-24 w-full" />}
          {decisions.data?.length === 0 && <EmptyState>No decisions recorded for this project yet.</EmptyState>}
          <ul className="space-y-2">
            {decisions.data?.map((d) => (
              <li key={d.id} className="rounded-lg border bg-card p-3.5">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={toneOf(d.status === "accepted" ? "good" : d.status === "rejected" ? "bad" : "warn")}>{d.status}</Badge>
                  <p className="min-w-0 flex-1 text-sm font-semibold">{d.question}</p>
                </div>
                {d.selected && <p className="mt-1.5 text-sm"><span className="text-muted-foreground">Answer: </span>{d.selected}</p>}
                {d.rationale && <p className="mt-1 text-[13px] text-muted-foreground">{d.rationale}</p>}
                <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span>proposed by {d.decider} · {when(d.created_at)}</span>
                  {d.status === "proposed" && (
                    <span className="ml-auto flex gap-1.5">
                      <Button size="sm" variant="outline" className="h-7" disabled={!who.trim() || decide.isPending} onClick={() => decide.mutate({ id: d.id, act: "accept" })}><Check />Accept</Button>
                      <Button size="sm" variant="outline" className="h-7" disabled={!who.trim() || decide.isPending} onClick={() => decide.mutate({ id: d.id, act: "reject" })}><X />Reject</Button>
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
          {decide.error && <p role="alert" className="text-[13px] text-danger">{explainError(decide.error)}</p>}
        </>
      )}
    </div>
  );
}
