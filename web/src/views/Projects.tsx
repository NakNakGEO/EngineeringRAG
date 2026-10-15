import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { useProjects } from "../data";
import { formatMetric } from "../format";
import { cn } from "../lib/utils";
import { Badge, Empty, ErrorNote, Panel, Stat, VirtualList } from "../ui";

interface FileRow { id: string; path: string; language: string; line_count: number; status: string }

// Plain-language names for the raw coverage keys the API returns.
const LABEL: Record<string, string> = {
  files_total: "Files indexed", parsable_files: "Parsable files", files_with_symbols: "Files with symbols", files_with_knowledge: "Files with notes",
  content_files: "Content files", files_with_content: "Files with content", content_chunks: "Content chunks", symbol_coverage: "Code understood",
  knowledge_coverage: "Notes coverage", content_coverage: "Content coverage", parse_errors: "Parse errors",
};

export function Projects({ initialProject = "" }: { initialProject?: string }) {
  const projects = useProjects();
  const [pid, setPid] = useState<string | null>(initialProject || null);
  const coverage = useQuery({ queryKey: ["coverage", pid], enabled: !!pid, queryFn: () => api<Record<string, unknown>>(`/projects/${pid}/coverage`) });
  const files = useQuery({ queryKey: ["files", pid], enabled: !!pid, queryFn: () => api<FileRow[]>(`/projects/${pid}/files?limit=1000`) });
  const cov = coverage.data ?? {};
  return (
    <div className="grid items-start gap-5 lg:grid-cols-[minmax(260px,340px)_minmax(0,1fr)]">
      <Panel title="Projects" description="Choose one to see how well it was understood.">
        <ErrorNote error={projects.error} />
        {projects.data?.length === 0 && <Empty>No project indexed yet.</Empty>}
        <ul className="space-y-1.5">
          {projects.data?.map((p) => (
            <li key={p.id}>
              <button
                onClick={() => setPid(p.id)}
                aria-pressed={p.id === pid}
                className={cn("block w-full rounded-lg border p-3 text-left transition-colors hover:bg-accent/60", p.id === pid && "border-primary bg-accent")}
              >
                <span className="flex items-center justify-between gap-2"><b className="truncate text-sm">{p.name}</b><Badge value={p.bootstrap_state} /></span>
                <span className="mt-0.5 block truncate text-xs text-muted-foreground">{p.last_branch} {p.last_commit?.slice(0, 8)}</span>
              </button>
            </li>
          ))}
        </ul>
      </Panel>
      <div className="grid min-w-0 gap-5">
        <Panel title="How well it was understood">
          {!pid ? <Empty>Select a project on the left.</Empty> : (
            <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 xl:grid-cols-4">
              {Object.entries(cov).filter(([, v]) => typeof v !== "object").map(([k, v]) => <Stat key={k} label={LABEL[k] ?? k.replace(/_/g, " ")} value={formatMetric(k, v)} />)}
            </div>
          )}
        </Panel>
        <Panel title={`Files (${files.data?.length ?? 0})`} description="First 1,000 files by path.">
          {!pid ? <Empty>Select a project to list its files.</Empty> : files.data && (
            <VirtualList items={files.data} height={460} rowHeight={38} keyOf={(f) => f.id} render={(f) => (
              <>
                <code className="truncate font-mono text-xs">{f.path}</code>
                <span className="ml-auto shrink-0 text-xs text-muted-foreground">{f.language} · {f.line_count} lines</span>
                <Badge value={f.status} />
              </>
            )} />
          )}
        </Panel>
      </div>
    </div>
  );
}
