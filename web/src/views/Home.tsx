import { useQueries, useQuery } from "@tanstack/react-query";
import {
  Activity, ArrowRight, BookOpen, FileDiff, FilePlus2, FileX2, FolderGit2, GitCommitHorizontal, MessageSquareText,
  Network, Play, Plus, Radar, Undo2, Zap, type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api, type RunPage } from "../api";
import { Cloud, type CloudDot } from "../components/Cloud";
import { Badge, toneOf } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { EmptyState } from "../components/ui/misc";
import { useBrain, useSelectedProject, type Coverage, type Note } from "../data";
import { groupColor } from "../graph/model";
import { useLive, type ActivityEvent } from "../live";
import { ago, cn } from "../lib/utils";
import { useTheme } from "../theme";

export type Target = "Project Map" | "Knowledge" | "Projects" | "Capabilities" | "Workshop" | "Mission Control" | "Observability";

interface Skill { id: string; state: string; goal: string }

function Head({ icon: Icon, title, right }: { icon: LucideIcon; title: string; right?: ReactNode }) {
  return (
    <CardHeader>
      <CardTitle><Icon className="size-4" />{title}</CardTitle>
      {right}
    </CardHeader>
  );
}

function useNow(ms = 1000) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

interface Row { id: string; icon: LucideIcon; tone: string; title: string; detail: string; project: string; at: string; kind: "added" | "modified" | "removed" | "reverted" | "sync" | "question" }

function describe(e: ActivityEvent, name: (id: string | null) => string): Row | null {
  const project = e.project_id ? name(e.project_id) : "";
  const base = { id: e.event_id, project, at: e.timestamp };
  if (e.type === "FILE_CHANGED") {
    const c = (e.data.change as string | undefined) ?? "modified";
    const map: Record<string, [LucideIcon, string, string, Row["kind"]]> = {
      added: [FilePlus2, "New file", "text-success", "added"],
      removed: [FileX2, "File removed", "text-danger", "removed"],
      reverted: [Undo2, "Back to committed", "text-muted-foreground", "reverted"],
      modified: [FileDiff, "File changed", "text-warning", "modified"],
    };
    const [icon, title, tone, kind] = map[c] ?? map.modified!;
    return { ...base, icon, title, tone, kind, detail: e.data.path ?? e.summary };
  }
  if (e.type === "PROJECT_SYNCED") return { ...base, icon: GitCommitHorizontal, title: "Index updated", tone: "text-primary", kind: "sync", detail: e.summary };
  if (e.type === "RUN_STARTED" && e.run_kind === "project_sync") return { ...base, icon: Play, title: "Reading the project", tone: "text-info", kind: "sync", detail: "Looking for changes…" };
  if (e.type === "CONTEXT_REQUESTED") return { ...base, icon: MessageSquareText, title: "Question answered", tone: "text-info", kind: "question", detail: e.summary };
  return null;
}

interface RingItem { id: string; title: string; icon: LucideIcon; onClick: () => void }

/** The ring of round launchers around the cloud: projects first, then the newest notes. */
function Orbit({ items, label, setLabel }: { items: RingItem[]; label: string; setLabel: (s: string) => void }) {
  const n = Math.max(1, items.length);
  return (
    <>
      {items.map((it, i) => {
        const a = (i / n) * Math.PI * 2 - Math.PI / 2;
        const Icon = it.icon;
        return (
          <button
            key={it.id}
            title={it.title}
            aria-label={it.title}
            onMouseEnter={() => setLabel(it.title)}
            onFocus={() => setLabel(it.title)}
            onMouseLeave={() => setLabel("")}
            onClick={it.onClick}
            className="absolute grid size-8 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full border border-input bg-card text-muted-foreground hover:border-primary hover:text-primary sm:size-9"
            style={{ left: `${50 + 47 * Math.cos(a)}%`, top: `${50 + 47 * Math.sin(a)}%` }}
          >
            <Icon className="size-4" />
          </button>
        );
      })}
      <p className="pointer-events-none absolute inset-x-0 bottom-[9%] truncate px-[12%] text-center text-xs text-muted-foreground">{label || "Hover a ring item to see what it is"}</p>
    </>
  );
}

export function Home({ onOpen }: { onOpen: (tab: Target, projectId: string) => void }) {
  const [focus, setFocus, list] = useSelectedProject();
  const live = useLive();
  const dark = useTheme() === "dark";
  const now = useNow();
  const { brain } = useBrain(focus);
  const names = useMemo(() => new Map(list.map((p) => [p.id, p.name])), [list]);

  const cov = useQueries({ queries: list.map((p) => ({ queryKey: ["coverage", p.id], queryFn: () => api<Coverage>(`/projects/${p.id}/coverage`) })) });
  const noteQs = useQueries({ queries: list.map((p) => ({ queryKey: ["notes", p.id], queryFn: () => api<Note[]>(`/knowledge?project_id=${p.id}&limit=200`) })) });
  const skills = useQuery({ queryKey: ["skills"], queryFn: () => api<Skill[]>("/skills") });
  const runs = useQuery({ queryKey: ["runs", "home"], queryFn: () => api<RunPage>("/runs?limit=7"), refetchInterval: 5000 });

  const totalFiles = cov.reduce((n, q) => n + (q.data?.files_total ?? 0), 0);
  const allNotes = noteQs.flatMap((q, i) => (q.data ?? []).map((n) => ({ ...n, project: list[i]?.id ?? "" })));
  const [hover, setHover] = useState("");

  const rows = useMemo(() => {
    const out: Row[] = [];
    for (const e of live.events) {
      const r = describe(e, (id) => (id ? names.get(id) ?? "" : ""));
      if (r) out.push(r);
      if (out.length >= 40) break;
    }
    return out;
  }, [live.events, names]);
  const lastHour = live.events.filter((e) => e.type === "FILE_CHANGED" && Date.now() - new Date(e.timestamp).getTime() < 3_600_000);
  const mix = useMemo(() => {
    const keys: [Row["kind"], string, string][] = [["added", "new", "var(--success)"], ["modified", "changed", "var(--primary)"], ["removed", "removed", "var(--danger)"], ["sync", "reads", "var(--info)"], ["question", "questions", "var(--warning)"]];
    const counts = keys.map(([k, label, color]) => ({ label, color, n: rows.filter((r) => r.kind === k).length }));
    return { counts, total: Math.max(1, counts.reduce((s, c) => s + c.n, 0)) };
  }, [rows]);

  const pulses = live.pulses(focus);
  const dots: CloudDot[] = useMemo(
    () => brain?.nodes.filter((n) => n.kind === "file").map((n) => ({ group: n.group, weight: Math.log2(n.lines + 2), changed: pulses.has(n.path ?? "") })) ?? [],
    [brain, pulses],
  );
  const colors = useMemo(() => new Map((brain?.groups ?? []).map((g) => [g, groupColor(g, brain!.groups, dark)])), [brain, dark]);

  const ring: RingItem[] = [
    ...list.map((p) => ({ id: `p:${p.id}`, title: `Project: ${p.name}`, icon: FolderGit2, onClick: () => onOpen("Project Map", p.id) })),
    ...[...allNotes].sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1)).slice(0, 26).map((n) => ({ id: `n:${n.id}`, title: n.title, icon: BookOpen, onClick: () => onOpen("Knowledge", n.project) })),
  ];

  return (
    <div className="grid gap-4 xl:grid-cols-[300px_minmax(0,1fr)_340px]">
      {/* left column */}
      <div className="space-y-4 xl:order-1">
        <Card>
          <Head icon={FolderGit2} title="Projects" />
          <CardContent className="space-y-1 pb-3">
            {list.length === 0 && <EmptyState>No project indexed yet. Run <code className="font-mono">py scripts\eios_cli.py add &lt;folder&gt;</code>.</EmptyState>}
            {list.map((p, i) => (
              <button
                key={p.id}
                onClick={() => setFocus(p.id)}
                className={cn("flex w-full items-center gap-3 rounded px-2 py-2 text-left hover:bg-accent", p.id === focus && "bg-accent")}
              >
                <span className="grid size-9 shrink-0 place-items-center rounded border bg-muted"><FolderGit2 className="size-[18px]" /></span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13.5px] font-semibold">{p.name}</span>
                  <span className="block truncate text-xs text-muted-foreground">{cov[i]?.data?.files_total?.toLocaleString() ?? "…"} files · {noteQs[i]?.data?.length ?? "…"} notes</span>
                </span>
                <Badge tone={toneOf(p.bootstrap_state)}>{p.bootstrap_state}</Badge>
              </button>
            ))}
          </CardContent>
        </Card>

        <Card>
          <Head icon={Radar} title="Watcher" right={<Badge tone={live.online ? "good" : "bad"} dot>{live.online ? "live" : "offline"}</Badge>} />
          <CardContent className="space-y-4">
            <div>
              <p className="font-mono text-3xl font-semibold tracking-tight text-primary tabular-nums">{now.toLocaleTimeString([], { hour12: false })}</p>
              <p className="label-caps mt-1 text-[10px] text-muted-foreground">Checking every few seconds</p>
            </div>
            <ul className="space-y-2 text-[13px]">
              {list.map((p) => {
                const t = live.lastSync.get(p.id) ?? p.last_synced_at;
                return (
                  <li key={p.id} className="flex items-center justify-between gap-2">
                    <span className="truncate">{p.name}</span>
                    <span className="shrink-0 text-xs text-muted-foreground">{t ? `read ${ago(t)}` : "not read yet"}</span>
                  </li>
                );
              })}
            </ul>
          </CardContent>
        </Card>
      </div>

      {/* centre: the OS */}
      <section className="order-first xl:order-2" aria-label="Overview">
        <div className="text-center">
          <h1 className="label-caps text-[22px] tracking-[0.2em] sm:text-[26px]">Engineering <span className="text-primary">OS</span></h1>
          <p className="mt-1 text-[11px] tracking-[0.18em] text-muted-foreground uppercase">{list.length} projects · {totalFiles.toLocaleString()} files · {allNotes.length} notes</p>
          <div className="mt-3 flex justify-center gap-2">
            <Button variant="outline" size="sm" onClick={() => onOpen("Project Map", focus)}><Network />Open map</Button>
            <Button variant="outline" size="sm" onClick={() => onOpen("Knowledge", focus)}><BookOpen />Notes</Button>
          </div>
        </div>
        <div className="relative mx-auto mt-4 aspect-square w-full max-w-[640px]">
          <div className="absolute inset-[7%] rounded-full border border-dashed" />
          <Cloud className="absolute inset-0 size-full" dots={dots} colors={colors} />
          <Orbit items={ring} label={hover} setLabel={setHover} />
        </div>
      </section>

      {/* right column */}
      <div className="space-y-4 xl:order-3">
        <Card>
          <Head icon={Activity} title="Live activity" right={<span className="label-caps text-[10px] text-muted-foreground">{live.online ? "updating" : "offline"}</span>} />
          <CardContent className="space-y-4">
            <div className="flex items-baseline gap-3">
              <span className="text-5xl font-semibold tracking-tight text-primary tabular-nums">{lastHour.length}</span>
              <span className="label-caps text-[10px] leading-4 text-muted-foreground">File changes<br />last hour</span>
            </div>
            <div>
              <p className="label-caps mb-2 text-[10px] text-muted-foreground">Latest</p>
              {rows.length === 0 && <p className="text-[13px] text-muted-foreground">Quiet right now. Edit a file in a watched project and it shows up here within seconds.</p>}
              <ul className="space-y-1.5">
                {rows.slice(0, 6).map((r, i) => {
                  const Icon = r.icon;
                  return (
                    <li key={r.id} className={cn("flex items-start gap-2.5 rounded px-1.5 py-1", i === 0 && "bg-accent")}>
                      <Icon className={cn("mt-0.5 size-4 shrink-0", r.tone)} />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-[13px] font-medium">{r.title}{r.project && <span className="font-normal text-muted-foreground"> · {r.project}</span>}</p>
                        <p className="truncate font-mono text-[11px] text-muted-foreground" title={r.detail}>{r.detail}</p>
                      </div>
                      <time className="shrink-0 text-[11px] text-muted-foreground" dateTime={r.at}>{ago(r.at)}</time>
                    </li>
                  );
                })}
              </ul>
            </div>
            <div>
              <p className="label-caps mb-1.5 text-[10px] text-muted-foreground">Recent mix</p>
              <div className="flex h-1.5 gap-0.5 overflow-hidden rounded-full bg-muted">
                {mix.counts.map((c) => <i key={c.label} style={{ width: `${(c.n / mix.total) * 100}%`, background: c.color }} />)}
              </div>
              <p className="mt-1.5 flex flex-wrap gap-x-3 text-[10px] tracking-wider text-muted-foreground uppercase">
                {mix.counts.filter((c) => c.n > 0).map((c) => <span key={c.label}>{c.n} {c.label}</span>)}
              </p>
            </div>
          </CardContent>
        </Card>

        <Card>
          <Head icon={Zap} title="Skills deck" right={<Button size="sm" variant="outline" className="h-7 border-primary text-primary" onClick={() => onOpen("Workshop", focus)}><Plus />Add skill</Button>} />
          <CardContent>
            {(skills.data?.length ?? 0) === 0 && <p className="text-[13px] text-muted-foreground">No skills registered yet.</p>}
            <div className="grid grid-cols-2 gap-2">
              {skills.data?.slice(0, 4).map((s) => (
                <div key={s.id} className="flex flex-col justify-between gap-2 rounded-md border bg-muted p-2.5">
                  <div className="min-w-0">
                    <p className="truncate font-mono text-[13px] text-primary">/{s.id}</p>
                    <Badge tone={toneOf(s.state)} className="mt-1">{s.state}</Badge>
                  </div>
                  <button className="grid size-6 place-items-center rounded border border-primary text-primary hover:bg-primary hover:text-primary-foreground" aria-label={`Open ${s.id} in capabilities`} onClick={() => onOpen("Capabilities", focus)}><Play className="size-3" /></button>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>

        <Card>
          <Head icon={Activity} title="Routines" right={<button className="label-caps flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground" onClick={() => onOpen("Observability", focus)}>All runs <ArrowRight className="size-3" /></button>} />
          <CardContent>
            <table className="w-full text-[12.5px]">
              <thead>
                <tr className="label-caps text-left text-[10px] text-muted-foreground"><th className="pb-2 font-semibold">Time</th><th className="pb-2 font-semibold">Routine</th><th className="pb-2 text-right font-semibold">Status</th></tr>
              </thead>
              <tbody>
                {runs.data?.items.map((r) => {
                  const running = r.status === "running";
                  return (
                    <tr key={r.id} className="border-t">
                      <td className="py-1.5 pr-2 font-mono text-muted-foreground">{new Date(r.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}</td>
                      <td className="max-w-[150px] truncate py-1.5 pr-2" title={r.goal}>{r.kind.replace(/_/g, " ")}</td>
                      <td className="py-1.5 text-right"><Badge tone={running ? "warn" : toneOf(r.status)}>{running ? "now" : r.status === "completed" ? "done" : r.status}</Badge></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {(runs.data?.items.length ?? 0) === 0 && <p className="text-[13px] text-muted-foreground">No runs yet.</p>}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
